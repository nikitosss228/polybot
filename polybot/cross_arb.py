"""L2 cross-event correlation arbitrage detector.

Where L1.5 worked WITHIN one event (monotonic series of internal members),
L2 works ACROSS different events that share a logical relationship Polymarket
doesn't auto-link in its UI. The relationship is hand-curated in a YAML
file under ``data/cross_arb.yaml``.

Three relationship types we currently support:

* ``subset``: event A entails event B → P(A) ≤ P(B). Arb when ask(A) > bid(B).
  Example: P(Trump wins presidency) ≤ P(Republican wins presidency).

* ``temporal_monotonic``: same fact tested at different deadlines.
  P(by date1) ≤ P(by date2) when date1 < date2 (later deadline = larger
  probability window). Example: P(Russia-Ukraine ceasefire by May 31) ≤
  P(... by Jun 30).

* ``mutex_sum``: members of a partition together cover Yes-space.
  Σ P(member_i Yes) ≤ 1. Arb when Σ asks < 1 (buy all → guaranteed payout 1).
  Useful for cases where Polymarket's neg-risk binding wasn't applied.
  This is the cross-event version of L1's partition arb.

The detector is *advisory* — it surfaces candidates but does not place
orders by itself. Wire into ``scanner.scan()`` once probe-validated, or
keep as a standalone analytics script.

Why YAML and not slug-pattern matching: false positives from sound-alike
slugs would pollute the signal. Manual curation is the cost of zero
false positives in this niche.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

import yaml

from .config import PROJECT_ROOT
from .logger import get_logger

log = get_logger(__name__)

DEFAULT_YAML = PROJECT_ROOT / "data" / "cross_arb.yaml"


@dataclass
class Leg:
    """One side of a correlation pair: identifies a market + outcome."""
    event_slug: str
    outcome: str = "Yes"     # "Yes" / "No" / a candidate name
    note: str = ""           # human-readable description for logs


@dataclass
class Pair:
    """One correlation entry from the YAML.

    For ``subset`` and ``temporal_monotonic`` the inequality is
    ``P(subset) ≤ P(superset)``. For ``mutex_sum`` we use ``members``
    (a list of legs) and check ``Σ P(member) ≤ 1``.

    ``threshold`` is the minimum gap (in price units, e.g. 0.03 = 3¢)
    above which we surface as an arb candidate. Conservative defaults
    (3¢) leave room for fees + slippage.
    """
    id: str
    type: str
    threshold: float = 0.03
    note: str = ""
    subset: Optional[Leg] = None
    superset: Optional[Leg] = None
    members: List[Leg] = field(default_factory=list)

    def validate(self) -> None:
        if self.type in ("subset", "temporal_monotonic"):
            if self.subset is None or self.superset is None:
                raise ValueError(f"pair {self.id}: missing subset/superset")
        elif self.type == "mutex_sum":
            if len(self.members) < 2:
                raise ValueError(f"pair {self.id}: mutex_sum needs ≥2 members")
        else:
            raise ValueError(f"pair {self.id}: unknown type {self.type!r}")
        if not (0 < self.threshold < 0.5):
            raise ValueError(f"pair {self.id}: threshold {self.threshold} out of range")


@dataclass
class Violation:
    """A flagged inequality breach. Caller decides what to do with it."""
    pair: Pair
    cheap_leg_label: str        # which leg is the underpriced one
    cheap_event_slug: str
    cheap_outcome: str
    cheap_ask: float            # what we'd pay to buy YES on cheap side
    expensive_leg_label: str
    expensive_event_slug: str
    expensive_outcome: str
    expensive_bid: float        # what we'd get selling YES on expensive side
    gap: float                  # liquidity-aware gap (cheap_ask - expensive_bid)
    detail: str = ""

    @property
    def gap_cents(self) -> float:
        return self.gap * 100


# ---------- YAML loader -----------------------------------------------------


def _leg_from_dict(d: Optional[Dict[str, Any]]) -> Optional[Leg]:
    if d is None:
        return None
    raw_outcome = d.get("outcome", "Yes")
    # YAML 1.1 parses unquoted Yes/No as booleans. Coerce to string and
    # remap booleans → expected names so users don't have to remember
    # to quote ``"Yes"`` in every entry.
    if raw_outcome is True:
        outcome = "Yes"
    elif raw_outcome is False:
        outcome = "No"
    else:
        outcome = str(raw_outcome)
    return Leg(
        event_slug=d["event_slug"],
        outcome=outcome,
        note=d.get("note", ""),
    )


def load_pairs(path: Path = DEFAULT_YAML) -> List[Pair]:
    if not path.exists():
        log.warning("cross_arb yaml not found: %s", path)
        return []
    with path.open() as f:
        blob = yaml.safe_load(f) or {}
    pairs_raw = blob.get("pairs", [])
    out: List[Pair] = []
    for raw in pairs_raw:
        try:
            p = Pair(
                id=raw["id"],
                type=raw["type"],
                threshold=float(raw.get("threshold", 0.03)),
                note=raw.get("note", ""),
                subset=_leg_from_dict(raw.get("subset")),
                superset=_leg_from_dict(raw.get("superset")),
                members=[_leg_from_dict(m) for m in raw.get("members", []) if m],
            )
            p.validate()
            out.append(p)
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("cross_arb yaml: skipping pair %r: %s", raw.get("id", "?"), exc)
    log.info("cross_arb: loaded %d pairs from %s", len(out), path.name)
    return out


# ---------- price lookup ----------------------------------------------------


@dataclass
class MarketSnapshot:
    """Minimal price view for one outcome of one market."""
    event_slug: str
    outcome: str
    bid: float
    ask: float
    closed: bool


def collect_event_slugs(pairs: List[Pair]) -> List[str]:
    """All event slugs referenced anywhere in the YAML."""
    slugs = set()
    for p in pairs:
        if p.subset: slugs.add(p.subset.event_slug)
        if p.superset: slugs.add(p.superset.event_slug)
        for m in p.members: slugs.add(m.event_slug)
    return sorted(slugs)


async def fetch_event_snapshots(
    fetch_event_by_slug: Callable[[str], Awaitable[Optional[Dict[str, Any]]]],
    slugs: List[str],
) -> Dict[Tuple[str, str], MarketSnapshot]:
    """Fetch all referenced events and build a (slug, outcome) → snapshot map.

    ``fetch_event_by_slug`` is injected so this module is testable without
    a live PolyClient. Returns a dict keyed by ``(event_slug, outcome_name)``.

    For events with multiple member markets (e.g. neg_risk where each
    candidate is its own market), we expand ALL of them — so the YAML
    can reference any specific candidate by name in the ``outcome`` field.
    """
    import json
    out: Dict[Tuple[str, str], MarketSnapshot] = {}
    for slug in slugs:
        try:
            event = await fetch_event_by_slug(slug)
        except Exception as exc:  # noqa: BLE001
            log.warning("fetch_event_by_slug(%s) failed: %s", slug, exc)
            continue
        if event is None:
            log.warning("event not found: %s", slug)
            continue
        for m in event.get("markets", []):
            try:
                outcomes = json.loads(m.get("outcomes") or "[]")
            except (TypeError, ValueError):
                continue
            best_bid = float(m.get("bestBid") or 0)
            best_ask = float(m.get("bestAsk") or 0)
            closed = bool(m.get("closed"))
            for name in outcomes:
                # For binary Yes/No, bid/ask correspond to the Yes-token. The
                # No-token's ask = 1 - Yes-bid (and bid = 1 - Yes-ask). For
                # neg_risk events with candidate names, each market is its
                # own binary so the same logic applies: bestBid/Ask refer to
                # the first outcome (which is the candidate's "Yes wins").
                if str(name).lower() in ("yes", outcomes[0].lower() if outcomes else ""):
                    out[(slug, name)] = MarketSnapshot(
                        event_slug=slug, outcome=name,
                        bid=best_bid, ask=best_ask, closed=closed,
                    )
                else:
                    # No-token side
                    out[(slug, name)] = MarketSnapshot(
                        event_slug=slug, outcome=name,
                        bid=max(0.0, 1 - best_ask) if best_ask > 0 else 0.0,
                        ask=max(0.0, 1 - best_bid) if best_bid > 0 else 0.0,
                        closed=closed,
                    )
    return out


# ---------- evaluation ------------------------------------------------------


def evaluate(
    pair: Pair,
    snapshots: Dict[Tuple[str, str], MarketSnapshot],
) -> Optional[Violation]:
    """Check inequality for a pair against the price snapshot. None if no violation."""
    if pair.type in ("subset", "temporal_monotonic"):
        sub = snapshots.get((pair.subset.event_slug, pair.subset.outcome))
        sup = snapshots.get((pair.superset.event_slug, pair.superset.outcome))
        if sub is None or sup is None:
            log.debug("pair %s: missing snapshot for sub or sup", pair.id)
            return None
        if sub.closed or sup.closed:
            return None
        if sub.ask <= 0 or sup.bid <= 0:
            return None
        # Logical invariant: P(subset) ≤ P(superset).
        # On Polymarket prices, P_market ∈ [bid, ask] for each market.
        # The strict liquidity-aware violation: bid(subset) > ask(superset).
        # That means [bid_sub, ask_sub] is entirely above [bid_sup, ask_sup]
        # — incompatible with P_sub ≤ P_sup at any consistent valuation.
        #
        # Arb construction when violated:
        #   * buy YES_superset at ask(sup)
        #   * buy NO_subset (i.e. short subset-Yes) at price (1 - bid_sub)
        # Total cost = ask(sup) + 1 - bid(sub).
        # Resolution payoffs across all scenarios (subset Yes implies superset Yes):
        #   sub=Yes, sup=Yes: NO_sub pays 0, YES_sup pays 1 → total 1
        #   sub=No,  sup=Yes: NO_sub pays 1, YES_sup pays 1 → total 2 (bonus)
        #   sub=No,  sup=No:  NO_sub pays 1, YES_sup pays 0 → total 1
        # Min payoff = 1. Profitable when cost < 1, i.e. bid(sub) > ask(sup).
        gap = sub.bid - sup.ask
        if gap > pair.threshold:
            return Violation(
                pair=pair,
                cheap_leg_label="superset",
                cheap_event_slug=sup.event_slug,
                cheap_outcome=sup.outcome,
                cheap_ask=sup.ask,
                expensive_leg_label="subset",
                expensive_event_slug=sub.event_slug,
                expensive_outcome=sub.outcome,
                expensive_bid=sub.bid,
                gap=gap,
                detail=(
                    f"P(subset) > P(superset) by {gap*100:.1f}¢: "
                    f"sub-{sub.outcome} bid={sub.bid:.3f} > sup-{sup.outcome} ask={sup.ask:.3f}"
                ),
            )
        return None

    if pair.type == "mutex_sum":
        snaps = []
        for m in pair.members:
            s = snapshots.get((m.event_slug, m.outcome))
            if s is None or s.closed or s.ask <= 0:
                return None
            snaps.append((m, s))
        sigma_ask = sum(s.ask for _, s in snaps)
        gap = 1.0 - sigma_ask
        if gap > pair.threshold:
            cheapest = min(snaps, key=lambda x: x[1].ask)
            sup_dummy = snaps[0][1]
            return Violation(
                pair=pair,
                cheap_leg_label=f"member[{cheapest[0].event_slug[:30]}]",
                cheap_event_slug=cheapest[1].event_slug,
                cheap_outcome=cheapest[1].outcome,
                cheap_ask=cheapest[1].ask,
                expensive_leg_label="bundle",
                expensive_event_slug="",
                expensive_outcome="",
                expensive_bid=0.0,
                gap=gap,
                detail=(
                    f"Σ ask = {sigma_ask:.3f} < 1 by {gap*100:.1f}¢; "
                    f"buying all members guarantees payoff 1"
                ),
            )
        return None

    return None


async def find_violations(
    fetch_event_by_slug: Callable[[str], Awaitable[Optional[Dict[str, Any]]]],
    pairs: Optional[List[Pair]] = None,
) -> List[Violation]:
    """Top-level entrypoint: load pairs (if not passed), fetch, evaluate."""
    pairs = pairs if pairs is not None else load_pairs()
    if not pairs:
        return []
    slugs = collect_event_slugs(pairs)
    snapshots = await fetch_event_snapshots(fetch_event_by_slug, slugs)
    log.info("cross_arb: snapshot cache has %d (slug,outcome) entries from %d events",
             len(snapshots), len(slugs))
    out: List[Violation] = []
    for p in pairs:
        v = evaluate(p, snapshots)
        if v is not None:
            out.append(v)
    out.sort(key=lambda v: -v.gap)
    return out
