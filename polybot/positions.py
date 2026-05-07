"""Open-position tracking with automatic close on UMA resolution.

Filling the gap in the bot's lifecycle: ``main.run_tick`` knew how to
place buys but never closed anything. Pre-2026-05-04, ``risk.record_close``
was defined but uncalled, so any live position would accumulate forever
with no realized PnL.

Design:

* ``Position`` is a dataclass per filled buy. Carries everything we need
  to compute realized PnL when the market resolves (entry price, shares,
  outcome index for parsing ``outcomePrices``).
* ``PositionRegistry`` owns the live state. Persistence is JSON at
  ``logs/positions.json`` with two arrays — ``open`` and ``closed`` — so
  closed positions are an immutable audit trail. ``RiskManager`` consults
  the registry for open exposure (replaces the old in-memory
  ``open_trades`` dict).
* Resolution check uses Gamma ``/markets?condition_ids=...`` in batch.
  ``closed: true`` plus ``outcomePrices`` is enough to compute the
  terminal payout — we don't need to talk to UMA directly. Polymarket's
  redemption flow (resolved tokens auto-redeem to USDC) is what
  actually settles balances; the registry just bookkeeps the realized
  PnL number.

Not in scope here (separate features):
* MM-spread exit posting (after a buy fill on a wide-spread market,
  posting the matching sell at ask). Requires order-book interaction
  beyond simple resolution polling.
* Stop-loss on adverse move. Hold-to-resolution strategies don't
  benefit from stop-loss; they just lock in losses early.
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
from .models import Candidate

log = get_logger(__name__)

POSITIONS_FILE = PROJECT_ROOT / "logs" / "positions.json"


@dataclass
class Position:
    token_id: str
    condition_id: str
    slug: str
    outcome_name: str
    outcome_index: int        # 0 or 1 — index into ``outcomePrices`` at resolution
    entry_price: float
    entry_size_usd: float
    shares: float             # entry_size_usd / entry_price
    entry_ts: str             # ISO-8601 UTC
    detector: str
    status: str = "open"      # "open" | "resolved_won" | "resolved_lost" | "closed"
    # Set on close:
    closed_ts: Optional[str] = None
    terminal_price: Optional[float] = None
    realized_pnl: Optional[float] = None

    @property
    def is_open(self) -> bool:
        return self.status == "open"


@dataclass
class _State:
    open: List[Position] = field(default_factory=list)
    closed: List[Position] = field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_jsonable(p: Position) -> Dict[str, Any]:
    return asdict(p)


def _from_jsonable(d: Dict[str, Any]) -> Position:
    return Position(**d)


class PositionRegistry:
    """Disk-backed registry of bot positions.

    Thread-unsafe: assumes a single asyncio loop. Persistence is
    write-on-mutation so a crashed process doesn't lose state.
    """

    def __init__(self, path: Path = POSITIONS_FILE) -> None:
        self.path = path
        self._state = _State()
        self._load()

    # ---- persistence ---------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            blob = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("positions.json load failed (%s); starting empty", exc)
            return
        self._state.open = [_from_jsonable(d) for d in blob.get("open", [])]
        self._state.closed = [_from_jsonable(d) for d in blob.get("closed", [])]
        log.info("PositionRegistry loaded: %d open, %d closed",
                 len(self._state.open), len(self._state.closed))

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            blob = {
                "open": [_to_jsonable(p) for p in self._state.open],
                "closed": [_to_jsonable(p) for p in self._state.closed],
            }
            self.path.write_text(json.dumps(blob, indent=2))
        except OSError as exc:
            log.warning("positions.json save failed: %s", exc)

    # ---- mutations -----------------------------------------------------

    def open_from_fill(
        self,
        *,
        token_id: str,
        condition_id: str,
        slug: str,
        outcome_name: str,
        outcome_index: int,
        entry_price: float,
        entry_size_usd: float,
        detector: str,
    ) -> Position:
        """Register a fill from an OrderTracker FillEvent. Used by main.run_tick
        when a buy order transitions PENDING → FILLED.

        Distinct from ``open_position`` which takes a full Candidate — by
        the time a fill is detected, the Candidate's Market context is
        stale (orderbook moved), and we already captured what we need
        in ``TrackedOrder.intent_payload``.
        """
        if any(p.token_id == token_id for p in self._state.open):
            raise ValueError(f"already have open position on token {token_id[:12]}...")
        if entry_price <= 0:
            raise ValueError(f"invalid entry_price {entry_price}")
        pos = Position(
            token_id=token_id,
            condition_id=condition_id,
            slug=slug,
            outcome_name=outcome_name,
            outcome_index=outcome_index,
            entry_price=entry_price,
            entry_size_usd=entry_size_usd,
            shares=entry_size_usd / entry_price,
            entry_ts=_now_iso(),
            detector=detector,
        )
        self._state.open.append(pos)
        self._save()
        log.info(
            "Position opened (from fill): %s %s @ %.3f size=$%.2f shares=%.2f (%s)",
            slug, outcome_name, entry_price, entry_size_usd,
            pos.shares, detector,
        )
        return pos

    def open_position(self, c: Candidate, size_usd: float) -> Position:
        """Register a new fill. Caller has already placed and confirmed the order."""
        if any(p.token_id == c.outcome.token_id for p in self._state.open):
            # Defence in depth: gate() should have rejected this earlier.
            raise ValueError(
                f"already have open position on token {c.outcome.token_id[:12]}..."
            )
        # Find the index of the bought outcome among the market's outcomes —
        # we need this to parse outcomePrices at resolution. Default to 0
        # if for some reason it's not in the list (shouldn't happen).
        outcome_index = 0
        for i, o in enumerate(c.market.outcomes):
            if o.token_id == c.outcome.token_id:
                outcome_index = i
                break
        if c.entry_price <= 0:
            raise ValueError(f"invalid entry_price {c.entry_price}")
        pos = Position(
            token_id=c.outcome.token_id,
            condition_id=c.market.condition_id,
            slug=c.market.slug,
            outcome_name=c.outcome.name,
            outcome_index=outcome_index,
            entry_price=c.entry_price,
            entry_size_usd=size_usd,
            shares=size_usd / c.entry_price,
            entry_ts=_now_iso(),
            detector=c.detector,
        )
        self._state.open.append(pos)
        self._save()
        log.info(
            "Position opened: %s %s @ %.3f size=$%.2f shares=%.2f (%s)",
            c.market.slug, c.outcome.name, c.entry_price, size_usd,
            pos.shares, c.detector,
        )
        return pos

    def close_position(self, token_id: str, terminal_price: float) -> Optional[Position]:
        """Move a position from ``open`` → ``closed`` and compute PnL.

        Returns the closed Position (or None if not found in open list).
        ``terminal_price`` should be 0.0 or 1.0 from the resolved market's
        ``outcomePrices`` at our outcome_index — fractional values still
        compute PnL correctly (e.g. cancellation/refund cases).
        """
        for i, p in enumerate(self._state.open):
            if p.token_id != token_id:
                continue
            payout = p.shares * terminal_price
            pnl = payout - p.entry_size_usd
            p.closed_ts = _now_iso()
            p.terminal_price = terminal_price
            p.realized_pnl = pnl
            p.status = "resolved_won" if terminal_price >= 0.5 else "resolved_lost"
            self._state.open.pop(i)
            self._state.closed.append(p)
            self._save()
            log.info(
                "Position closed: %s %s entry=%.3f → terminal=%.3f shares=%.2f "
                "size=$%.2f payout=$%.2f pnl=$%+.2f",
                p.slug, p.outcome_name, p.entry_price, terminal_price,
                p.shares, p.entry_size_usd, payout, pnl,
            )
            return p
        return None

    # ---- queries -------------------------------------------------------

    @property
    def open_positions(self) -> List[Position]:
        return list(self._state.open)

    @property
    def closed_positions(self) -> List[Position]:
        return list(self._state.closed)

    def has_open(self, token_id: str) -> bool:
        return any(p.token_id == token_id for p in self._state.open)

    @property
    def open_exposure_usd(self) -> float:
        return sum(p.entry_size_usd for p in self._state.open)

    def summary(self) -> Dict[str, Any]:
        n_open = len(self._state.open)
        n_closed = len(self._state.closed)
        realized = sum((p.realized_pnl or 0.0) for p in self._state.closed)
        wins = sum(1 for p in self._state.closed if p.status == "resolved_won")
        return {
            "n_open": n_open,
            "n_closed": n_closed,
            "open_exposure_usd": self.open_exposure_usd,
            "realized_pnl": realized,
            "wins": wins,
            "losses": n_closed - wins,
        }

    # ---- resolution checking -------------------------------------------

    async def check_resolutions(
        self,
        fetch_by_cond: Callable[[List[str]], Awaitable[List[Dict[str, Any]]]],
        on_close: Optional[Callable[[Position], None]] = None,
    ) -> List[Position]:
        """Poll Gamma for resolution status of every open position.

        ``fetch_by_cond`` is an async callable that takes a list of
        condition_ids and returns the raw Gamma /markets dicts —
        injected so this module is testable without a live client.
        Production calls into ``PolyClient.fetch_markets_by_condition_ids``.

        For each market with ``closed: true`` and parseable
        ``outcomePrices``, calls ``close_position`` plus the optional
        ``on_close`` callback (used to push realized_pnl into RiskManager).
        Returns the list of newly-closed positions.
        """
        if not self._state.open:
            return []

        cond_ids = sorted({p.condition_id for p in self._state.open})
        try:
            raw_markets = await fetch_by_cond(cond_ids)
        except Exception as exc:  # noqa: BLE001
            log.warning("Gamma resolution fetch failed: %s", exc)
            return []

        market_by_cond: Dict[str, Dict[str, Any]] = {}
        for raw in raw_markets:
            cid = raw.get("conditionId")
            if cid:
                market_by_cond[cid] = raw

        newly_closed: List[Position] = []
        now_dt = datetime.now(timezone.utc)
        for pos in list(self._state.open):
            raw = market_by_cond.get(pos.condition_id)
            if raw is None:
                continue
            prices_raw = raw.get("outcomePrices") or "[]"
            try:
                prices = json.loads(prices_raw)
                terminal = float(prices[pos.outcome_index])
            except (json.JSONDecodeError, IndexError, ValueError, TypeError) as exc:
                log.warning(
                    "Position %s: outcomePrices unparseable (%s); skipping",
                    pos.slug, exc,
                )
                continue

            is_closed = bool(raw.get("closed"))
            soft_resolved = False
            if not is_closed:
                # Polymarket sometimes leaves markets ``closed: false`` for
                # days after the underlying event has effectively resolved
                # (UMA settlement lag). Two-tier detection:
                #
                # tier 1 — ULTRA-extreme (≥0.999 or ≤0.001): close
                #   immediately regardless of endDate. At this tightness the
                #   market is essentially settled; example caught:
                #   wta-starodu-waltert sub-market at 0.9995 after the
                #   tournament match resolved, while endDate was set to
                #   tournament-end (a week away).
                #
                # tier 2 — extreme (≥0.995 or ≤0.005): close only if
                #   endDate is at least 24h past. Avoids false-positives
                #   where a market temporarily spikes near 1.0 mid-trade.
                end_str = raw.get("endDate") or raw.get("endDateIso") or ""
                end_passed = False
                if end_str:
                    try:
                        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                        end_passed = (now_dt - end_dt).total_seconds() > 86400
                    except (ValueError, TypeError):
                        end_passed = False
                price_floats = [float(x) for x in prices]
                ultra_extreme = any(p >= 0.999 or p <= 0.001 for p in price_floats)
                extreme = any(p >= 0.995 or p <= 0.005 for p in price_floats)
                soft_resolved = ultra_extreme or (extreme and end_passed)
                if soft_resolved:
                    tier = "ULTRA" if ultra_extreme else "moderate+endpast"
                    log.info(
                        "Position %s: soft-resolved (%s) prices=%s endDate=%s",
                        pos.slug, tier, prices_raw, end_str,
                    )

            if not (is_closed or soft_resolved):
                continue

            closed = self.close_position(pos.token_id, terminal)
            if closed is not None:
                newly_closed.append(closed)
                if on_close is not None:
                    try:
                        on_close(closed)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("on_close callback raised: %s", exc)
        return newly_closed
