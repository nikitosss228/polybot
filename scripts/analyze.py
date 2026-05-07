#!/usr/bin/env python3
"""Polybot strategy analyzer — paper PnL over the study CSVs.

Reads ``logs/candidates.csv`` and ``logs/price_track.csv`` and reports how
the scanner's candidates have moved since they were first surfaced. Without
ground-truth resolutions we use the **most recent observed price** as a
proxy for the realised outcome and report a paper PnL:

    paper_pnl = size_usd * (current_price - entry_price) / entry_price

Buckets the report by detector, edge%, confidence, and 24h volume so we
can tell *which* slice of the strategy is actually predictive. Designed
to run as a oneshot systemd service every few hours; writes a dated
file under ``logs/`` and also prints to stdout for journald.
"""

from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
CAND_CSV = LOG_DIR / "candidates.csv"
TRACK_CSV = LOG_DIR / "price_track.csv"
RESOLUTIONS_CSV = LOG_DIR / "resolutions.csv"


# ---------- helpers ---------------------------------------------------------


def _f(value: str, default: float = 0.0) -> float:
    try:
        return float(value) if value not in ("", None) else default
    except ValueError:
        return default


def _classify_detector(reason: str) -> str:
    if reason.startswith("end_date"):
        return "date_expired"
    if reason.startswith("extreme price"):
        return "extreme_priced"
    if reason.startswith("bundle "):
        return "bundle_arb"
    if reason.startswith("mm "):
        return "mm_spread"
    return "unknown"


def _bucket(value: float, edges: List[float], labels: List[str]) -> str:
    """Return the label for the bucket containing ``value``.

    ``edges`` defines the *upper* bounds of all but the last bucket.
    """
    for edge, label in zip(edges, labels):
        if value < edge:
            return label
    return labels[-1]


EDGE_BUCKETS = ([2.0, 5.0, 10.0, 20.0],
                ["<2%", "2-5%", "5-10%", "10-20%", ">=20%"])
CONF_BUCKETS = ([0.3, 0.5, 0.7, 0.9],
                ["<0.3", "0.3-0.5", "0.5-0.7", "0.7-0.9", ">=0.9"])
VOL_BUCKETS = ([5_000.0, 50_000.0, 250_000.0],
               ["<5k", "5-50k", "50-250k", ">=250k"])


# Strict-gate proposal — flags surfaced from 14:19 UTC analysis on 2026-04-30:
#   * micro-timeframe crypto markets drift to 0/1 within minutes (high-variance,
#     no real predictive edge for the bot);
#   * edge_pct >= 20% behaves like adverse-selection (loss-making bucket);
#   * confidence < 0.3 is a noise bucket (~40% win, negative PnL).
# Strict-pass = none of the three flags fired. Used for instrumentation only —
# the live scanner still surfaces the wide net so we can keep comparing.

MICRO_TF_PATTERNS = (
    re.compile(r"-updown-\d+[mhd]-"),          # btc-updown-5m-1777..., eth-updown-15m-...
    re.compile(r"-up-or-down-.*-\d+(am|pm)-et$"),  # ethereum-up-or-down-april-30-2026-4am-et
)
HIGH_EDGE_THRESHOLD = 20.0
LOW_CONF_THRESHOLD = 0.3


def is_micro_tf(slug: str) -> bool:
    return any(p.search(slug) for p in MICRO_TF_PATTERNS)


def strict_flags(cand: Dict[str, str]) -> Tuple[bool, bool, bool]:
    """(high_edge, low_conf, micro_tf) — True means the flag *fires* (bad)."""
    high_edge = _f(cand["edge_pct"]) >= HIGH_EDGE_THRESHOLD
    low_conf = _f(cand["confidence"]) < LOW_CONF_THRESHOLD
    micro_tf = is_micro_tf(cand.get("slug", ""))
    return high_edge, low_conf, micro_tf


def passes_strict(cand: Dict[str, str]) -> bool:
    return not any(strict_flags(cand))


def fail_reason(cand: Dict[str, str]) -> str:
    flags = strict_flags(cand)
    fired = [name for name, fired in zip(("high_edge", "low_conf", "micro_tf"), flags) if fired]
    if not fired:
        return "pass"
    if len(fired) > 1:
        return "multi_flag"
    return fired[0]


# ---------- data load -------------------------------------------------------


def load_first_candidates() -> Dict[str, Dict[str, str]]:
    """For each token_id, the *earliest* candidates.csv row."""
    if not CAND_CSV.exists():
        return {}
    out: Dict[str, Dict[str, str]] = {}
    with CAND_CSV.open(newline="") as f:
        for row in csv.DictReader(f):
            tid = row["token_id"]
            existing = out.get(tid)
            if existing is None or int(row["tick_id"]) < int(existing["tick_id"]):
                out[tid] = row
    return out


