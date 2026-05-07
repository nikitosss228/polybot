#!/usr/bin/env python3
"""Filter simulator — replays study CSVs under arbitrary filter combos.

Lets us A/B-test gating ideas (edge bands, volume floors, detector subsets)
without restarting the bot or waiting more days. Reuses analyze.py loaders
and stats so numbers line up with the canonical analyzer.

Run modes:

  simulate.py                   # comparison table across preset scenarios
  simulate.py --low-conf-probe  # breakdown of the low_conf strict-fail bucket
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyze  # noqa: E402


@dataclass
class Filter:
    name: str
    edge_min: Optional[float] = None
    edge_max: Optional[float] = None
    conf_min: Optional[float] = None
    vol_min: Optional[float] = None
    exclude_detectors: Tuple[str, ...] = ()
    exclude_micro_tf: bool = True

    def keep(self, cand: Dict[str, str]) -> bool:
        edge = analyze._f(cand["edge_pct"])
        conf = analyze._f(cand["confidence"])
        vol = analyze._f(cand["volume_24h"])
        det = analyze._classify_detector(cand["reason"])
        slug = cand.get("slug", "")
        if self.edge_min is not None and edge < self.edge_min:
            return False
        if self.edge_max is not None and edge >= self.edge_max:
            return False
        if self.conf_min is not None and conf < self.conf_min:
            return False
        if self.vol_min is not None and vol < self.vol_min:
            return False
        if det in self.exclude_detectors:
            return False
        if self.exclude_micro_tf and analyze.is_micro_tf(slug):
            return False
        return True


_NON_MM = ("date_expired", "extreme_priced", "bundle_arb", "unknown")
_MM_ONLY = ("mm_spread",)

SCENARIOS: List[Filter] = [
    Filter(name="baseline_all",
           exclude_micro_tf=False),
    Filter(name="strict_gate",
           edge_max=20.0, conf_min=0.3),
    Filter(name="edge_5_10_only",
           edge_min=5.0, edge_max=10.0, conf_min=0.3),
    Filter(name="edge_2_10",
           edge_min=2.0, edge_max=10.0, conf_min=0.3),
    Filter(name="vol_50k_floor",
           edge_max=20.0, conf_min=0.3, vol_min=50_000.0),
    Filter(name="no_extreme",
           edge_max=20.0, conf_min=0.3, exclude_detectors=("extreme_priced",)),
    Filter(name="combo_tight",
           edge_min=2.0, edge_max=10.0, conf_min=0.3, vol_min=50_000.0),
    Filter(name="combo_very_tight",
           edge_min=5.0, edge_max=10.0, conf_min=0.5, vol_min=50_000.0),
    # --- gate-loosening sweep (test whether 20% edge cap is leaving money on table) ---
    Filter(name="loose_edge_50",
           edge_max=50.0, conf_min=0.3),
    Filter(name="loose_edge_100",
           edge_max=100.0, conf_min=0.3),
    Filter(name="no_edge_ceiling",
           conf_min=0.3),
    # --- detector-isolation (split mm_spread, where PnL = size*edge/100, from directional) ---
    Filter(name="mm_only_strict",
           edge_max=20.0, conf_min=0.3, exclude_detectors=_NON_MM),
    Filter(name="mm_only_no_ceiling",
           conf_min=0.3, exclude_detectors=_NON_MM),
    Filter(name="non_mm_strict",
           edge_max=20.0, conf_min=0.3, exclude_detectors=_MM_ONLY),
    Filter(name="non_mm_no_ceiling",
           conf_min=0.3, exclude_detectors=_MM_ONLY),
]


def run_scenarios(joined) -> List[Tuple[Filter, analyze.BucketStats]]:
    out = []
    for f in SCENARIOS:
        stats = analyze.BucketStats()
        for cand, track in joined:
            if not f.keep(cand):
                continue
            entry = analyze._f(cand["entry_price"])
            current = analyze._f(track["outcome_price"])
            size = analyze._f(cand["size_usd"])
            pnl, drift_pct = analyze.compute_pnl(cand, entry, current, size)
            stats.add(entry, current, size, pnl, drift_pct)
        out.append((f, stats))
    return out


def print_comparison(rows: List[Tuple[Filter, analyze.BucketStats]]) -> None:
    print(f"{'scenario':<22} {'n':>5} {'win%':>6} {'avg_pnl':>9} "
          f"{'total_pnl':>10} {'deployed':>10} {'roi%':>7}")
    print("-" * 76)
    for f, s in rows:
        if s.count == 0:
            print(f"{f.name:<22} {'0':>5}")
            continue
        win = s.wins / s.count * 100.0
        avg_pnl = s.sum_pnl / s.count
        roi = (s.sum_pnl / s.sum_size * 100.0) if s.sum_size > 0 else 0.0
        print(f"{f.name:<22} {s.count:>5} {win:>5.1f}% "
              f"${avg_pnl:>+7.3f} ${s.sum_pnl:>+8.2f} "
              f"${s.sum_size:>8.2f} {roi:>+6.2f}%")


# ---------- low_conf probe -------------------------------------------------


def low_conf_strict_fail(cand: Dict[str, str]) -> bool:
    """The exact subset analyze.py labels as low_conf strict-fail (only that flag fires)."""
    high_edge, low_conf, micro_tf = analyze.strict_flags(cand)
    return low_conf and not high_edge and not micro_tf


def probe_low_conf(joined) -> None:
    subset = [(c, t) for c, t in joined if low_conf_strict_fail(c)]
    if not subset:
        print("low_conf strict-fail bucket is empty.")
        return

    print(f"low_conf-only strict-fail subset: n={len(subset)}\n")

    # By detector
    print("By detector:")
    by_det = analyze.aggregate(subset,
                               lambda c, t: analyze._classify_detector(c["reason"]))
    for line in analyze.render(by_det, order=["date_expired", "extreme_priced", "unknown"]):
        print(line)
    print()

    # By edge bucket
    edges, edge_labels = analyze.EDGE_BUCKETS
    print("By edge_pct bucket:")
    by_edge = analyze.aggregate(
        subset, lambda c, t: analyze._bucket(analyze._f(c["edge_pct"]), edges, edge_labels))
    for line in analyze.render(by_edge, order=edge_labels):
        print(line)
    print()

    # By volume bucket
    vedges, vlabels = analyze.VOL_BUCKETS
    print("By volume_24h bucket:")
    by_vol = analyze.aggregate(
        subset, lambda c, t: analyze._bucket(analyze._f(c["volume_24h"]), vedges, vlabels))
    for line in analyze.render(by_vol, order=vlabels):
        print(line)
    print()

    # Confidence distribution within the bucket (all are <0.3 by construction)
    print("Confidence distribution within bucket (all <0.3):")
    fine_edges = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30]
    fine_labels = ["<0.05", "0.05-0.10", "0.10-0.15", "0.15-0.20", "0.20-0.25", "0.25-0.30"]
    by_fine = analyze.aggregate(
        subset, lambda c, t: analyze._bucket(analyze._f(c["confidence"]), fine_edges, fine_labels))
    for line in analyze.render(by_fine, order=fine_labels):
        print(line)
    print()

    # Top winners and losers in the subset for spot-check
    moves = []
    for cand, track in subset:
        entry = analyze._f(cand["entry_price"])
        current = analyze._f(track["outcome_price"])
        size = analyze._f(cand["size_usd"])
        if entry <= 0:
            continue
        pnl = size * (current - entry) / entry
        moves.append((pnl, cand, current))
    moves.sort(key=lambda x: x[0], reverse=True)
    print("Top 5 winners in low_conf bucket:")
    for pnl, cand, current in moves[:5]:
        print(f"  pnl=${pnl:+6.3f}  conf={analyze._f(cand['confidence']):.2f} "
              f"edge={analyze._f(cand['edge_pct']):.1f}%  "
              f"{cand['outcome_name']:<6} {analyze._f(cand['entry_price']):.3f}->{current:.3f}  "
              f"{cand['slug'][:55]}")
    print("Top 5 losers in low_conf bucket:")
    for pnl, cand, current in moves[-5:][::-1]:
        print(f"  pnl=${pnl:+6.3f}  conf={analyze._f(cand['confidence']):.2f} "
              f"edge={analyze._f(cand['edge_pct']):.1f}%  "
              f"{cand['outcome_name']:<6} {analyze._f(cand['entry_price']):.3f}->{current:.3f}  "
              f"{cand['slug'][:55]}")


# ---------- main -----------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--low-conf-probe", action="store_true",
                    help="Drill into the low_conf strict-fail subset instead of the comparison table.")
    args = ap.parse_args()

    cands = analyze.load_first_candidates()
    prices = analyze.load_latest_prices()
    joined = [(c, prices[t]) for t, c in cands.items() if t in prices]
    print(f"Loaded {len(joined)} token observations "
          f"(of {len(cands)} candidates).\n")

    if args.low_conf_probe:
        probe_low_conf(joined)
    else:
        rows = run_scenarios(joined)
        print_comparison(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
