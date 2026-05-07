#!/usr/bin/env python3
"""Parametric backtest over study CSVs — find filter combos with positive
net-after-fees ROI on directional detectors.

Loads ``candidates.csv`` + ``resolutions.csv`` and computes realized
directional PnL for each *confidently resolved* candidate (near_one → 1.0,
near_zero → 0.0; ambiguous fell_out_of_feed and never-observed defaults
are excluded). Subtracts an entry-side taker fee inferred from the slug
(3% sports, 4% politics/finance/tech, 5% culture/economics/weather,
7.2% crypto). No exit fee — the model assumes hold-to-resolution where
UMA redemption is free.

Sweeps a grid of (min_edge, max_edge, min_vol, min_conf, micro_tf) and
reports per-cell stats sorted by net-ROI. Cells with n<50 are dropped
to suppress small-sample noise.

mm_spread is **excluded** from this backtest — its PnL model
``size * edge_pct / 100`` requires a fill-rate assumption the study CSVs
can't validate. Run ``analyze.py`` for its (paper-bound) numbers.

bundle_arb is dropped too — sample n=3 in current data, not enough
to say anything.

Output: human-readable ranked table to stdout. Run cost <30s on 4-core box.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
CAND_CSV = LOG_DIR / "candidates.csv"
RES_CSV = LOG_DIR / "resolutions.csv"
HISTORY_JSONL = LOG_DIR / "backtest_history.jsonl"

MIN_N_PER_CELL = 50

# The "validated cell" — found 2026-05-04 (n=64, NET +1.78%). Walk-forward
# tracking watches if this same fixed-filter cell holds up over time. Don't
# tune these without rebaselining the whole history file.
VALIDATED_CELL = {
    "min_edge": 2.0, "max_edge": 20.0, "min_conf": 0.0,
    "min_vol": 250_000.0, "exclude_micro": False,
    "detectors": ("date_expired", "extreme_priced"),
}

# ---------- fee inference ---------------------------------------------------

# Slug-prefix heuristic for fee category. Order matters — first match wins.
# When in doubt, default to 4% politics (median), which is also Polymarket's
# most common category. Verified categories from the Gamma API survey:
#   sports_fees_v2 → 3%, politics_fees → 4%, finance_prices_fees → 4%,
#   tech_fees → 4%, culture_fees → 5%, economics_fees → 5%, weather_fees → 5%,
#   crypto_fees_v2 → 7.2%
SPORTS_PREFIXES = (
    "lol-", "cs2-", "dota2-", "val-", "atp-", "wta-", "nba-", "mlb-",
    "nfl-", "nhl-", "epl-", "ucl-", "uel-", "sea-", "rusrp-", "bra-",
    "f1-", "fra-", "ger-", "esp-", "ita-", "ned-", "tur-", "rus-",
    "ukr1-", "fra1-", "ger1-", "esp1-", "ita1-", "f-", "rugby-",
    "boxing-", "ufc-", "mma-", "cricket-", "ipl-", "tennis-",
    "golf-", "snooker-", "darts-", "pga-",
    "mar1-",  # Moroccan 1st-tier football
)
CRYPTO_KEYWORDS = (
    "bitcoin", "ethereum", "btc-", "eth-", "sol-", "xrp-", "bnb-",
    "doge", "ada-", "matic-", "avax-", "trx-", "ltc-", "linkup",
    "shib", "polygon", "chainlink", "memecoin", "cryptocurrency",
    "fdv-", "token-launch",
)
WEATHER_KEYWORDS = (
    "temperature-in-", "rain-in-", "snow-in-", "weather-",
)
CULTURE_KEYWORDS = (
    "eurovision", "oscar", "grammy", "emmy", "tweets",
    "tour-de-france", "song-contest",
)
FINANCE_KEYWORDS = (
    "wti-", "xauusd", "xagusd", "spy-", "qqq-", "amzn-", "tsla-",
    "aapl-", "msft-", "googl-", "meta-", "nvda-", "stock-",
    "gas-", "oil-", "gold-", "silver-",
)


def fee_rate_for_slug(slug: str) -> float:
    """Return the inferred Polymarket taker fee for this market.

    Conservative: defaults to 4% (politics rate) when the slug doesn't
    match any pattern. The grid sweep is run twice (raw + after-fee),
    so cells whose ranking depends critically on fee inference are
    visible — anything stable across both views is robust.
    """
    s = slug.lower()
    for p in SPORTS_PREFIXES:
        if s.startswith(p):
            return 0.03
    for kw in CRYPTO_KEYWORDS:
        if kw in s:
            return 0.072
    for kw in WEATHER_KEYWORDS:
        if kw in s:
            return 0.05
    for kw in CULTURE_KEYWORDS:
        if kw in s:
            return 0.05
    for kw in FINANCE_KEYWORDS:
        if kw in s:
            return 0.04
    return 0.04  # default politics


# ---------- detector + flags (mirror analyze.py) ----------------------------


def classify_detector(reason: str) -> str:
    if reason.startswith("end_date"):
        return "date_expired"
    if reason.startswith("extreme price"):
        return "extreme_priced"
    if reason.startswith("bundle "):
        return "bundle_arb"
    if reason.startswith("mm "):
        return "mm_spread"
    return "unknown"


MICRO_TF_PATTERNS = (
    re.compile(r"-updown-\d+[mhd]-"),
    re.compile(r"-up-or-down-.*-\d+(am|pm)-et$"),
)


def is_micro_tf(slug: str) -> bool:
    return any(p.search(slug) for p in MICRO_TF_PATTERNS)


# ---------- data load -------------------------------------------------------


@dataclass
class Trade:
    detector: str
    slug: str
    entry: float
    terminal: float
    size: float
    edge_pct: float
    confidence: float
    volume_24h: float
    micro_tf: bool
    fee_rate: float

    @property
    def directional_pnl(self) -> float:
        if self.entry <= 0:
            return 0.0
        return self.size * (self.terminal - self.entry) / self.entry

    @property
    def fee_paid(self) -> float:
        return self.fee_rate * self.size

    @property
    def net_pnl(self) -> float:
        return self.directional_pnl - self.fee_paid


def _f(value, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_resolutions() -> Dict[str, float]:
    """Map token_id → terminal payout (1.0 or 0.0).

    Same filter as analyze.load_resolutions: requires non-empty
    last_seen_ts AND a confident reason (near_one / near_zero).
    """
    if not RES_CSV.exists():
        return {}
    out: Dict[str, float] = {}
    with RES_CSV.open(newline="") as f:
        for row in csv.DictReader(f):
            if not row.get("last_seen_ts"):
                continue
            reason = row.get("resolution_reason", "")
            if reason == "near_one":
                out[row["token_id"]] = 1.0
            elif reason == "near_zero":
                out[row["token_id"]] = 0.0
    return out


def load_first_candidates() -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    with CAND_CSV.open(newline="") as f:
        for row in csv.DictReader(f):
            tid = row["token_id"]
            existing = out.get(tid)
            if existing is None or int(row["tick_id"]) < int(existing["tick_id"]):
                out[tid] = row
    return out


def load_trades() -> List[Trade]:
    resolutions = load_resolutions()
    candidates = load_first_candidates()
    trades: List[Trade] = []
    for tid, c in candidates.items():
        if tid not in resolutions:
            continue  # not yet resolved confidently
        det = classify_detector(c.get("reason", ""))
        if det not in ("date_expired", "extreme_priced"):
            continue  # skip mm_spread (model uncertainty), bundle_arb (n=3)
        slug = c.get("slug", "")
        trades.append(Trade(
            detector=det,
            slug=slug,
            entry=_f(c["entry_price"]),
            terminal=resolutions[tid],
            size=_f(c["size_usd"]),
            edge_pct=_f(c["edge_pct"]),
            confidence=_f(c["confidence"]),
            volume_24h=_f(c["volume_24h"]),
            micro_tf=is_micro_tf(slug),
            fee_rate=fee_rate_for_slug(slug),
        ))
    return trades


# ---------- grid sweep ------------------------------------------------------


@dataclass
class Cell:
    n: int = 0
    wins: int = 0
    total_size: float = 0.0
    total_directional: float = 0.0
    total_fees: float = 0.0

    def add(self, t: Trade) -> None:
        self.n += 1
        self.total_size += t.size
        self.total_directional += t.directional_pnl
        self.total_fees += t.fee_paid
        if t.terminal > t.entry:
            self.wins += 1

    @property
    def total_net(self) -> float:
        return self.total_directional - self.total_fees

    @property
    def roi_gross(self) -> float:
        return self.total_directional / self.total_size if self.total_size > 0 else 0.0

    @property
    def roi_net(self) -> float:
        return self.total_net / self.total_size if self.total_size > 0 else 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0


def matches_filter(t: Trade,
                   min_edge: float, max_edge: float,
                   min_conf: float, min_vol: float,
                   exclude_micro: bool,
                   detectors: Optional[Tuple[str, ...]] = None) -> bool:
    if detectors is not None and t.detector not in detectors:
        return False
    if not (min_edge <= t.edge_pct <= max_edge):
        return False
    if t.confidence < min_conf:
        return False
    if t.volume_24h < min_vol:
        return False
    if exclude_micro and t.micro_tf:
        return False
    return True


def sweep(trades: List[Trade]) -> List[Tuple[dict, Cell]]:
    grid = {
        "min_edge": [0.0, 1.0, 2.0, 5.0, 10.0],
        "max_edge": [10.0, 20.0, 50.0, 100.0, 1000.0],
        "min_conf": [0.0, 0.3, 0.5, 0.7],
        "min_vol":  [0.0, 1_000.0, 5_000.0, 50_000.0, 250_000.0],
        "exclude_micro": [True, False],
        "detectors": [
            ("date_expired", "extreme_priced"),  # both
            ("date_expired",),
            ("extreme_priced",),
        ],
    }
    keys = list(grid.keys())
    out: List[Tuple[dict, Cell]] = []
    for combo in product(*[grid[k] for k in keys]):
        params = dict(zip(keys, combo))
        if params["min_edge"] >= params["max_edge"]:
            continue
        cell = Cell()
        for t in trades:
            if matches_filter(t,
                              min_edge=params["min_edge"],
                              max_edge=params["max_edge"],
                              min_conf=params["min_conf"],
                              min_vol=params["min_vol"],
                              exclude_micro=params["exclude_micro"],
                              detectors=params["detectors"]):
                cell.add(t)
        if cell.n >= MIN_N_PER_CELL:
            out.append((params, cell))
    return out


# ---------- output ----------------------------------------------------------


def _fmt_dets(d: Tuple[str, ...]) -> str:
    if len(d) == 2:
        return "both"
    return d[0]


def print_top(results: List[Tuple[dict, Cell]], top_n: int = 30) -> None:
    if not results:
        print("No cells with n >= 50 — sample too small for any filter.")
        return

    # Rank by net ROI desc.
    ranked = sorted(results, key=lambda r: -r[1].roi_net)

    print(f"{len(results)} cells with n >= {MIN_N_PER_CELL} (out of total grid)")
    print()
    print("Top by NET-AFTER-FEES ROI (the bar that matters):")
    print(f"  {'rank':>4}  {'detector':>14}  {'edge%':>10}  {'conf':>5}  "
          f"{'vol':>8}  {'noTF':>4}  "
          f"{'n':>5}  {'win%':>5}  {'gross_roi':>9}  {'NET_ROI':>8}  "
          f"{'NET_$':>9}")
    for rank, (params, cell) in enumerate(ranked[:top_n], 1):
        edge_str = f"{params['min_edge']:.0f}-{params['max_edge']:.0f}" \
            if params['max_edge'] < 1000 else f">{params['min_edge']:.0f}"
        vol_str = f"{params['min_vol']:.0f}" if params['min_vol'] >= 1000 else "any"
        if params['min_vol'] >= 1000:
            vol_str = f"{params['min_vol']/1000:.0f}k"
        notf = "yes" if params["exclude_micro"] else "no"
        print(f"  {rank:>4}  {_fmt_dets(params['detectors']):>14}  "
              f"{edge_str:>10}  {params['min_conf']:>5.2f}  "
              f"{vol_str:>8}  {notf:>4}  "
              f"{cell.n:>5}  {cell.win_rate*100:>4.1f}%  "
              f"{cell.roi_gross*100:>+8.2f}%  "
              f"{cell.roi_net*100:>+7.2f}%  "
              f"${cell.total_net:>+7.2f}")

    # Highlight: best NET-positive cells (the answer to "is there an edge?")
    positive = [r for r in ranked if r[1].roi_net > 0]
    print()
    print(f"Cells with NET-positive ROI (n>={MIN_N_PER_CELL}): {len(positive)} out of {len(ranked)}")
    if not positive:
        print("→ NO filter combo on (date_expired, extreme_priced) shows positive net ROI after fees on the resolved subset. The directional edge is below fees.")
        return
    # Show count by min_edge bucket so we can see if it's just one outlier
    # cell or a robust region of parameter space.
    print(f"  Distribution of net-positive cells across grid axes:")
    for axis in ("min_edge", "max_edge", "min_conf", "min_vol", "exclude_micro", "detectors"):
        by_axis: Dict = defaultdict(int)
        for params, _ in positive:
            v = params[axis]
            if isinstance(v, tuple):
                v = "+".join(v)
            by_axis[v] += 1
        # Render with most-frequent first.
        rendered = ", ".join(f"{k}:{n}" for k, n in
                             sorted(by_axis.items(), key=lambda x: -x[1]))
        print(f"    {axis:>14}: {rendered}")


def print_marginal(trades: List[Trade]) -> None:
    """Show one-axis-at-a-time net ROI: for each value of min_vol (with no
    other filter), what does ROI look like?  Same for min_edge / micro_tf.

    This separates "robust filter" (large effect) from "lucky overlap with
    a specific cell". A filter axis that monotonically improves NET ROI
    as you tighten it is a real signal; one that flickers is noise.
    """
    def _roi_under(filter_fn) -> Tuple[Cell, float]:
        c = Cell()
        for t in trades:
            if filter_fn(t):
                c.add(t)
        return c, c.roi_net

    print("\nMarginal effect — one filter axis at a time (no other filters):")

    print("\n  min_vol_24h:")
    for mv in (0, 1_000, 5_000, 50_000, 250_000):
        c, _ = _roi_under(lambda t, mv=mv: t.volume_24h >= mv)
        print(f"    >= ${mv:>7,}: n={c.n:>4}  win={c.win_rate*100:>5.1f}%  "
              f"gross={c.roi_gross*100:>+6.2f}%  NET={c.roi_net*100:>+6.2f}%")

    print("\n  edge_pct (min_edge fixed at 0):")
    for me in (0, 1, 2, 5, 10, 20):
        c, _ = _roi_under(lambda t, me=me: t.edge_pct >= me)
        print(f"    >= {me:>3}%: n={c.n:>4}  win={c.win_rate*100:>5.1f}%  "
              f"gross={c.roi_gross*100:>+6.2f}%  NET={c.roi_net*100:>+6.2f}%")

    print("\n  edge_pct (max_edge ceiling):")
    for me in (10, 20, 50, 100, 1000):
        c, _ = _roi_under(lambda t, me=me: t.edge_pct <= me)
        label = f"<= {me:>3}%" if me < 1000 else "no ceiling"
        print(f"    {label:>10}: n={c.n:>4}  win={c.win_rate*100:>5.1f}%  "
              f"gross={c.roi_gross*100:>+6.2f}%  NET={c.roi_net*100:>+6.2f}%")

    print("\n  exclude micro_tf:")
    for excl in (False, True):
        c, _ = _roi_under(lambda t, excl=excl: not (excl and t.micro_tf))
        label = "yes" if excl else "no (include all)"
        print(f"    {label:>20}: n={c.n:>4}  win={c.win_rate*100:>5.1f}%  "
              f"gross={c.roi_gross*100:>+6.2f}%  NET={c.roi_net*100:>+6.2f}%")

    print("\n  by detector (alone):")
    for det in ("date_expired", "extreme_priced"):
        c, _ = _roi_under(lambda t, det=det: t.detector == det)
        print(f"    {det:>16}: n={c.n:>4}  win={c.win_rate*100:>5.1f}%  "
              f"gross={c.roi_gross*100:>+6.2f}%  NET={c.roi_net*100:>+6.2f}%")


def _cell_to_dict(cell: Cell) -> dict:
    return {
        "n": cell.n,
        "wins": cell.wins,
        "win_rate": round(cell.win_rate, 4),
        "deployed_usd": round(cell.total_size, 2),
        "gross_pnl": round(cell.total_directional, 2),
        "fees_paid": round(cell.total_fees, 2),
        "net_pnl": round(cell.total_net, 2),
        "roi_gross": round(cell.roi_gross, 6),
        "roi_net": round(cell.roi_net, 6),
    }


def evaluate_cell(trades: List[Trade], params: dict) -> Cell:
    cell = Cell()
    for t in trades:
        if matches_filter(t,
                          min_edge=params["min_edge"],
                          max_edge=params["max_edge"],
                          min_conf=params["min_conf"],
                          min_vol=params["min_vol"],
                          exclude_micro=params["exclude_micro"],
                          detectors=params["detectors"]):
            cell.add(t)
    return cell


def collect_summary(trades: List[Trade]) -> dict:
    """Produce the JSONL row written on each --snapshot run.

    Three pieces:
      * baseline: all directional trades with no filter (sanity reference)
      * validated_cell: the fixed VALIDATED_CELL filter, tracked over time
        to see if the original 2026-05-04 finding (NET +1.78%, n=64) holds
        up as the sample grows
      * top_cells: top-3 cells in the current grid, by NET ROI — to see if
        the optimum drifts away from VALIDATED_CELL
    """
    base = Cell()
    for t in trades:
        base.add(t)

    validated = evaluate_cell(trades, VALIDATED_CELL)

    results = sweep(trades)
    ranked = sorted(results, key=lambda r: -r[1].roi_net)
    top_cells = []
    for params, cell in ranked[:3]:
        top_cells.append({
            "filter": {
                "min_edge": params["min_edge"],
                "max_edge": params["max_edge"],
                "min_conf": params["min_conf"],
                "min_vol": params["min_vol"],
                "exclude_micro": params["exclude_micro"],
                "detectors": list(params["detectors"]),
            },
            "stats": _cell_to_dict(cell),
        })

    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "trades_resolved_total": len(trades),
        "baseline": _cell_to_dict(base),
        "validated_cell": {
            "filter": {**VALIDATED_CELL, "detectors": list(VALIDATED_CELL["detectors"])},
            "stats": _cell_to_dict(validated),
        },
        "top_cells": top_cells,
        "n_grid_cells_passing_min_n": len(results),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", action="store_true",
                    help=f"Append a JSON summary line to {HISTORY_JSONL.name} and exit silently. "
                         "Intended for periodic timer-driven walk-forward tracking.")
    args = ap.parse_args()

    if args.snapshot:
        trades = load_trades()
        if not trades:
            return 1
        summary = collect_summary(trades)
        HISTORY_JSONL.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY_JSONL.open("a") as f:
            f.write(json.dumps(summary, separators=(",", ":")) + "\n")
        return 0

    print(f"Loading trades from {CAND_CSV.name} + {RES_CSV.name}...")
    trades = load_trades()
    print(f"Loaded {len(trades)} resolved directional trades "
          f"(date_expired + extreme_priced).\n")

    if not trades:
        print("No trades to sweep — bot may not have produced any directional candidates yet.")
        return 1

    # Headline: unfiltered baseline so we know what we're comparing to.
    base = Cell()
    for t in trades:
        base.add(t)
    print(f"Baseline (no filter, all directional resolved trades):")
    print(f"  n={base.n}  win={base.win_rate*100:.1f}%  "
          f"deployed=${base.total_size:.2f}  "
          f"gross PnL=${base.total_directional:+.2f}  "
          f"fees paid=${base.total_fees:.2f}  "
          f"NET PnL=${base.total_net:+.2f}  ({base.roi_net*100:+.2f}% ROI)\n")

    # Validated-cell tracking — the headline finding from 2026-05-04.
    val = evaluate_cell(trades, VALIDATED_CELL)
    print(f"Validated cell (vol≥$250k AND edge 2-20%, both detectors):")
    print(f"  n={val.n}  win={val.win_rate*100:.1f}%  "
          f"NET ${val.total_net:+.2f}  ({val.roi_net*100:+.2f}% ROI)")
    if HISTORY_JSONL.exists():
        # Show the last few snapshots so the user sees the time-series shape.
        with HISTORY_JSONL.open() as f:
            history = [json.loads(line) for line in f]
        if history:
            print(f"  history: {len(history)} prior snapshots in {HISTORY_JSONL.name}")
            for h in history[-5:]:
                vc = h["validated_cell"]["stats"]
                print(f"    {h['ts']:<22}  n={vc['n']:>4}  NET=${vc['net_pnl']:+.2f}  ROI={vc['roi_net']*100:+.2f}%")
    print()

    print_marginal(trades)
    print()
    print(f"Sweeping grid (min_edge × max_edge × min_conf × min_vol × micro_tf × detector)...")
    results = sweep(trades)
    print()
    print_top(results, top_n=30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
