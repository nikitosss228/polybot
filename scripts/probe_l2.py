#!/usr/bin/env python3
"""L2 cross-event correlation probe.

Loads the curated YAML at ``data/cross_arb.yaml``, fetches current
prices for every referenced event from Gamma, and reports per-pair:

  * the current bid/ask for both legs
  * the gap = bid(subset) - ask(superset). Positive = arb violation.
  * whether gap exceeds the pair's threshold (= surface-worthy)

Two modes:

  ``--snapshot``  — silent, append a JSON line per run to
                   ``logs/cross_arb_snapshots.jsonl`` for trend tracking
  (default)       — pretty-printed table to stdout

Doesn't touch the bot's order pipeline. Run repeatedly (e.g. hourly via
a systemd timer) to see if any chain ever drifts into arb territory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polybot.client import PolyClient
from polybot.cross_arb import (
    DEFAULT_YAML, MarketSnapshot, collect_event_slugs,
    evaluate, fetch_event_snapshots, load_pairs,
)

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
SNAPSHOT_FILE = LOG_DIR / "cross_arb_snapshots.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def run() -> dict:
    pairs = load_pairs()
    if not pairs:
        return {"ts": _now_iso(), "pairs_loaded": 0, "violations": []}

    slugs = collect_event_slugs(pairs)
    async with PolyClient() as pc:
        snapshots = await fetch_event_snapshots(pc.fetch_event_by_slug, slugs)

    violations = []
    pair_states = []
    for p in pairs:
        v = evaluate(p, snapshots)
        # Always record per-pair state for the snapshot, even if no violation
        if p.type in ("subset", "temporal_monotonic"):
            sub = snapshots.get((p.subset.event_slug, p.subset.outcome))
            sup = snapshots.get((p.superset.event_slug, p.superset.outcome))
            pair_states.append({
                "id": p.id,
                "type": p.type,
                "threshold_cents": round(p.threshold * 100, 1),
                "sub_slug": p.subset.event_slug,
                "sub_bid": round(sub.bid, 4) if sub else None,
                "sub_ask": round(sub.ask, 4) if sub else None,
                "sup_slug": p.superset.event_slug,
                "sup_bid": round(sup.bid, 4) if sup else None,
                "sup_ask": round(sup.ask, 4) if sup else None,
                "gap_cents": round((sub.bid - sup.ask) * 100, 2) if sub and sup else None,
                "violation": v is not None,
            })
        if v is not None:
            violations.append({
                "id": v.pair.id,
                "type": v.pair.type,
                "gap_cents": round(v.gap * 100, 2),
                "cheap_slug": v.cheap_event_slug,
                "cheap_outcome": v.cheap_outcome,
                "cheap_ask": round(v.cheap_ask, 4),
                "expensive_slug": v.expensive_event_slug,
                "expensive_outcome": v.expensive_outcome,
                "expensive_bid": round(v.expensive_bid, 4),
                "detail": v.detail,
            })

    return {
        "ts": _now_iso(),
        "pairs_loaded": len(pairs),
        "events_fetched": len(slugs),
        "snapshots": len(snapshots),
        "pair_states": pair_states,
        "violations": violations,
    }


def print_report(summary: dict) -> None:
    print(f"L2 probe @ {summary['ts']}  pairs={summary['pairs_loaded']}  "
          f"events={summary.get('events_fetched', 0)}  violations={len(summary['violations'])}")
    print()
    print(f"{'pair_id':<35} {'sub_bid':>7} {'sup_ask':>7} {'gap':>6}  status")
    print("-" * 75)
    for ps in summary.get("pair_states", []):
        sb = f"{ps['sub_bid']:.3f}" if ps['sub_bid'] is not None else "—"
        sa = f"{ps['sup_ask']:.3f}" if ps['sup_ask'] is not None else "—"
        gap = f"{ps['gap_cents']:+5.1f}¢" if ps['gap_cents'] is not None else "  —  "
        status = "★ ARB!" if ps['violation'] else "ok"
        print(f"  {ps['id']:<33} {sb:>7} {sa:>7} {gap:>7}  {status}")
    if summary["violations"]:
        print()
        print("Violations:")
        for v in summary["violations"]:
            print(f"  ★ {v['id']}: {v['detail']}")
            print(f"    cheap:    {v['cheap_slug']}  {v['cheap_outcome']} ask={v['cheap_ask']}")
            print(f"    expensive:{v['expensive_slug']}  {v['expensive_outcome']} bid={v['expensive_bid']}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", action="store_true",
                    help="Append a JSON summary to logs/cross_arb_snapshots.jsonl and exit silently.")
    args = ap.parse_args()

    summary = await run()

    if args.snapshot:
        SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with SNAPSHOT_FILE.open("a") as f:
            f.write(json.dumps(summary, separators=(",", ":")) + "\n")
        return 0

    print_report(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
