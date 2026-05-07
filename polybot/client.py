"""Polymarket CLOB + Gamma API client.

Combines two data sources:

* **Gamma API** (``gamma-api.polymarket.com``) — gives market metadata,
  24h volume, liquidity, current bid/ask, token IDs, UMA resolution
  status. No auth needed. Used for the scanner.

* **CLOB API** (``clob.polymarket.com``) via ``py-clob-client`` — needed
  for placing orders, fetching the order book, and querying our own
  USDC balance + open orders. ``py-clob-client`` is synchronous so we
  wrap it in ``asyncio.to_thread`` to keep the loop happy.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    BalanceAllowanceParams,
    OrderArgs,
    OrderType,
)
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY, SELL  # noqa: F401

from .config import SETTINGS
from .logger import get_logger
from .models import Market, Outcome
from .retry import with_retries

log = get_logger(__name__)

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"


def _redact_proxy_creds(url: str) -> str:
    """Hide ``user:pass@`` from a proxy URL before logging it."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    creds, _, host = rest.rpartition("@")
    if not creds:
        return url
    return f"{scheme}://***@{host}"


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        # Gamma sometimes returns trailing 'Z'; fromisoformat needs +00:00.
        cleaned = value.replace("Z", "+00:00")
        return datetime.fromisoformat(cleaned).astimezone(timezone.utc)
    except ValueError:
        return None


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalise_market(raw: Dict[str, Any]) -> Optional[Market]:
    """Convert one Gamma-API record into our Market dataclass."""
    if not raw or raw.get("closed") is None:
        return None

    # Outcomes + prices come as JSON-encoded strings. clobTokenIds too.
    import json
    try:
        outcomes_raw = json.loads(raw.get("outcomes") or "[]")
        prices_raw = json.loads(raw.get("outcomePrices") or "[]")
        token_ids = json.loads(raw.get("clobTokenIds") or "[]")
    except (TypeError, ValueError):
        return None

    if not outcomes_raw or len(outcomes_raw) != len(token_ids):
        return None

    outcomes: List[Outcome] = []
    for i, name in enumerate(outcomes_raw):
        price = _to_float(prices_raw[i]) if i < len(prices_raw) else 0.0
        outcomes.append(Outcome(name=str(name), token_id=str(token_ids[i]), price=price))

    return Market(
        condition_id=raw.get("conditionId", ""),
        question=raw.get("question", ""),
        slug=raw.get("slug", ""),
        end_date=_parse_iso(raw.get("endDate") or raw.get("endDateIso")),
        start_date=_parse_iso(raw.get("startDate") or raw.get("startDateIso")),
        volume_24h=_to_float(raw.get("volume24hr")),
        volume_total=_to_float(raw.get("volume")),
        liquidity=_to_float(raw.get("liquidity")),
        spread=_to_float(raw.get("spread")),
        best_bid=_to_float(raw.get("bestBid")),
        best_ask=_to_float(raw.get("bestAsk")),
        last_trade_price=_to_float(raw.get("lastTradePrice"), default=None) if raw.get("lastTradePrice") is not None else None,
        one_day_change=_to_float(raw.get("oneDayPriceChange"), default=None) if raw.get("oneDayPriceChange") is not None else None,
        closed=bool(raw.get("closed")),
        active=bool(raw.get("active")),
        accepting_orders=bool(raw.get("acceptingOrders")),
        neg_risk=bool(raw.get("negRisk")),
        uma_resolution_status=raw.get("umaResolutionStatus"),
        outcomes=outcomes,
    )


# ---------------------------------------------------------------------------