def load_latest_prices() -> Dict[str, Dict[str, str]]:
    """For each token_id, the *latest* price_track.csv row."""
    if not TRACK_CSV.exists():
        return {}
    out: Dict[str, Dict[str, str]] = {}
    with TRACK_CSV.open(newline="") as f:
        for row in csv.DictReader(f):
            tid = row["token_id"]
            existing = out.get(tid)
            if existing is None or int(row["tick_id"]) > int(existing["tick_id"]):
                out[tid] = row
    return out


def load_resolutions() -> Dict[str, float]:
    """For each confidently-resolved token_id, its terminal payout (0.0 or 1.0).

    A row is *confident* iff (a) it has a non-empty ``last_seen_ts`` (so the
    token was actually observed after its first appearance — guards against
    the study.py default-0.0 case where a token vanished immediately and
    ``last_seen_price`` defaulted to zero) and (b) its ``resolution_reason``
    is ``near_one`` (last observed >=0.95 → assume payout 1.0) or
    ``near_zero`` (last observed <=0.05 → assume payout 0.0). Rows with
    ``fell_out_of_feed`` are dropped — last seen between 0.05 and 0.95 means
    we don't know which side won.
    """
    if not RESOLUTIONS_CSV.exists():
        return {}
    out: Dict[str, float] = {}
    with RESOLUTIONS_CSV.open(newline="") as f:
        for row in csv.DictReader(f):
            if not row.get("last_seen_ts"):
                continue
            reason = row.get("resolution_reason", "")
            if reason == "near_one":
                out[row["token_id"]] = 1.0
            elif reason == "near_zero":
                out[row["token_id"]] = 0.0
    return out


# ---------- aggregation -----------------------------------------------------


class BucketStats:
    __slots__ = ("count", "sum_entry", "sum_current", "sum_drift_pct",
                 "sum_pnl", "sum_size", "wins", "losses", "no_move")

    def __init__(self) -> None:
        self.count = 0
        self.sum_entry = 0.0
        self.sum_current = 0.0
        self.sum_drift_pct = 0.0
        self.sum_pnl = 0.0
        self.sum_size = 0.0
        self.wins = 0
        self.losses = 0
        self.no_move = 0

    def add(self, entry: float, current: float, size: float,
            pnl: float, drift_pct: float) -> None:
        self.count += 1
        self.sum_entry += entry
        self.sum_current += current
        self.sum_drift_pct += drift_pct
        self.sum_pnl += pnl
        self.sum_size += size
        if current > entry:
            self.wins += 1
        elif current < entry:
            self.losses += 1
        else:
            self.no_move += 1

    def line(self, label: str) -> str:
        if self.count == 0:
            return f"  {label:<14}  n=0"
        avg_entry = self.sum_entry / self.count
        avg_current = self.sum_current / self.count
        avg_drift = self.sum_drift_pct / self.count
        avg_pnl = self.sum_pnl / self.count
        win_rate = self.wins / self.count * 100.0
        return (
            f"  {label:<14}  n={self.count:>4}  "
            f"entry={avg_entry:.3f}  now={avg_current:.3f}  "
            f"drift={avg_drift:+6.2f}%  win={win_rate:5.1f}%  "
            f"avg_pnl=${avg_pnl:+6.3f}  total_pnl=${self.sum_pnl:+8.2f}  "
            f"deployed=${self.sum_size:7.2f}"
        )


def compute_pnl(cand: Dict[str, str], entry: float, current: float,
                size: float) -> Tuple[float, float]:
    """Return (paper_pnl, drift_pct) for a (candidate, latest-price) pair.

    PnL accounting is detector-aware. ``mm_spread`` flags wide-spread
    market-making opportunities; its ``entry_price`` is recorded as
    ``best_bid`` (where the MM would post). Treating that as a directional
    buy-and-hold to ``current`` produces phantom returns — when an
    illiquid 0.03 longshot resolves to 1.0, the directional formula
    booked ~3200% PnL even though no MM strategy would hold to
    resolution. The realistic per-observation upper bound is one
    round-trip's spread capture: ``size * edge_pct / 100``, where
    ``edge_pct`` was computed at surface time as ``(spread - 2¢) / mid``.
    """
    if entry <= 0:
        return 0.0, 0.0
    drift_pct = (current - entry) / entry * 100.0
    if _classify_detector(cand["reason"]) == "mm_spread":
        edge = _f(cand["edge_pct"])
        return size * edge / 100.0, drift_pct
    return size * (current - entry) / entry, drift_pct


