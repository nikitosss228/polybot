#!/usr/bin/env python3
"""L1.5 monotonic-series pre-flight probe.

Purpose: before committing to a full ``detect_monotonic_series_arb``
build, quantify two things from current Gamma data:

  1. **Coverage**: how many themed events parse cleanly to a sortable
     key (price threshold or date)?
  2. **Density**: among those, how many show *current* monotone-ordering
     violations large enough to clear fees?

If coverage is < ~30 events or density is 0 → drop the L1.5 build.
If coverage is healthy and we see real violations >= 2× fee_rate →
proceed to build.

This script is read-only and standalone — it does not import polybot
modules so it can be re-run cheaply on demand. Output is a single
human-readable summary.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import requests

GAMMA_HOST = "https://gamma-api.polymarket.com"
PAGE_SIZE = 100
MAX_EVENTS = 400
SNAPSHOT_PATH = Path(__file__).resolve().parent.parent / "logs" / "probe_l15_snapshots.jsonl"
HTTP_TIMEOUT = 20

# Fee-aware violation threshold. Sports = 3% taker, politics = 4%, culture = 5%.
# A 2-leg arb pays fees on both legs, so the per-pair threshold for "real"
# edge is 2 × fee_rate. Use 6% as a midpoint sentinel; report the histogram
# anyway so the right cutoff is visible from the data.
THRESHOLD_REAL = 0.06   # 6 cents — clears 2 × 3% sports fees with margin
THRESHOLD_LOOSE = 0.02  # 2 cents — surface for sanity-check, below fees

# Maker-mode parameters. We model "post limit just inside the spread by
# PRICE_IMPROVEMENT cents" (price-improving each leg by ~1¢ vs the taker
# would-have-paid price). Filled maker orders pay 0 fee and earn
# MAKER_REBATE_FRAC × fee_rate as a rebate. Polymarket's feeSchedule
# universally has takerOnly=true and rebateRate=0.25 across all 7 fee
# categories observed 2026-05-04. This model is a CEILING on maker-side
# economics — the probe cannot observe actual fill rates. Real maker
# fills depend on someone hitting the posted limits before they're
# moved by other quotes, which is unobservable in dry-run.
PRICE_IMPROVEMENT = 0.01     # 1¢ price improvement per leg (configurable)
MAKER_REBATE_FRAC = 0.25     # rebate is 25% of the would-have-been taker fee


# ---------- parsers ---------------------------------------------------------


# Threshold: numeric value, optional $/↑/↓ prefix, optional thousands separator,
# optional "k"/"K"/"m"/"M"/"b"/"B" suffix. We extract the *first* number we see
# *and* the arrow prefix if present — the arrow encodes which side of the
# threshold the question is on ("↑ $X" = "above X" vs "↓ $X" = "below X"),
# and these are two DIFFERENT monotone relationships even within the same
# event. Mixing them in one sort produces phantom inversions.
_NUM_RE = re.compile(
    r"""
    (?P<arrow>[↑↓])?\s*\$?\s*
    (?P<num>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)
    \s*(?P<suffix>[kKmMbB])?
    """,
    re.VERBOSE,
)


def parse_threshold(title: str) -> Optional[Tuple[Optional[str], float]]:
    """Extract ``(arrow, value)`` from a ``groupItemTitle``.

    ``arrow`` is "↑", "↓", or ``None`` (no arrow → single-direction series).
    Returns ``None`` if the title doesn't look like a numeric threshold.
    """
    if not title:
        return None
    m = _NUM_RE.search(title)
    if not m:
        return None
    raw = m.group("num").replace(",", "")
    try:
        val = float(raw)
    except ValueError:
        return None
    suffix = m.group("suffix")
    if suffix in ("k", "K"):
        val *= 1_000
    elif suffix in ("m", "M"):
        val *= 1_000_000
    elif suffix in ("b", "B"):
        val *= 1_000_000_000
    return m.group("arrow"), val


_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_DATE_RE = re.compile(
    r"""
    (?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?
              |jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?
              |nov(?:ember)?|dec(?:ember)?)
    \s+(?P<day>\d{1,2})
    (?:[\s,]+(?P<year>\d{4}))?
    """,
    re.VERBOSE | re.IGNORECASE,
)


def parse_date(title: str, fallback_year: int) -> Optional[datetime]:
    """Extract a date from titles like 'by September 30, 2025' / 'April 30'.

    ``fallback_year`` is used when the title omits the year (common for
    same-year series like 'March 31' / 'April 30'). Caller passes the
    event's end-date year as a sane default.
    """
    if not title:
        return None
    m = _DATE_RE.search(title)
    if not m:
        return None
    month = _MONTHS.get(m.group("month").lower())
    if month is None:
        return None
    try:
        day = int(m.group("day"))
        year = int(m.group("year")) if m.group("year") else fallback_year
        return datetime(year, month, day)
    except (ValueError, TypeError):
        return None


# ---------- fetch -----------------------------------------------------------


def fetch_events(max_n: int) -> List[dict]:
    out: List[dict] = []
    offset = 0
    while len(out) < max_n:
        params = {
            "closed": "false",
            "active": "true",
            "archived": "false",
            "limit": min(PAGE_SIZE, max_n - len(out)),
            "offset": offset,
            "order": "volume24hr",
            "ascending": "false",
        }
        r = requests.get(f"{GAMMA_HOST}/events", params=params, timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        page = r.json()
        if not isinstance(page, list) or not page:
            break
        out.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return out


# ---------- analysis --------------------------------------------------------


@dataclass
class ParsedMember:
    title: str
    key: float
    ask_yes: float
    bid_yes: float
    closed: bool
    active: bool
    accepting: bool
    fee_rate: float  # taker rate from feeSchedule.rate; 0.03 sports, 0.04 politics/finance/tech, 0.05 culture/economics/weather, 0.072 crypto

    @property
    def tradable(self) -> bool:
        return (
            self.accepting
            and self.active
            and not self.closed
            and self.ask_yes > 0.0
        )


def _f(value, default=0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _make_member(m: dict, key: float) -> ParsedMember:
    sched = m.get("feeSchedule") or {}
    rate = _f(sched.get("rate"), default=0.04)  # default 4% if missing — neither dust nor pessimistic
    return ParsedMember(
        title=m.get("groupItemTitle", ""),
        key=float(key),
        ask_yes=_f(m.get("bestAsk")),
        bid_yes=_f(m.get("bestBid")),
        closed=bool(m.get("closed")),
        active=bool(m.get("active")),
        accepting=bool(m.get("acceptingOrders")),
        fee_rate=rate,
    )


def _parse_event_into_series(
    event: dict,
) -> List[Tuple[str, str, List[ParsedMember]]]:
    """Decompose an event into one or more analyzable monotone sub-series.

    Returns a list of ``(kind, sub_label, members)`` tuples. Most events
    yield a single tuple. Events whose threshold titles split across
    ``↑`` / ``↓`` arrows yield one tuple per arrow direction (each with
    ≥3 distinct keys) — splitting recovers events like
    ``what-price-will-wti-hit-in-may-2026`` whose 20 markets encode
    two independent monotone relationships ("wti reaches at least $X"
    vs "wti dips to $X or below").

    ``sub_label`` is "" for whole-event series, or the arrow string
    ("↑" / "↓") for split sub-series. Surfaced in display only.
    Empty list = event doesn't fit any parser.
    """
    markets = event.get("markets", [])
    if len(markets) < 3:
        return []

    end_date_str = event.get("endDate") or ""
    fallback_year = datetime.now().year
    if end_date_str:
        try:
            fallback_year = datetime.fromisoformat(end_date_str.replace("Z", "+00:00")).year
        except ValueError:
            pass

    titles = [m.get("groupItemTitle", "") for m in markets]

    # Try date parser FIRST — threshold parser would greedily match the
    # day-of-month number in titles like "March 31" and produce nonsense
    # keys (all titles ending in '31' would collapse to key=31.0).
    dates = [parse_date(t, fallback_year) for t in titles]
    if all(d is not None for d in dates) and len({d.toordinal() for d in dates}) >= 3:
        members = [_make_member(m, d.toordinal()) for m, d in zip(markets, dates)]
        return [("date", "", members)]

    thresholds = [parse_threshold(t) for t in titles]
    if not all(x is not None for x in thresholds):
        return []

    arrows = {x[0] for x in thresholds}
    keys = [x[1] for x in thresholds]

    if len(arrows) == 1 and len(set(keys)) >= 3:
        members = [_make_member(m, k) for m, k in zip(markets, keys)]
        return [("threshold", "", members)]

    # Mixed arrows — split per direction. Each sub-series needs ≥3 distinct keys.
    out: List[Tuple[str, str, List[ParsedMember]]] = []
    for arrow in sorted(arrows, key=lambda a: a or ""):
        idxs = [i for i, t in enumerate(thresholds) if t[0] == arrow]
        sub_keys = [thresholds[i][1] for i in idxs]
        if len(idxs) < 3 or len(set(sub_keys)) < 3:
            continue
        sub_members = [_make_member(markets[i], thresholds[i][1]) for i in idxs]
        label = arrow if arrow else "no-arrow"
        out.append(("threshold", label, sub_members))
    return out


@dataclass
class Inversion:
    """A flagged monotone-violation pair with four honesty levels.

    Taker-mode (we cross the spread):
    * ``ask_gap`` — purely the difference in ask-yes between the two
      legs after directional sign-fix. Optimistic / paper-only.
    * ``liq_gap`` — bid-aware: ``cheap_ask - expensive_bid``. The real
      spread we'd capture filling at quotes. The XAU finding
      (bestBid 0.01-0.03 vs ask 0.92) collapses liq_gap to -90¢ where
      ask_gap looked like +55¢.
    * ``net_after_fees`` — ``liq_gap - (fee_a + fee_b)``. Positive = real
      after-fees taker arb if quoted depth holds.

    Maker-mode (we post inside the spread, wait for fills):
    * ``maker_net`` — assumes both limits fill at PRICE_IMPROVEMENT cents
      inside the spread, pays 0 fee, earns MAKER_REBATE_FRAC × fee_rate
      rebate per filled leg. This is a CEILING — fill rate is unobservable
      in dry-run, so the actual maker PnL is at-most this number times
      the (unknown) joint-fill probability.
    """
    cheap: ParsedMember
    expensive: ParsedMember
    ask_gap: float
    liq_gap: float
    net_after_fees: float
    maker_net: float


def _detect_violations(members: List[ParsedMember], slug: str) -> List[Inversion]:
    """Walk sorted-by-key pairs; return inversions where ask_yes ordering
    contradicts the event's expected monotone direction.

    Direction inference: count adjacent diffs in ask_yes after sorting by
    key; the *dominant sign* is the expected direction. For each inversion
    pair we compute three metrics (see ``Inversion``). Caller decides which
    threshold to apply — the probe keeps all three so downstream analysis
    of the snapshot JSONL can split paper / liquidity / after-fees signal.
    """
    tradable = [m for m in members if m.tradable]
    if len(tradable) < 3:
        return []
    tradable.sort(key=lambda m: m.key)
    asks = [m.ask_yes for m in tradable]

    # Vote for direction.
    diffs = [asks[i + 1] - asks[i] for i in range(len(asks) - 1)]
    pos = sum(1 for d in diffs if d > 0)
    neg = sum(1 for d in diffs if d < 0)
    if pos == 0 and neg == 0:
        return []
    direction = +1 if pos >= neg else -1

    out: List[Inversion] = []
    for i in range(len(tradable)):
        for j in range(i + 1, len(tradable)):
            mi, mj = tradable[i], tradable[j]
            if direction == +1:
                # Asks should INCREASE with key. Inversion: mj.ask < mi.ask.
                # Construct: buy YES on j (cheap) + buy NO on i (overpriced YES).
                # Net cost = mj.ask + (1 - mi.bid). Profit when ask_j < bid_i.
                if mj.ask_yes >= mi.ask_yes:
                    continue
                ask_gap = mi.ask_yes - mj.ask_yes
                liq_gap = mi.bid_yes - mj.ask_yes
                cheap, expensive = mj, mi
            else:
                # Asks should DECREASE with key. Inversion: mj.ask > mi.ask.
                if mj.ask_yes <= mi.ask_yes:
                    continue
                ask_gap = mj.ask_yes - mi.ask_yes
                liq_gap = mj.bid_yes - mi.ask_yes
                cheap, expensive = mi, mj
            if ask_gap < THRESHOLD_LOOSE:
                continue
            taker_net = liq_gap - (cheap.fee_rate + expensive.fee_rate)
            # Maker-mode model. Post limit-buys inside the spread by
            # PRICE_IMPROVEMENT each leg:
            #   leg A cost = cheap.ask - PI
            #   leg B cost = (1 - expensive.bid) - PI   [buy NO at posted-inside-spread]
            #   bundle cost = cheap.ask + 1 - expensive.bid - 2*PI
            #   gross profit (when both fill, one resolves Yes) = 1 - bundle = liq_gap + 2*PI
            #   maker pays 0 fee + earns rebate = MAKER_REBATE_FRAC * fee * leg_cost
            leg_a_cost = max(0.0, cheap.ask_yes - PRICE_IMPROVEMENT)
            leg_b_cost = max(0.0, (1.0 - expensive.bid_yes) - PRICE_IMPROVEMENT)
            gross_profit = 1.0 - (leg_a_cost + leg_b_cost)
            rebate = MAKER_REBATE_FRAC * (
                cheap.fee_rate * leg_a_cost + expensive.fee_rate * leg_b_cost
            )
            maker_net = gross_profit + rebate
            out.append(Inversion(
                cheap=cheap, expensive=expensive,
                ask_gap=ask_gap, liq_gap=liq_gap,
                net_after_fees=taker_net, maker_net=maker_net,
            ))
    return out


# ---------- main ------------------------------------------------------------


def _run(quiet: bool = False) -> dict:
    """Run the probe and return a structured summary dict.

    When ``quiet`` is True, suppresses stdout — used by snapshot mode
    where the report is just noise. The returned dict is the
    canonical machine-readable output and is what gets appended to
    the snapshots JSONL.
    """
    if not quiet:
        print(f"Fetching up to {MAX_EVENTS} events from {GAMMA_HOST}/events...")
    events = fetch_events(MAX_EVENTS)
    if not quiet:
        print(f"Got {len(events)} events.\n")

    themed = [e for e in events
              if not e.get("enableNegRisk") and len(e.get("markets", [])) >= 3]

    # Each entry is (event, sub_label, members) — sub_label is "" for
    # whole-event series, "↑" / "↓" / "no-arrow" for split sub-series.
    parsed_threshold: List[Tuple[dict, str, List[ParsedMember]]] = []
    parsed_date: List[Tuple[dict, str, List[ParsedMember]]] = []
    rejected_reasons: Counter = Counter()
    n_split_events = 0

    for e in themed:
        series_list = _parse_event_into_series(e)
        if not series_list:
            titles = [m.get("groupItemTitle", "") for m in e.get("markets", [])]
            if not any(titles):
                rejected_reasons["empty_titles"] += 1
            elif any(parse_threshold(t) is not None for t in titles):
                rejected_reasons["unparseable_titles"] += 1
            elif any(parse_date(t, 2026) is not None for t in titles):
                rejected_reasons["mixed_date_partial"] += 1
            else:
                rejected_reasons["unparseable_titles"] += 1
            continue
        if len(series_list) > 1:
            n_split_events += 1
        for kind, label, members in series_list:
            if kind == "threshold":
                parsed_threshold.append((e, label, members))
            elif kind == "date":
                parsed_date.append((e, label, members))

    parsed_all = parsed_threshold + parsed_date
    series_with_3plus_tradable = []
    for e, label, members in parsed_all:
        tradable = [m for m in members if m.tradable]
        if len(tradable) >= 3:
            series_with_3plus_tradable.append((e, label, members, tradable))

    paper_arbs: List[Tuple[dict, str, Inversion]] = []        # ask_gap >= THRESHOLD_REAL (legacy "after-fees" metric, ~6¢)
    liq_arbs: List[Tuple[dict, str, Inversion]] = []          # liq_gap > 0 — buy cheap-ask + buy opposite-bid still <$1
    net_arbs: List[Tuple[dict, str, Inversion]] = []          # taker net > 0
    maker_arbs: List[Tuple[dict, str, Inversion]] = []        # maker_net > 0 (ceiling — fill rate unobserved)
    loose_count = 0
    # Best per series by maker_net (since that's the new-most-promising
    # metric and the watch-list of regimes-to-monitor).
    per_series_best: dict = {}

    for e, label, members, tradable in series_with_3plus_tradable:
        slug = e.get("slug", "")
        for inv in _detect_violations(members, slug):
            loose_count += 1
            if inv.ask_gap >= THRESHOLD_REAL:
                paper_arbs.append((e, label, inv))
            if inv.liq_gap > 0:
                liq_arbs.append((e, label, inv))
            if inv.net_after_fees > 0:
                net_arbs.append((e, label, inv))
            if inv.maker_net > 0:
                maker_arbs.append((e, label, inv))
            key = (slug, label)
            cur = per_series_best.get(key)
            if cur is None or inv.maker_net > cur[2].maker_net:
                per_series_best[key] = (e, label, inv)

    flagged_events = []
    for (slug, label), (e, _label, inv) in per_series_best.items():
        if inv.ask_gap < THRESHOLD_LOOSE:
            continue
        flagged_events.append({
            "slug": slug,
            "sub": label,
            "vol24h": float(e.get("volume24hr", 0) or 0),
            "ask_gap_cents": round(inv.ask_gap * 100, 2),
            "liq_gap_cents": round(inv.liq_gap * 100, 2),
            "net_after_fees_cents": round(inv.net_after_fees * 100, 2),
            "maker_net_cents": round(inv.maker_net * 100, 2),
            "fee_a_pct": round(inv.cheap.fee_rate * 100, 2),
            "fee_b_pct": round(inv.expensive.fee_rate * 100, 2),
            "cheap_title": inv.cheap.title,
            "cheap_ask": inv.cheap.ask_yes,
            "cheap_bid": inv.cheap.bid_yes,
            "expensive_title": inv.expensive.title,
            "expensive_ask": inv.expensive.ask_yes,
            "expensive_bid": inv.expensive.bid_yes,
        })
    # Sort by maker_net desc (the new highest-honesty metric we have).
    flagged_events.sort(key=lambda r: -r["maker_net_cents"])

    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "events_fetched": len(events),
        "themed": len(themed),
        "parsed_threshold_series": len(parsed_threshold),
        "parsed_date_series": len(parsed_date),
        "split_events": n_split_events,
        "rejected": dict(rejected_reasons),
        "series_with_3plus_tradable": len(series_with_3plus_tradable),
        "loose_violations_pairs": loose_count,
        "paper_arb_pairs": len(paper_arbs),
        "liq_arb_pairs": len(liq_arbs),
        "net_arb_pairs": len(net_arbs),                                  # taker
        "net_arb_distinct_series": len({(e.get("slug"), lbl) for e, lbl, _ in net_arbs}),
        "maker_arb_pairs": len(maker_arbs),
        "maker_arb_distinct_series": len({(e.get("slug"), lbl) for e, lbl, _ in maker_arbs}),
        "flagged_events": flagged_events,
    }


def _print_human_report(summary: dict) -> None:
    print(f"Themed events (enableNegRisk=False, >=3 markets): {summary['themed']}")
    print(f"  parsed as threshold series: {summary['parsed_threshold_series']}")
    print(f"  parsed as date series:      {summary['parsed_date_series']}")
    print(f"  split mixed-arrow events:   {summary['split_events']}")
    print(f"  rejected: {summary['rejected']}\n")
    print(f"Series with >=3 tradable members: {summary['series_with_3plus_tradable']}\n")
    print(f"Inversions found (sorted by honesty):")
    print(f"  loose ask-gap > {THRESHOLD_LOOSE*100:.0f}¢:                  {summary['loose_violations_pairs']:>4}  (paper, no liquidity check)")
    print(f"  paper after-fees > {THRESHOLD_REAL*100:.0f}¢ (legacy):      {summary['paper_arb_pairs']:>4}  (still ask-only)")
    print(f"  liquidity-aware (cheap_ask < expensive_bid): {summary['liq_arb_pairs']:>4}  (could fill at quote)")
    print(f"  TAKER net > 0 (liq_gap minus 2× taker fee):  {summary['net_arb_pairs']:>4}")
    print(f"  MAKER net > 0 (price-improve, 0 fee + rebate): {summary['maker_arb_pairs']:>4}  ← ceiling, fill rate unobserved")
    print()
    flagged = summary["flagged_events"]
    if flagged:
        print("Top inversions (one per series, by maker_net):")
        for r in flagged[:15]:
            label = f" [{r['sub']}]" if r['sub'] else ""
            mtag = " ★" if r["maker_net_cents"] > 0 else ""
            print(f"  ask={r['ask_gap_cents']:+5.1f}¢  liq={r['liq_gap_cents']:+5.1f}¢  taker={r['net_after_fees_cents']:+5.1f}¢  "
                  f"maker={r['maker_net_cents']:+5.1f}¢{mtag}  vol24h=${r['vol24h']:>10,.0f}  "
                  f"fees={r['fee_a_pct']:.1f}%/{r['fee_b_pct']:.1f}%  {r['slug']}{label}")
            print(f"    cheap   '{r['cheap_title']}' bid={r['cheap_bid']:.3f}  ask={r['cheap_ask']:.3f}")
            print(f"    expens. '{r['expensive_title']}' bid={r['expensive_bid']:.3f}  ask={r['expensive_ask']:.3f}")
        print()
    print("=" * 70)
    if summary["series_with_3plus_tradable"] < 30:
        print(f"COVERAGE THIN: only {summary['series_with_3plus_tradable']} parseable series.")
    else:
        print(f"COVERAGE OK: {summary['series_with_3plus_tradable']} parseable series.")
    if summary["net_arb_distinct_series"] == 0:
        print("TAKER DENSITY ZERO — no pair survives both liquidity AND taker fees.")
    else:
        print(f"TAKER DENSITY: {summary['net_arb_distinct_series']} series show taker net > 0.")
    if summary["maker_arb_distinct_series"] == 0:
        print("MAKER DENSITY ZERO — even rebate-aware maker model finds no positive net.")
    else:
        print(f"MAKER DENSITY: {summary['maker_arb_distinct_series']} series ★ would clear the maker math (if both legs filled).")
        print("  ↑ This is a CEILING. Probe cannot observe joint-leg fill rate p; real maker PnL")
        print("    = ceiling × p². In dry-run, p is unknown. To validate, post live test orders.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", action="store_true",
                    help=f"Append a JSON summary line to {SNAPSHOT_PATH} and exit silently. "
                         "Intended for periodic timer-driven runs.")
    args = ap.parse_args()

    if args.snapshot:
        summary = _run(quiet=True)
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SNAPSHOT_PATH.open("a") as f:
            f.write(json.dumps(summary, separators=(",", ":")) + "\n")
        return 0

    summary = _run(quiet=False)
    _print_human_report(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
