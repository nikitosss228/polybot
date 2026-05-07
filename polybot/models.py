"""Lightweight value objects for market and candidate data."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


@dataclass
class Outcome:
    name: str          # "Yes" / "No" / candidate name
    token_id: str      # ERC1155 conditional token id (decimal string)
    price: float       # current best ask price (probability-equivalent in [0,1])


@dataclass
class Market:
    """Normalised view of a Polymarket market combining CLOB + Gamma data."""

    condition_id: str
    question: str
    slug: str
    end_date: Optional[datetime]
    start_date: Optional[datetime]
    volume_24h: float
    volume_total: float
    liquidity: float
    spread: float                 # decimal (e.g. 0.04 = 4¢)
    best_bid: float
    best_ask: float
    last_trade_price: Optional[float]
    one_day_change: Optional[float]
    closed: bool
    active: bool
    accepting_orders: bool
    neg_risk: bool
    uma_resolution_status: Optional[str]
    outcomes: List[Outcome] = field(default_factory=list)

    # ---- helpers --------------------------------------------------------

    @property
    def is_tradable(self) -> bool:
        return self.active and not self.closed and self.accepting_orders

    @property
    def days_to_end(self) -> Optional[float]:
        if self.end_date is None:
            return None
        now = datetime.now(timezone.utc)
        return (self.end_date - now).total_seconds() / 86400.0

    def outcome_by_name(self, name: str) -> Optional[Outcome]:
        for o in self.outcomes:
            if o.name.lower() == name.lower():
                return o
        return None


@dataclass
class Candidate:
    """Result of an edge detector — a potential trade with rationale."""

    market: Market
    outcome: Outcome     # which side to buy
    side: str            # always "BUY" for now (we don't sell short on Polymarket)
    entry_price: float   # max price we'd pay
    edge_pct: float      # (1 - entry_price) / entry_price * 100, expected return if YES
    reason: str          # short rationale string for logs
    confidence: float    # 0..1 — detector's self-rated confidence
    detector: str = ""   # which detector surfaced this — for analytics/CSV slicing
    # --- bundle arb only: second leg (NO side) we'd buy alongside `outcome` ---
    pair_outcome: Optional[Outcome] = None
    pair_price: float = 0.0
    # Pre-fee edge_pct minus an estimated gas/fee impact, in percentage points.
    # Detectors can set this directly; otherwise risk.gate falls back to edge_pct.
    edge_pct_net: Optional[float] = None

    @property
    def score(self) -> float:
        """Used to rank candidates when multiple pass the gate."""
        return self.edge_pct * self.confidence