def aggregate(joined: Iterable[Tuple[Dict[str, str], Dict[str, str]]],
              key_fn) -> Dict[str, BucketStats]:
    out: Dict[str, BucketStats] = defaultdict(BucketStats)
    for cand, track in joined:
        entry = _f(cand["entry_price"])
        current = _f(track["outcome_price"])
        size = _f(cand["size_usd"])
        pnl, drift_pct = compute_pnl(cand, entry, current, size)
        out[key_fn(cand, track)].add(entry, current, size, pnl, drift_pct)
    return out


# ---------- report ----------------------------------------------------------


def render(buckets: Dict[str, BucketStats], order: Optional[List[str]] = None) -> List[str]:
    keys = order if order is not None else sorted(buckets.keys())
    lines = []
    overall = BucketStats()
    for k in keys:
        s = buckets.get(k)
        if s is None:
            continue
        lines.append(s.line(k))
        overall.count += s.count
        overall.sum_entry += s.sum_entry
        overall.sum_current += s.sum_current
        overall.sum_drift_pct += s.sum_drift_pct
        overall.sum_pnl += s.sum_pnl
        overall.sum_size += s.sum_size
        overall.wins += s.wins
        overall.losses += s.losses
        overall.no_move += s.no_move
    lines.append("  " + "-" * 110)
    lines.append(overall.line("ALL"))
    return lines


