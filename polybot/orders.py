"""Order lifecycle tracker — bridges 'order posted' to 'order filled'.

Closes a pre-2026-05-04 bug in ``main.run_tick``: after ``place_buy_limit``
returned successfully, the code immediately called ``registry.open_position``.
But ``place_buy_limit`` posts a **GTC limit** that may sit on the book for
hours or never fill. Treating POST as FILL meant any never-filled limit
would create a phantom position that the resolution sweep would later try
to close — confusing real PnL with paper noise.

This module fixes that by introducing an explicit
PENDING → FILLED|CANCELLED|PARTIALLY_FILLED pipeline:

* ``OrderTracker.register_pending(order)`` — caller records intent after a
  successful POST. Persisted immediately so a crashed bot recovers state.
* ``OrderTracker.reconcile(client)`` — async sweep that asks the CLOB
  about each tracked order and emits ``FillEvent`` objects for state
  transitions. Caller (main.run_tick) reacts to fills (open / close
  positions) and to cancellations (cleanup).

Polymarket order states observed in the SDK: ``LIVE``, ``MATCHED``,
``CANCELED``, ``DELAYED``, ``UNMATCHED``. We collapse these to four
internal states for simplicity. Partial fills are detected by the
``size_matched`` vs ``size`` fields when present.

Disabled in DRY_RUN — ``main`` constructs a tracker but never calls
``register_pending`` since the dry-run path doesn't post real orders.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .config import PROJECT_ROOT
from .logger import get_logger

log = get_logger(__name__)

ORDERS_FILE = PROJECT_ROOT / "logs" / "orders.json"


class OrderState(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"


# Polymarket SDK statuses → OrderState. CLOB uses uppercase strings.
# DELAYED orders are fraud-screened; treat as pending. UNMATCHED is the
# terminal state for expired-without-match orders — surface as cancelled
# so callers don't keep waiting on it.
_POLY_STATUS_MAP = {
    "LIVE": OrderState.PENDING,
    "DELAYED": OrderState.PENDING,
    "MATCHED": OrderState.FILLED,
    "CANCELED": OrderState.CANCELLED,
    "CANCELLED": OrderState.CANCELLED,
    "UNMATCHED": OrderState.CANCELLED,
}


@dataclass
class TrackedOrder:
    """One posted order we're watching for fill events.

    For a ``BUY``: ``intent_payload`` carries the original Candidate's
    fields needed to construct a Position on fill (slug, condition_id,
    detector, outcome_name, outcome_index, entry_price, size_usd).

    For a ``SELL``: ``parent_token_id`` points at the open Position whose
    sell-side this order is exiting. ``intent_payload`` is empty.
    """
    order_id: str
    side: str                           # "BUY" or "SELL"
    token_id: str
    price: float                        # posted price
    size: float                         # posted size in shares
    size_usd: float                     # posted notional ($)
    posted_ts: str                      # ISO-8601 UTC
    state: OrderState = OrderState.PENDING
    intent_payload: Dict[str, Any] = field(default_factory=dict)
    parent_token_id: Optional[str] = None
    last_seen_ts: Optional[str] = None  # ISO of last reconcile that observed this order
    size_matched: float = 0.0           # shares matched so far (partial fills)
    final_status: Optional[str] = None  # raw SDK status string when terminal

    @property
    def is_terminal(self) -> bool:
        return self.state in (OrderState.FILLED, OrderState.CANCELLED)


@dataclass
class FillEvent:
    """Emitted by reconcile when a tracked order changes state.

    ``new_state`` is one of FILLED / PARTIALLY_FILLED / CANCELLED.
    ``order`` is the up-to-date TrackedOrder. ``previous_state`` is the
    state we had before this reconcile so callers can detect e.g.
    PARTIALLY_FILLED → FILLED transitions.
    """
    order: TrackedOrder
    previous_state: OrderState
    new_state: OrderState


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class OrderTracker:
    def __init__(self, path: Path = ORDERS_FILE) -> None:
        self.path = path
        # Active = not in terminal state. Closed = filled/cancelled audit trail.
        self._active: Dict[str, TrackedOrder] = {}
        self._closed: List[TrackedOrder] = []
        self._load()

    # ---- persistence ---------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            blob = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("orders.json load failed (%s); starting empty", exc)
            return
        for d in blob.get("active", []):
            try:
                o = self._from_dict(d)
                self._active[o.order_id] = o
            except (TypeError, ValueError) as exc:
                log.warning("orders.json: skipping malformed active row (%s)", exc)
        for d in blob.get("closed", []):
            try:
                self._closed.append(self._from_dict(d))
            except (TypeError, ValueError) as exc:
                log.warning("orders.json: skipping malformed closed row (%s)", exc)
        log.info("OrderTracker loaded: %d active, %d closed",
                 len(self._active), len(self._closed))

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            blob = {
                "active": [self._to_dict(o) for o in self._active.values()],
                "closed": [self._to_dict(o) for o in self._closed],
            }
            self.path.write_text(json.dumps(blob, indent=2))
        except OSError as exc:
            log.warning("orders.json save failed: %s", exc)

    @staticmethod
    def _to_dict(o: TrackedOrder) -> Dict[str, Any]:
        d = asdict(o)
        d["state"] = o.state.value
        return d

    @staticmethod
    def _from_dict(d: Dict[str, Any]) -> TrackedOrder:
        d = dict(d)
        d["state"] = OrderState(d["state"])
        return TrackedOrder(**d)

    # ---- mutations -----------------------------------------------------

    def register_pending(self, order: TrackedOrder) -> None:
        if order.order_id in self._active:
            raise ValueError(f"order_id {order.order_id} already tracked")
        order.state = OrderState.PENDING
        self._active[order.order_id] = order
        self._save()
        log.info(
            "Order tracked: %s %s %s @ %.4f size=%.4f shares ($%.2f) on %s",
            order.order_id[:12], order.side, "PENDING",
            order.price, order.size, order.size_usd, order.token_id[:12],
        )

    def _retire(self, order: TrackedOrder, new_state: OrderState,
                final_status: Optional[str] = None) -> None:
        order.state = new_state
        order.final_status = final_status
        order.last_seen_ts = _now_iso()
        self._active.pop(order.order_id, None)
        self._closed.append(order)

    # ---- queries -------------------------------------------------------

    @property
    def active(self) -> List[TrackedOrder]:
        return list(self._active.values())

    @property
    def closed(self) -> List[TrackedOrder]:
        return list(self._closed)

    def get(self, order_id: str) -> Optional[TrackedOrder]:
        if order_id in self._active:
            return self._active[order_id]
        for o in self._closed:
            if o.order_id == order_id:
                return o
        return None

    def has_pending_buy(self, token_id: str) -> bool:
        return any(
            o.side == "BUY" and o.token_id == token_id and o.state == OrderState.PENDING
            for o in self._active.values()
        )

    # ---- reconcile -----------------------------------------------------

    async def reconcile(
        self,
        get_order_status: Callable[[str], Awaitable[Optional[Dict[str, Any]]]],
    ) -> List[FillEvent]:
        """Poll the CLOB for the status of each pending order; return the
        list of state-transition events.

        ``get_order_status`` is an async callable that takes an order_id
        and returns the raw SDK status dict (or ``None`` for not-found,
        which means the order is finalized — typically filled — and has
        rolled out of the active orderbook).

        We use a per-order callable rather than a bulk ``get_orders()``
        so testability and clarity stay reasonable. The bot's tick rate
        is 600s and active-order count is bounded by exposure caps, so
        sequential per-order polling is fine.
        """
        events: List[FillEvent] = []
        if not self._active:
            return events
        for order in list(self._active.values()):
            try:
                status = await get_order_status(order.order_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("reconcile: get_order_status(%s) failed: %s",
                            order.order_id[:12], exc)
                continue
            transition = self._apply_status(order, status)
            if transition is not None:
                events.append(transition)
        if events:
            self._save()
        return events

    def _apply_status(
        self,
        order: TrackedOrder,
        status: Optional[Dict[str, Any]],
    ) -> Optional[FillEvent]:
        """Apply a single order's status update; return event if state changed.

        ``status`` is the SDK's get_order response (dict with ``status``,
        ``size_matched``, etc.) or ``None`` when the order is no longer
        on the book — Polymarket returns 404 for fully-matched orders
        once they've been removed, which is effectively a fill.
        """
        prev = order.state
        order.last_seen_ts = _now_iso()

        if status is None:
            # Order no longer reachable on the CLOB. Conservative
            # interpretation: it filled. (CLOB removes terminal orders.)
            self._retire(order, OrderState.FILLED, final_status="not_found")
            return FillEvent(order, prev, OrderState.FILLED)

        raw = str(status.get("status") or "").upper()
        new_state = _POLY_STATUS_MAP.get(raw)
        size_matched = _to_float(status.get("size_matched"))
        order.size_matched = size_matched

        if new_state is None:
            # Unknown status — log and keep waiting.
            log.warning("reconcile: unknown order status %r for %s",
                        raw, order.order_id[:12])
            return None

        # Detect partial-fill while still LIVE.
        if new_state == OrderState.PENDING and size_matched > 0 \
                and size_matched < order.size:
            order.state = OrderState.PARTIALLY_FILLED
            return FillEvent(order, prev, OrderState.PARTIALLY_FILLED)

        if new_state == prev:
            return None

        if new_state in (OrderState.FILLED, OrderState.CANCELLED):
            self._retire(order, new_state, final_status=raw)
            return FillEvent(order, prev, new_state)

        order.state = new_state
        return FillEvent(order, prev, new_state)


def _to_float(v: Any, default: float = 0.0) -> float:
    if v in (None, ""):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
