#!/usr/bin/env python3
"""Gate-loosening verdict — compares the latest checkpoint to the 2026-05-02 baseline.

Question: does the simulate.py sweep still show the same pattern after ~5 more days
of data, or did it shift?

  - non_mm_strict vs non_mm_no_ceiling — should still be ~0 delta if the hypothesis
    holds (loosening doesn't help directional detectors)
  - mm_only_strict vs mm_only_no_ceiling — if mm_only_no_ceiling ROI stays ≥30%,
    mm_spread phantom-vs-real question is still open; if it collapses, phantom-PnL
    was the explanation
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path
from typing import Dict, Optional

LOG_DIR = Path("/root/polybot/logs")
BASELINE = LOG_DIR / "checkpoint_2026-05-02_0832.txt"

ROW_RE = re.compile(
    r"^(\w+)\s+(\d+)\s+([\d.]+)%\s+\$\s*([+-][\d.]+)\s+"
    r"\$\s*([+-][\d.]+)\s+\$\s*([\d.]+)\s+([+-][\d.]+)%"
)

WATCH = [
    "strict_gate",
    "non_mm_strict",
    "non_mm_no_ceiling",
    "mm_only_strict",
    "mm_only_no_ceiling",
    "loose_edge_50",
    "no_edge_ceiling",
]


def parse_scenarios(text: str) -> Dict[str, Dict[str, float]]:
    out: Dict[str, Dict[str, float]] = {}
    in_section = False
    for line in text.splitlines():
        if "### simulate.py (filter scenarios) ###" in line:
            in_section = True
            continue
        if in_section and line.startswith("###"):
            break
        if not in_section:
            continue
        m = ROW_RE.match(line.strip())
        if not m:
            continue
        name, n, win, avg_pnl, total_pnl, deployed, roi = m.groups()
        out[name] = {
            "n": int(n),
            "win_pct": float(win),
            "total_pnl": float(total_pnl),
            "deployed": float(deployed),
            "roi_pct": float(roi),
        }
    return out


def latest_checkpoint(after: Path) -> Optional[Path]:
    """Pick the lex-greatest checkpoint that's strictly newer than `after`.

    Filenames are checkpoint_YYYY-MM-DD_HHMM.txt so lex order = chronological order.
    """
    candidates = sorted(LOG_DIR.glob("checkpoint_*.txt"))
    candidates = [p for p in candidates if p.name > after.name]
    return candidates[-1] if candidates else None


def fmt_delta(now: float, base: float, suffix: str = "") -> str:
    delta = now - base
    sign = "+" if delta >= 0 else ""
    return f"{now:.2f}{suffix} (Δ {sign}{delta:.2f}{suffix} vs {base:.2f}{suffix})"


def main() -> int:
    if not BASELINE.exists():
        print(f"ERROR: baseline {BASELINE} missing", file=sys.stderr)
        return 1
    latest = latest_checkpoint(after=BASELINE)
    if latest is None:
        print(f"ERROR: no checkpoint newer than baseline {BASELINE.name} found",
              file=sys.stderr)
        return 1

    base_rows = parse_scenarios(BASELINE.read_text())
    latest_rows = parse_scenarios(latest.read_text())

    if not latest_rows:
        print(f"ERROR: latest checkpoint {latest.name} has no parseable simulate rows",
              file=sys.stderr)
        return 1

    out_lines = []
    out_lines.append(f"Polybot gate-loosening verdict — {dt.datetime.now(dt.timezone.utc).isoformat()}Z")
    out_lines.append(f"Baseline: {BASELINE.name}")
    out_lines.append(f"Latest:   {latest.name}")
    out_lines.append("")
    out_lines.append(f"{'scenario':<22} {'n':>6} {'roi%':>22} {'win%':>16}")
    out_lines.append("-" * 72)
    for name in WATCH:
        b = base_rows.get(name)
        n = latest_rows.get(name)
        if b is None or n is None:
            out_lines.append(f"{name:<22}  (missing in {'baseline' if b is None else 'latest'})")
            continue
        out_lines.append(
            f"{name:<22} {n['n']:>6}  "
            f"{fmt_delta(n['roi_pct'], b['roi_pct'], '%'):>22}  "
            f"{fmt_delta(n['win_pct'], b['win_pct'], '%'):>16}"
        )

    out_lines.append("")
    out_lines.append("Hypothesis check:")

    nm_s = latest_rows.get("non_mm_strict")
    nm_n = latest_rows.get("non_mm_no_ceiling")
    mm_s = latest_rows.get("mm_only_strict")
    mm_n = latest_rows.get("mm_only_no_ceiling")

    if nm_s and nm_n:
        gap = nm_n["roi_pct"] - nm_s["roi_pct"]
        verdict = "HOLDS" if abs(gap) < 1.0 else "BROKEN"
        out_lines.append(
            f"  H1 (loosening doesn't help directional): {verdict} — "
            f"non_mm gap {gap:+.2f}% (held if |Δ| < 1%)"
        )
    if mm_s and mm_n:
        out_lines.append(
            f"  H2 (mm_spread no-ceiling stays ROI ≥30%): "
            f"{'HOLDS' if mm_n['roi_pct'] >= 30.0 else 'COLLAPSED'} — "
            f"mm_only_no_ceiling ROI now {mm_n['roi_pct']:.2f}% "
            f"(was {base_rows['mm_only_no_ceiling']['roi_pct']:.2f}%)"
        )
        out_lines.append(
            f"     mm_only_strict ROI now {mm_s['roi_pct']:.2f}% "
            f"(was {base_rows['mm_only_strict']['roi_pct']:.2f}%) "
            f"— if this also collapsed, phantom-PnL was the whole story"
        )

    report = "\n".join(out_lines) + "\n"
    out_path = LOG_DIR / f"verdict_{dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d_%H%M')}.txt"
    out_path.write_text(report)
    print(report)
    print(f"Verdict written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