def main() -> int:
    cands = load_first_candidates()
    prices = load_latest_prices()
    resolutions = load_resolutions()
    joined: List[Tuple[Dict[str, str], Dict[str, str]]] = []
    for tid, cand in cands.items():
        track = prices.get(tid)
        if track is None:
            continue
        joined.append((cand, track))

    now = datetime.now(timezone.utc)
    out_path = LOG_DIR / f"analysis_{now.strftime('%Y-%m-%d_%H%M')}.txt"
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    lines: List[str] = []
    lines.append(f"Polybot strategy analysis — {now.isoformat(timespec='seconds')}")
    lines.append(f"Candidates seen (unique token_ids):  {len(cands):>5}")
    lines.append(f"Tokens with at least one price update: {len(joined):>5}")
    lines.append(f"Tokens stale (no price update yet):   {len(cands) - len(joined):>5}")
    lines.append(f"Tokens with confident resolution:    {sum(1 for c,_ in joined if c['token_id'] in resolutions):>5}")
    lines.append("PnL model: directional (size * (current-entry)/entry) for all "
                 "detectors EXCEPT mm_spread, which uses round-trip spread "
                 "capture (size * edge_pct/100) — see compute_pnl() docstring.")
    lines.append("")

    if not joined:
        lines.append("Not enough data yet — need at least one tick after the candidate's "
                     "first appearance to compute drift. Come back after a few ticks.")
        report = "\n".join(lines) + "\n"
        out_path.write_text(report)
        sys.stdout.write(report)
        return 0

    # By detector ------------------------------------------------------------
    lines.append("By detector:")
    by_det = aggregate(joined, lambda c, t: _classify_detector(c["reason"]))
    lines.extend(render(by_det, order=["date_expired", "extreme_priced", "bundle_arb", "mm_spread", "unknown"]))
    lines.append("")

    # By edge_pct bucket -----------------------------------------------------
    lines.append("By edge_pct bucket (at entry):")
    edges, edge_labels = EDGE_BUCKETS
    by_edge = aggregate(joined,
                        lambda c, t: _bucket(_f(c["edge_pct"]), edges, edge_labels))
    lines.extend(render(by_edge, order=edge_labels))
    lines.append("")

    # By confidence bucket ---------------------------------------------------
    lines.append("By confidence bucket (at entry):")
    cedges, clabels = CONF_BUCKETS
    by_conf = aggregate(joined,
                        lambda c, t: _bucket(_f(c["confidence"]), cedges, clabels))
    lines.extend(render(by_conf, order=clabels))
    lines.append("")

    # By 24h volume bucket ---------------------------------------------------
    lines.append("By volume_24h bucket (at entry):")
    vedges, vlabels = VOL_BUCKETS
    by_vol = aggregate(joined,
                       lambda c, t: _bucket(_f(c["volume_24h"]), vedges, vlabels))
    lines.extend(render(by_vol, order=vlabels))
    lines.append("")

    # Strict-gate proposal --------------------------------------------------
    # Comparison of "what we capture today" vs "what would survive a tightened
    # gate" — instrumentation only, no live behavior change. See
    # MICRO_TF_PATTERNS / HIGH_EDGE_THRESHOLD / LOW_CONF_THRESHOLD up top.
    strict_joined = [(c, t) for (c, t) in joined if passes_strict(c)]
    lines.append(
        f"Strict gate (edge<{HIGH_EDGE_THRESHOLD:.0f}% AND "
        f"conf>={LOW_CONF_THRESHOLD} AND not micro-tf):"
    )
    by_strict = aggregate(joined, lambda c, t: "pass" if passes_strict(c) else "fail")
    lines.extend(render(by_strict, order=["pass", "fail"]))
    lines.append("")

    lines.append("Strict-fail breakdown (which flag fired):")
    by_fail = aggregate(joined, lambda c, t: fail_reason(c))
    lines.extend(render(by_fail, order=["high_edge", "low_conf", "micro_tf", "multi_flag"]))
    lines.append("")

    if strict_joined:
        lines.append("STRICT SUBSET — by detector:")
        lines.extend(render(
            aggregate(strict_joined, lambda c, t: _classify_detector(c["reason"])),
            order=["date_expired", "extreme_priced", "bundle_arb", "mm_spread", "unknown"],
        ))
        lines.append("")

        lines.append("STRICT SUBSET — by edge_pct bucket:")
        lines.extend(render(
            aggregate(strict_joined, lambda c, t: _bucket(_f(c["edge_pct"]), edges, edge_labels)),
            order=edge_labels,
        ))
        lines.append("")

        lines.append("STRICT SUBSET — by confidence bucket:")
        lines.extend(render(
            aggregate(strict_joined, lambda c, t: _bucket(_f(c["confidence"]), cedges, clabels)),
            order=clabels,
        ))
        lines.append("")

        lines.append("STRICT SUBSET — by volume_24h bucket:")
        lines.extend(render(
            aggregate(strict_joined, lambda c, t: _bucket(_f(c["volume_24h"]), vedges, vlabels)),
            order=vlabels,
        ))
        lines.append("")

    # Realized subset --------------------------------------------------------
    # Replace the latest-observed-price proxy with the inferred terminal payout
    # (1.0 for near_one, 0.0 for near_zero; fell_out_of_feed and never-observed
    # rows are excluded — see load_resolutions). This is the honest realized
    # PnL for directional detectors. For mm_spread the per-trade model
    # (size * edge_pct / 100) does not depend on terminal price, so its
    # realized number equals its paper number — fill rate remains the open
    # question for mm_spread.
    def _with_terminal(track: Dict[str, str], terminal: float) -> Dict[str, str]:
        t = dict(track)
        t["outcome_price"] = f"{terminal:.6f}"
        return t

    realized_joined = [
        (cand, _with_terminal(track, resolutions[cand["token_id"]]))
        for (cand, track) in joined
        if cand["token_id"] in resolutions
    ]
    if realized_joined:
        lines.append(
            f"REALIZED SUBSET — confident resolutions only "
            f"(n={len(realized_joined)} of {len(joined)}, "
            f"{len(realized_joined) * 100 / len(joined):.0f}%):"
        )
        lines.append("REALIZED — by detector:")
        lines.extend(render(
            aggregate(realized_joined, lambda c, t: _classify_detector(c["reason"])),
            order=["date_expired", "extreme_priced", "bundle_arb", "mm_spread", "unknown"],
        ))
        lines.append("")

        lines.append("REALIZED — by edge_pct bucket:")
        lines.extend(render(
            aggregate(realized_joined, lambda c, t: _bucket(_f(c["edge_pct"]), edges, edge_labels)),
            order=edge_labels,
        ))
        lines.append("")

        realized_strict = [(c, t) for (c, t) in realized_joined if passes_strict(c)]
        if realized_strict:
            lines.append("REALIZED STRICT SUBSET — by detector:")
            lines.extend(render(
                aggregate(realized_strict, lambda c, t: _classify_detector(c["reason"])),
                order=["date_expired", "extreme_priced", "bundle_arb", "mm_spread", "unknown"],
            ))
            lines.append("")

    # Top movers -------------------------------------------------------------
    lines.append("Top 10 winners (highest paper PnL):")
    moves = []
    for cand, track in joined:
        entry = _f(cand["entry_price"])
        current = _f(track["outcome_price"])
        size = _f(cand["size_usd"])
        if entry <= 0:
            continue
        pnl, _ = compute_pnl(cand, entry, current, size)
        moves.append((pnl, cand, current))
    moves.sort(key=lambda x: x[0], reverse=True)
    for pnl, cand, current in moves[:10]:
        lines.append(
            f"  pnl=${pnl:+6.3f}  {cand['outcome_name']:<6} "
            f"{_f(cand['entry_price']):.3f} -> {current:.3f}  "
            f"{cand['slug'][:60]}"
        )
    lines.append("")
    lines.append("Top 10 losers (lowest paper PnL):")
    for pnl, cand, current in moves[-10:][::-1]:
        lines.append(
            f"  pnl=${pnl:+6.3f}  {cand['outcome_name']:<6} "
            f"{_f(cand['entry_price']):.3f} -> {current:.3f}  "
            f"{cand['slug'][:60]}"
        )

    report = "\n".join(lines) + "\n"
    out_path.write_text(report)
    sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
