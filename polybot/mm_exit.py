"""Market-making exit-side scheduler for mm_spread positions.

The mm_spread detector flags wide-bid-ask markets and posts a buy at
``best_bid``. To realize the spread we need to also post a sell at
``entry_price + spread_target`` once the buy fills. Without this layer
the mm_spread strategy is half-built — it captures inventory at the
bid but never exits, so any unfilled sell turns into a directional
buy-and-hold, which is **not** what mm_spread's PnL model assumes
(``size * edge_pct/100`` requires a round-trip).

This module:
* Maintains a per-position scheduled-sell record.
* On each tick: for every position awaiting a sell-post, fetches the
  current order book, decides a target sell price, posts the sell, and
  hands it to the OrderTracker.
* Reacts to OrderTracker fill events for sells: closes the position via
  PositionRegistry with the actual sell price.

Design choices:
* **Sell target**: ``min(best_ask - 1¢, entry_price + spread_capture)``
  where spread_capture defaults to 80% of the original spread observed
  at buy-time. This is conservative — leaves 20% of the spread as
  margin to clear faster than the very best ask.
* **No re-pricing**: if the market moves away from our target before
  the sell fills, we let the order sit. Re-pricing is a follow-up
  feature; for v1 we want to MEASURE fill rate, not optimize against it.
* **Stale-cancel**: not implemented in v1. If a sell sits unfilled for
  hours and the market moves substantially, it'll just sit. The
  scheduler can be extended later with a TTL.

Disabled in DRY_RUN — the scheduler does nothing if there are no real
positions tagged ``mm_spread`` in the registry, which is true in
dry-run since ``main`` doesn't register positions on dry-run order
"placements".
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .config import PROJECT_ROOT
from .logger import get_logger
from .orders import OrderTracker, TrackedOrder

log = get_logger(__name__)

EXIT_STATE_FILE = PROJECT_ROOT / "logs" / "mm_exit_state.json"
SPREAD_CAPTURE_FRAC = 0.80  # of the at-buy spread; leaves 20% margin.
PRICE_IMPROVEMENT = 0.01    # never post AT the ask; always 1¢ inside.


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ScheduledSell:
    """A sell-side intent for an mm_spread position.

    States:
      ``pending_post``  — waiting for the next tick to post the sell
                          (we don't post inside open_position because
                          the orderbook may have moved).
      ``posted``        — sell order live; OrderTracker watches it.
      ``done``          — sell filled OR cancelled; position closed.
    """
    token_id: str                       # the YES/NO token we hold
    condition_id: str
    slug: str
    shares: float                       # what we bought
    entry_price: float                  # what we paid
    spread_at_buy: float                # original spread when buy posted
    state: str = "pending_post"
    created_ts: str = field(default_factory=_now_iso)
    sell_order_id: Optional[str] = None
    sell_target_price: Optional[float] = None
    posted_ts: Optional[str] = None
    closed_ts: Optional[str] = None
    realized_sell_price: Optional[float] = None


class ExitManager:
    def __init__(self, path: Path = EXIT_STATE_FILE) -> None:
        self.path = path
        self._scheduled: Dict[str, ScheduledSell] = {}
        self._load()

    # ---- persistence ---------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            blob = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("mm_exit_state.json load failed (%s); starting empty", exc)
            return
        for d in blob.get("scheduled", []):
            try:
                s = ScheduledSell(**d)
                self._scheduled[s.token_id] = s
            except (TypeError, ValueError) as exc:
                log.warning("mm_exit_state.json: skipping malformed row (%s)", exc)
        log.info("ExitManager loaded: %d scheduled sells", len(self._scheduled))

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            blob = {"scheduled": [asdict(s) for s in self._scheduled.values()]}
            self.path.write_text(json.dumps(blob, indent=2))
        except OSError as exc:
            log.warning("mm_exit_state.json save failed: %s", exc)

    # ---- mutations -----------------------------------------------------

    def schedule_sell(
        self,
        token_id: str,
        condition_id: str,
        slug: str,
        shares: float,
        entry_price: float,
        spread_at_buy: float,
    ) -> ScheduledSell:
        """Called when an mm_spread BUY fills. Records intent to post sell
        at the next ``tick``.
        """
        if token_id in self._scheduled:
            log.warning("schedule_sell: %s already scheduled, replacing",
                        token_id[:12])
        s = ScheduledSell(
            token_id=token_id,
            condition_id=condition_id,
            slug=slug,
            shares=shares,
            entry_price=entry_price,
            spread_at_buy=spread_at_buy,
        )
        self._scheduled[token_id] = s
        self._save()
        log.info(
            "Sell scheduled: %s shares=%.2f entry=%.4f spread_at_buy=%.4f → "
            "target~%.4f (%.0f%% spread capture)",
            slug, shares, entry_price, spread_at_buy,
            entry_price + SPREAD_CAPTURE_FRAC * spread_at_buy,
            SPREAD_CAPTURE_FRAC * 100,
        )
        return s

    def mark_posted(self, token_id: str, order_id: str, target_price: float) -> None:
        s = self._scheduled.get(token_id)
        if s is None:
            log.warning("mark_posted: no scheduled sell for %s", token_id[:12])
            return
        s.state = "posted"
        s.sell_order_id = order_id
        s.sell_target_price = target_price
        s.posted_ts = _now_iso()
        self._save()

    def mark_done(self, token_id: str, realized_sell_price: float) -> None:
        s = self._scheduled.get(token_id)
        if s is None:
            return
        s.state = "done"
        s.realized_sell_price = realized_sell_price
        s.closed_ts = _now_iso()
        self._save()
        log.info(
            "Sell done: %s realized_sell=%.4f vs entry=%.4f shares=%.2f gross=%+.4f",
            s.slug, realized_sell_price, s.entry_price, s.shares,
            (realized_sell_price - s.entry_price) * s.shares,
        )

    # ---- queries -------------------------------------------------------

    def get(self, token_id: str) -> Optional[ScheduledSell]:
        return self._scheduled.get(token_id)

    @property
    def pending_post(self) -> List[ScheduledSell]:
        return [s for s in self._scheduled.values() if s.state == "pending_post"]

    @property
    def posted(self) -> List[ScheduledSell]:
        return [s for s in self._scheduled.values() if s.state == "posted"]

    # ---- per-tick action -----------------------------------------------

    async def tick(
        self,
        post_sell: Callable[[str, float, float], Awaitable[Dict[str, Any]]],
        get_orderbook: Callable[[str], Awaitable[Dict[str, Any]]],
        tracker: OrderTracker,
    ) -> List[ScheduledSell]:
        """For every ``pending_post`` sell, fetch the current ask and post.

        ``post_sell(token_id, price, size_shares)`` is an async callable
        that places the sell order and returns the response dict
        (must contain ``order_id``). ``get_orderbook(token_id)`` returns
        a dict with at least ``best_ask`` (float).

        Posts the sell at ``min(best_ask - 1¢, entry_price + 80% × original spread)``.
        Registers the new order with the tracker so reconcile() will
        notice when it fills. Returns the list of newly-posted scheduled
        sells.
        """
        out: List[ScheduledSell] = []
        for s in self.pending_post:
            try:
                book = await get_orderbook(s.token_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("ExitManager.tick: get_orderbook(%s) failed: %s",
                            s.token_id[:12], exc)
                continue
            best_ask = float(book.get("best_ask") or 0.0)
            target = s.entry_price + SPREAD_CAPTURE_FRAC * s.spread_at_buy
            if best_ask > 0:
                target = min(target, best_ask - PRICE_IMPROVEMENT)
            target = max(s.entry_price + 0.005, target)  # never sell below entry+0.5¢
            target = round(min(target, 0.999), 4)        # CLOB price bounds

            try:
                resp = await post_sell(s.token_id, target, s.shares)
            except Exception as exc:  # noqa: BLE001
                log.warning("ExitManager.tick: post_sell(%s) failed: %s",
                            s.token_id[:12], exc)
                continue
            order_id = resp.get("order_id") or resp.get("orderID") or resp.get("id")
            if not order_id:
                log.error("ExitManager.tick: post_sell returned no order_id: %s", resp)
                continue

            tracker.register_pending(TrackedOrder(
                order_id=str(order_id),
                side="SELL",
                token_id=s.token_id,
                price=target,
                size=s.shares,
                size_usd=s.shares * target,
                posted_ts=_now_iso(),
                parent_token_id=s.token_id,
            ))
            self.mark_posted(s.token_id, str(order_id), target)
            out.append(s)
        return out
