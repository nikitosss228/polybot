"""Edge detectors that turn live markets into trade candidates.

Three detectors run over every fetched market. Any one of them can produce
a Candidate; they are not mutually exclusive (the same market may surface
from two detectors with different rationales — they will be deduped by the
caller).

The detectors are intentionally cheap and conservative. The bot is sized
for a $100 budget — the loss of a single bad trade hurts, so we'd rather
miss edge than create false positives.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, List, Optional

from .config import SETTINGS
from .logger import get_logger
from .models import Candidate, Market, Outcome

log = get_logger(__name__)


def _edge_pct(price: float) -> float:
    """% return if YES, given we bought at `price` and contract resolves to 1."""
    if price <= 0:
        return 0.0
    return (1.0 - price) / price * 100.0


CONSENSUS_THRESHOLD = 0.70  # winning side must be at least this clearly favored


def _consensus_outcome(market: Market) -> Optional[Outcome]:
    """Pick the outcome with the highest implied probability **only if** it
    clears CONSENSUS_THRESHOLD. We refuse to "trade the favorite" of a
    coin-flip market at 0.51 — there's no edge, that's just gambling.
    """
    candidates = [
        o for o in market.outcomes
        if SETTINGS.min_price <= o.price <= SETTINGS.max_price
    ]
    if not candidates:
        return None
    top = max(candidates, key=lambda o: o.price)
    if top.price < CONSENSUS_THRESHOLD:
        return None
    return top


# ---------------------------------------------------------------------------
# Detector A — Date-expired
# ---------------------------------------------------------------------------

def detect_date_expired(market: Market) -> Optional[Candidate]:
    """Markets whose end_date is in the past but still trading non-extremely.

    Strong heuristic: if a market still accepts orders past its declared end
    date, it usually means UMA hasn't fired yet. The "winning" side is often
    obvious from the price itself.
    """
    if market.end_date is None:
        return None
    days = market.days_to_end
    if days is None or days >= 0:  # not expired yet
        return None
    if abs(days) > 30:
        return None  # very stale; data may be unreliable

    out = _consensus_outcome(market)
    if out is None:
        return None

    edge = _edge_pct(out.price)
    if edge < SETTINGS.min_edge_pct:
        return None

    # Confidence drops if it's only just expired — UMA may still flip.
    age_days = -days
    confidence = min(1.0, age_days / 3.0) * 0.7  # cap 0.7 — never fully sure
    return Candidate(
        market=market,
        outcome=out,
        side="BUY",
        entry_price=out.price,
        edge_pct=edge,
        reason=f"end_date {age_days:.1f}d ago; price {out.price:.3f}",
        confidence=confidence,
        detector="date_expired",
    )


# ---------------------------------------------------------------------------
# Detector B — Extreme price + volume confirmation
# ---------------------------------------------------------------------------

def detect_extreme_priced(market: Market) -> Optional[Candidate]:
    """Liquid markets where one side is priced ≥ 0.90 (was 0.95 pre-2026-05-06).

    Lowered threshold from 0.95→0.90 after paper-trading showed fees
    consume gross win at entry ≥0.95 (5¢ max gross vs 4¢ fee = ~0¢ net).
    At entry 0.90, max gross is 10¢ → net ~6¢ after fees, real edge.
    Trade-off: win rate drops slightly (still ~95% historically) but
    per-trade EV becomes meaningfully positive.

    The "extreme" outcome is treated as effectively decided. We buy that
    side at whatever ask is showing, capturing the (1 - price) edge to
    resolution. Volume gate guards against thin/manipulable books.
    """
    if market.volume_24h < SETTINGS.min_24h_volume:
        return None

    # We only act on the high-side outcome (price >= 0.90).
    out = max(market.outcomes, key=lambda o: o.price)
    if out.price < 0.90 or out.price > SETTINGS.max_price:
        return None

    edge = _edge_pct(out.price)
    if edge < SETTINGS.min_edge_pct:
        return None

    # Confidence: scale 0.50..0.99 for entry, 0..1 for vol up to $50k.
    # Reason: lower entry = bigger margin = higher quality, but also
    # slightly less certain than at-the-ceiling. Net result peaks
    # mid-range (entry ~0.93).
    vol_factor = min(1.0, market.volume_24h / 50_000.0)
    price_factor = (out.price - 0.90) / 0.09  # 0..1 across [0.90, 0.99]
    confidence = 0.5 + 0.4 * vol_factor * max(0.2, price_factor)
    return Candidate(
        market=market,
        outcome=out,
        side="BUY",
        entry_price=out.price,
        edge_pct=edge,
        reason=f"extreme price {out.price:.3f} on ${market.volume_24h:,.0f} vol24h",
        confidence=min(1.0, confidence),
        detector="extreme_priced",
    )


# ---------------------------------------------------------------------------
# Detector C — Bundle (sum-of-asks) arbitrage
# ---------------------------------------------------------------------------

def detect_bundle_arb(market: Market) -> Optional[Candidate]:
    """Two-outcome markets where ask_yes + ask_no < 1 - fees.

    Buying both sides costs (y + n) USDC and pays exactly 1 USDC at
    resolution — risk-free regardless of which side wins. We surface
    the YES side as primary; the NO side is attached as ``pair_outcome``
    so a future executor can place both legs as one logical trade.

    Math edge: (1 - y - n) / (y + n) * 100. Net edge subtracts an
    estimated 2× gas (one order per leg) over the combined cost.
    """
    if not SETTINGS.bundle_arb_enabled:
        return None
    if len(market.outcomes) != 2:
        return None
    if market.volume_24h < SETTINGS.min_24h_volume:
        return None

    yes = market.outcome_by_name("Yes")
    no = market.outcome_by_name("No")
    if yes is None or no is None:
        # Fall back to first/second outcome by index — some non-Yes/No markets
        # are still binary (e.g. candidate vs. opponent).
        yes, no = market.outcomes[0], market.outcomes[1]

    # We need real ask prices on both legs. Outcome.price comes from Gamma's
    # mid/last and is a usable proxy when best_bid/best_ask aren't per-token,
    # but skip if either leg is at 0 (no liquidity / not actually tradable).
    y_ask = yes.price
    n_ask = no.price
    if y_ask <= 0 or n_ask <= 0:
        return None
    cost = y_ask + n_ask
    if cost >= 1.0:
        return None
    if cost < 0.5:
        # Sum < 0.5 implies one or both outcomes are effectively-decided dust;
        # data is more likely stale than a real arb. Conservative skip.
        return None

    gross_edge = (1.0 - cost) / cost * 100.0
    if gross_edge < SETTINGS.bundle_arb_min_edge_pct:
        return None

    # Two orders' worth of gas, amortised over the combined cost.
    fee_drag_pct = (2.0 * SETTINGS.gas_estimate_per_order) / cost * 100.0
    net_edge = gross_edge - fee_drag_pct
    if net_edge < SETTINGS.min_edge_after_fees_pct:
        return None

    # Confidence: scales with edge size and 24h volume. Bundle arbs in liquid
    # markets are the textbook "free money" case; rare and trustworthy.
    vol_factor = min(1.0, market.volume_24h / 50_000.0)
    edge_factor = min(1.0, gross_edge / 5.0)  # 5% gross edge → full confidence
    confidence = 0.6 + 0.4 * vol_factor * edge_factor

    return Candidate(
        market=market,
        outcome=yes,
        side="BUY",
        entry_price=y_ask,
        edge_pct=gross_edge,
        reason=f"bundle ask_y+ask_n={cost:.3f} (edge {gross_edge:.2f}%)",
        confidence=min(1.0, confidence),
        detector="bundle_arb",
        pair_outcome=no,
        pair_price=n_ask,
        edge_pct_net=net_edge,
    )


# ---------------------------------------------------------------------------
# Detector D — Wide-spread market-making opportunity (study-mode flagger)
# ---------------------------------------------------------------------------

def detect_mm_spread(market: Market) -> Optional[Candidate]:
    """Liquid markets with a wide bid-ask spread.

    Surfaces (does NOT yet auto-trade) markets where a market-making
    strategy of posting inside the book could capture the spread.
    The candidate's "edge" is the spread minus 2¢ for the two
    competing orders we'd post inside best_bid / best_ask.

    We deliberately keep this study-only for now: live MM has real
    inventory risk and adverse selection that a momentary scan
    can't gauge. The point is to log how often the opportunity
    appears so a future executor knows what target hit-rate to expect.
    """
    if not SETTINGS.mm_flagger_enabled:
        return None
    if market.spread < SETTINGS.mm_min_spread:
        return None
    if market.volume_24h < SETTINGS.mm_min_volume:
        return None
    if market.best_bid <= 0 or market.best_ask <= 0:
        return None
    if market.best_ask <= market.best_bid:
        return None

    # Use the bid as the entry-price proxy — that's where we'd post.
    # edge_pct here is "spread-capture as % of capital deployed on a
    # round-trip", i.e. (spread - 2¢) / mid.
    mid = (market.best_bid + market.best_ask) / 2.0
    if mid <= 0:
        return None
    capture = max(0.0, market.spread - 0.02)
    edge_pct = capture / mid * 100.0
    if edge_pct < SETTINGS.min_edge_after_fees_pct:
        return None

    # Pick the side closer to even-money to surface as the "primary" leg.
    primary = min(market.outcomes, key=lambda o: abs(o.price - 0.5)) \
        if market.outcomes else None
    if primary is None:
        return None

    vol_factor = min(1.0, market.volume_24h / 50_000.0)
    confidence = 0.4 + 0.3 * vol_factor  # capped low: no track record yet

    return Candidate(
        market=market,
        outcome=primary,
        side="BUY",
        entry_price=market.best_bid,
        edge_pct=edge_pct,
        reason=f"mm spread={market.spread:.3f} on ${market.volume_24h:,.0f} vol24h",
        confidence=min(1.0, confidence),
        detector="mm_spread",
        edge_pct_net=edge_pct,
    )


# ---------------------------------------------------------------------------


DETECTORS = (detect_date_expired, detect_extreme_priced,
             detect_bundle_arb, detect_mm_spread)


def scan(markets: Iterable[Market]) -> List[Candidate]:
    """Run every detector over every market; dedupe by (condition_id, outcome)."""
    seen: dict[tuple[str, str], Candidate] = {}
    for m in markets:
        if not m.is_tradable:
            continue
        for det in DETECTORS:
            cand = det(m)
            if cand is None:
                continue
            key = (m.condition_id, cand.outcome.token_id)
            existing = seen.get(key)
            if existing is None or cand.score > existing.score:
                seen[key] = cand
    return sorted(seen.values(), key=lambda c: c.score, reverse=True)