class PolyClient:
    """Async-friendly wrapper combining CLOB and Gamma APIs."""

    def __init__(self) -> None:
        self._clob: Optional[ClobClient] = None
        self._http: Optional[aiohttp.ClientSession] = None

    # ---- lifecycle -----------------------------------------------------

    async def __aenter__(self) -> "PolyClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def connect(self) -> None:
        timeout = aiohttp.ClientTimeout(total=20)
        # Optional SOCKS5 proxy: when SOCKS5_PROXY is set (e.g. for routing
        # around Polymarket's EU/US geoblock), wrap the aiohttp session
        # in a ProxyConnector. py-clob-client uses ``requests`` separately
        # and picks up HTTPS_PROXY from the environment — that part is
        # configured at the systemd-unit level so the same proxy URL
        # applies to both code paths.
        if SETTINGS.has_socks5_proxy:
            try:
                from aiohttp_socks import ProxyConnector  # type: ignore
            except ImportError as exc:
                raise RuntimeError(
                    "SOCKS5_PROXY is set but aiohttp_socks is not installed. "
                    "Run: pip install aiohttp_socks"
                ) from exc
            # aiohttp_socks rejects the ``socks5h://`` scheme (it's a
            # requests/PySocks convention meaning "resolve DNS at proxy").
            # aiohttp_socks resolves DNS at the proxy by default, so just
            # normalise to ``socks5://``.
            proxy_url = SETTINGS.socks5_proxy.replace("socks5h://", "socks5://", 1)
            connector = ProxyConnector.from_url(proxy_url)
            self._http = aiohttp.ClientSession(timeout=timeout, connector=connector)
            log.info("PolyClient: routing aiohttp through SOCKS5 proxy %s",
                     _redact_proxy_creds(SETTINGS.socks5_proxy))
        else:
            self._http = aiohttp.ClientSession(timeout=timeout)

        if not SETTINGS.has_wallet:
            log.info("No PRIVATE_KEY — running in read-only / scanner mode")
            self._clob = ClobClient(host=CLOB_HOST, chain_id=POLYGON)
            return

        # Authenticated client — derives or creates API creds for L2 endpoints.
        funder = SETTINGS.funder_address or None
        sig_type = SETTINGS.signature_type
        clob = await asyncio.to_thread(
            ClobClient,
            CLOB_HOST,
            POLYGON,
            SETTINGS.private_key,
            None,             # creds (we'll set below)
            sig_type,
            funder,
        )
        creds: ApiCreds = await asyncio.to_thread(clob.create_or_derive_api_creds)
        clob.set_api_creds(creds)
        self._clob = clob
        addr = await asyncio.to_thread(clob.get_address)
        log.info("Authenticated CLOB client: address=%s sig_type=%d", addr, sig_type)

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None

    @property
    def clob(self) -> ClobClient:
        if self._clob is None:
            raise RuntimeError("PolyClient.connect() not awaited")
        return self._clob

    @property
    def http(self) -> aiohttp.ClientSession:
        if self._http is None:
            raise RuntimeError("PolyClient.connect() not awaited")
        return self._http

    # ---- market discovery (Gamma) --------------------------------------

    @with_retries()
    async def _gamma_get(self, path: str, params: Any) -> Any:
        # ``params`` accepts dict (single-value per key) or list of tuples
        # for repeated params (e.g. ?condition_ids=a&condition_ids=b).
        url = f"{GAMMA_HOST}{path}"
        async with self.http.get(url, params=params) as r:
            r.raise_for_status()
            return await r.json()

    async def fetch_event_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        """Fetch one Gamma /events record by its slug. Returns ``None`` if
        not found. Used by the L2 cross-event-correlation detector to
        look up YAML-curated event references.
        """
        try:
            page = await self._gamma_get("/events", {"slug": slug})
        except Exception as exc:  # noqa: BLE001
            log.warning("fetch_event_by_slug(%s) failed: %s", slug, exc)
            return None
        if isinstance(page, list) and page:
            return page[0]
        return None

    async def fetch_markets_by_condition_ids(
        self,
        condition_ids: List[str],
        *,
        closed_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """Resolution-status fetch for a known set of condition_ids.

        Returns raw Gamma /markets dicts (not the normalised ``Market``
        dataclass) — callers like ``PositionRegistry.check_resolutions``
        need fields like ``closed`` and ``outcomePrices`` that the
        normaliser doesn't expose.

        ``closed_only=True`` (default) filters to settled markets only —
        the resolution-detection use case. Gamma's ``/markets`` endpoint
        excludes closed markets when ``condition_ids`` is passed without
        an explicit closed filter, and ``closed=any`` returns 422, so we
        must pick. Pass ``False`` to fetch open + closed (issues two
        round trips per chunk).

        Important: Gamma requires **repeated** ``condition_ids`` params
        (``?condition_ids=a&condition_ids=b``), NOT comma-separated. The
        comma-separated form silently returns ``[]`` for closed markets.
        Empty input returns ``[]``.
        """
        if not condition_ids:
            return []
        out: List[Dict[str, Any]] = []
        # Chunk by IDs not URL length — aiohttp handles long URLs fine,
        # and Gamma seems to accept ~50 repeated params without issue.
        # Use 30 to stay safely within both URL and param-count limits.
        for i in range(0, len(condition_ids), 30):
            chunk = condition_ids[i:i + 30]
            base_params: List[tuple] = [("condition_ids", c) for c in chunk]
            if closed_only:
                params = base_params + [("closed", "true")]
                page = await self._gamma_get("/markets", params)
                if isinstance(page, list):
                    out.extend(page)
            else:
                for closed_val in ("true", "false"):
                    params = base_params + [("closed", closed_val)]
                    page = await self._gamma_get("/markets", params)
                    if isinstance(page, list):
                        out.extend(page)
        return out

    async def fetch_active_markets(
        self,
        *,
        min_volume_24h: float = 0,
        limit_per_page: int = 100,
        max_markets: int = 2000,
    ) -> List[Market]:
        """Page through Gamma's /markets endpoint pulling tradable markets.

        Filters server-side: closed=false, active=true, archived=false, with
        a min volume_24h threshold to skip dust. Sorted by 24h volume desc.
        """
        out: List[Market] = []
        offset = 0
        while len(out) < max_markets:
            params = {
                "closed": "false",
                "active": "true",
                "archived": "false",
                "limit": min(limit_per_page, max_markets - len(out)),
                "offset": offset,
                "order": "volume24hr",
                "ascending": "false",
            }
            page = await self._gamma_get("/markets", params)
            if not isinstance(page, list) or not page:
                break
            for raw in page:
                m = _normalise_market(raw)
                if m is None:
                    continue
                if m.volume_24h < min_volume_24h:
                    # Sorted desc — once we drop below the threshold we can stop.
                    return out
                if not m.is_tradable:
                    continue
                out.append(m)
            if len(page) < limit_per_page:
                break
            offset += limit_per_page
        return out

    # ---- market data (CLOB) --------------------------------------------

    @with_retries()
    async def get_order_book(self, token_id: str) -> Any:
        return await asyncio.to_thread(self.clob.get_order_book, token_id)

    async def get_book_summary(self, token_id: str) -> Dict[str, float]:
        """Compact ``{best_bid, best_ask, mid}`` for the token. Used by
        ExitManager to decide sell-side posting price; we don't need
        the full book depth here.

        Falls back to 0.0 for any field the CLOB doesn't provide.
        """
        book = await self.get_order_book(token_id)
        # py-clob-client returns an OrderBookSummary with .bids / .asks
        # arrays of {price, size} entries, sorted with best at the
        # nearest-to-mid end. Best-bid = highest bid; best-ask = lowest ask.
        def _best(side, picker):
            if not side:
                return 0.0
            try:
                # Could be list of objects or list of dicts depending on SDK version.
                prices = []
                for item in side:
                    p = getattr(item, "price", None)
                    if p is None and isinstance(item, dict):
                        p = item.get("price")
                    if p is not None:
                        prices.append(_to_float(p))
                return picker(prices) if prices else 0.0
            except (TypeError, ValueError):
                return 0.0
        bids = getattr(book, "bids", None) or (book.get("bids") if isinstance(book, dict) else None)
        asks = getattr(book, "asks", None) or (book.get("asks") if isinstance(book, dict) else None)
        best_bid = _best(bids, max)
        best_ask = _best(asks, min)
        mid = (best_bid + best_ask) / 2.0 if (best_bid > 0 and best_ask > 0) else 0.0
        return {"best_bid": best_bid, "best_ask": best_ask, "mid": mid}

    @with_retries()
    async def get_midpoint(self, token_id: str) -> float:
        res = await asyncio.to_thread(self.clob.get_midpoint, token_id)
        # Returns {"mid": "0.123"}
        if isinstance(res, dict):
            return _to_float(res.get("mid"))
        return _to_float(res)

    async def get_order_status(self, order_id: str) -> Optional[Dict[str, Any]]:
        """Look up a single order's current status. Returns the SDK dict
        (with ``status``, ``size_matched``, etc.) or ``None`` for not
        found — Polymarket removes terminal/matched orders from the
        active book, which OrderTracker interprets as a fill.

        Used by OrderTracker.reconcile.
        """
        if not SETTINGS.has_wallet:
            return None
        try:
            res = await asyncio.to_thread(self.clob.get_order, order_id)
        except Exception as exc:  # noqa: BLE001
            # Polymarket returns 404 for unknown / matched orders. The SDK
            # raises (or returns None depending on version); treat both as "gone".
            msg = str(exc).lower()
            if "404" in msg or "not found" in msg:
                return None
            raise
        return res if isinstance(res, dict) else None

    # ---- account -------------------------------------------------------

    @with_retries()
    async def get_usdc_balance(self) -> float:
        if not SETTINGS.has_wallet:
            return 0.0
        from py_clob_client.clob_types import AssetType
        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=SETTINGS.signature_type,
        )
        res = await asyncio.to_thread(self.clob.get_balance_allowance, params)
        # Polymarket returns USDC in 6-decimal units as a string.
        balance_raw = _to_float(res.get("balance") if isinstance(res, dict) else res)
        return balance_raw / 1_000_000.0

    @with_retries()
    async def get_open_orders(self) -> List[Dict[str, Any]]:
        if not SETTINGS.has_wallet:
            return []
        return await asyncio.to_thread(self.clob.get_orders) or []

    # ---- trading -------------------------------------------------------

    async def place_sell_limit(
        self,
        token_id: str,
        price: float,
        size_shares: float,
        order_type: str = "GTC",
    ) -> Optional[Dict[str, Any]]:
        """Place a sell limit order sized in SHARES (we sell N tokens at price P).

        Mirrors ``place_buy_limit`` but for the exit side. Used by
        ``ExitManager.tick`` to post the matching sell after an
        mm_spread buy fills. Sizing in shares (not USD) because we're
        selling exactly the inventory we accumulated — the dollar value
        of the sell depends on the fill price, which can move.
        """
        if SETTINGS.dry_run:
            return {
                "dry_run": True, "side": "SELL",
                "token_id": token_id, "price": price, "size_shares": size_shares,
            }
        if not SETTINGS.has_wallet:
            raise RuntimeError("place_sell_limit called without PRIVATE_KEY")

        if price <= 0 or price >= 1:
            raise ValueError(f"price {price} out of bounds for limit sell")
        shares = round(size_shares, 2)
        args = OrderArgs(
            token_id=token_id,
            price=price,
            size=shares,
            side="SELL",
        )
        ot = getattr(OrderType, order_type, OrderType.GTC)
        signed = await asyncio.to_thread(self.clob.create_order, args)
        resp = await asyncio.to_thread(self.clob.post_order, signed, ot)
        return resp

    async def place_buy_limit(
        self,
        token_id: str,
        price: float,
        size_usd: float,
        order_type: str = "GTC",
    ) -> Optional[Dict[str, Any]]:
        """Place a buy limit order sized in USDC (size_usd / price = shares)."""
        if SETTINGS.dry_run:
            return {
                "dry_run": True,
                "token_id": token_id,
                "price": price,
                "size_usd": size_usd,
                "shares": round(size_usd / price, 4) if price > 0 else 0.0,
            }
        if not SETTINGS.has_wallet:
            raise RuntimeError("place_buy_limit called without PRIVATE_KEY")

        if price <= 0:
            raise ValueError("price must be > 0")
        shares = round(size_usd / price, 2)
        args = OrderArgs(
            token_id=token_id,
            price=price,
            size=shares,
            side="BUY",
        )
        ot = getattr(OrderType, order_type, OrderType.GTC)
        signed = await asyncio.to_thread(self.clob.create_order, args)
        resp = await asyncio.to_thread(self.clob.post_order, signed, ot)
        return resp
