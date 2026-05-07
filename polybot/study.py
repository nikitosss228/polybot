"""Study-mode logging: per-tick CSV snapshots for offline strategy validation.

In dry-run we don't risk capital — the goal is to capture enough data that
later we can answer questions like:

  * Of the candidates the scanner surfaced, how did their prices evolve?
  * Did the predicted edge realise (price → 1.0 by end_date)?
  * Which detector / confidence band actually predicted profitable moves?

Two append-only CSVs live under logs/:

  * ``candidates.csv``  — one row per (tick, candidate) the scanner surfaced.
  * ``price_track.csv`` — one row per tick per *every* token previously seen
    as a candidate, for as long as the token is still in active markets.

State (tick counter + tracked-token registry with first-seen ts/price) lives
in ``logs/study_state.json`` so restarts pick up where we left off.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .config import PROJECT_ROOT
from .logger import get_logger
from .models import Candidate, Market, Outcome

log = get_logger(__name__)

LOG_DIR = PROJECT_ROOT / "logs"
CANDIDATES_CSV = LOG_DIR / "candidates.csv"
PRICE_TRACK_CSV = LOG_DIR / "price_track.csv"
RESOLUTIONS_CSV = LOG_DIR / "resolutions.csv"
STATE_FILE = LOG_DIR / "study_state.json"

# Header is frozen for backward-compat with the running systemd bot's
# existing candidates.csv (43k+ rows of baseline data we don't want to
# invalidate). Detector type is recoverable from the ``reason`` prefix;
# bundle-arb's NO leg lives only in memory + polybot.log for now.
CANDIDATES_HEADER = [
    "ts", "tick_id", "condition_id", "slug", "question",
    "outcome_name", "token_id",
    "entry_price", "edge_pct", "confidence", "score", "size_usd",
    "days_to_end", "volume_24h", "liquidity", "spread",
    "best_bid", "best_ask", "reason",
]

PRICE_TRACK_HEADER = [
    "ts", "tick_id", "token_id", "slug", "outcome_name",
    "outcome_price", "best_bid", "best_ask", "spread",
    "volume_24h", "days_to_end",
    "first_seen_ts", "first_seen_price",
]

RESOLUTIONS_HEADER = [
    "ts", "tick_id", "token_id", "slug", "outcome_name",
    "first_seen_ts", "first_seen_price",
    "last_seen_ts", "last_seen_price",
    "missing_ticks",          # how many consecutive ticks it was absent
    "resolution_reason",      # "fell_out_of_feed" / "near_zero" / "near_one"
]

# After this many consecutive ticks where a tracked token does not appear in
# the active-markets feed, treat it as "resolved" (closed/archived) and
# emit a resolutions.csv row using the last-known price as a proxy for the
# realised outcome. Set conservatively — markets briefly drop below the vol
# floor and reappear all the time.
RESOLVE_AFTER_MISSING_TICKS = 6


@dataclass
class TrackedToken:
    first_seen_ts: str
    first_seen_price: float
    slug: str
    outcome_name: str
    last_seen_ts: str = ""
    last_seen_price: float = 0.0
    missing_count: int = 0


def _ensure_header(path: Path, header: List[str]) -> None:
    if path.exists() and path.stat().st_size > 0:
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        csv.writer(f).writerow(header)


def _fmt_days(d: Optional[float]) -> str:
    return f"{d:.4f}" if d is not None else ""


def _classify_resolution(last_price: float) -> str:
    if last_price >= 0.95:
        return "near_one"
    if last_price <= 0.05:
        return "near_zero"
    return "fell_out_of_feed"


class StudyLogger:
    """Append-only CSV logger for dry-run study mode."""

    def __init__(self) -> None:
        self.tick_id: int = 0
        self.tracked: Dict[str, TrackedToken] = {}
        self._load()
        _ensure_header(CANDIDATES_CSV, CANDIDATES_HEADER)
        _ensure_header(PRICE_TRACK_CSV, PRICE_TRACK_HEADER)
        _ensure_header(RESOLUTIONS_CSV, RESOLUTIONS_HEADER)

    def _load(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            blob = json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to load study state (%s); starting fresh", exc)
            return
        self.tick_id = int(blob.get("tick_id", 0))
        for tid, raw in (blob.get("tracked_tokens") or {}).items():
            self.tracked[tid] = TrackedToken(
                first_seen_ts=raw.get("first_seen_ts", ""),
                first_seen_price=float(raw.get("first_seen_price", 0.0)),
                slug=raw.get("slug", ""),
                outcome_name=raw.get("outcome_name", ""),
                last_seen_ts=raw.get("last_seen_ts", ""),
                last_seen_price=float(raw.get("last_seen_price", 0.0)),
                missing_count=int(raw.get("missing_count", 0)),
            )

    def _save(self) -> None:
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            blob = {
                "tick_id": self.tick_id,
                "tracked_tokens": {
                    tid: t.__dict__ for tid, t in self.tracked.items()
                },
            }
            STATE_FILE.write_text(json.dumps(blob, indent=2))
        except OSError as exc:
            log.warning("Failed to persist study state: %s", exc)

    def begin_tick(self) -> str:
        self.tick_id += 1
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def write_candidates(
        self,
        ts: str,
        candidates: Iterable[Candidate],
        sizes_by_token: Dict[str, float],
    ) -> int:
        rows: List[List] = []
        for c in candidates:
            tid = c.outcome.token_id
            if tid not in self.tracked:
                self.tracked[tid] = TrackedToken(
                    first_seen_ts=ts,
                    first_seen_price=c.entry_price,
                    slug=c.market.slug,
                    outcome_name=c.outcome.name,
                )
            rows.append([
                ts, self.tick_id,
                c.market.condition_id, c.market.slug, c.market.question,
                c.outcome.name, tid,
                f"{c.entry_price:.4f}", f"{c.edge_pct:.4f}",
                f"{c.confidence:.4f}", f"{c.score:.4f}",
                f"{sizes_by_token.get(tid, 0.0):.2f}",
                _fmt_days(c.market.days_to_end),
                f"{c.market.volume_24h:.2f}",
                f"{c.market.liquidity:.2f}",
                f"{c.market.spread:.4f}",
                f"{c.market.best_bid:.4f}",
                f"{c.market.best_ask:.4f}",
                c.reason,
            ])
        if rows:
            with CANDIDATES_CSV.open("a", newline="") as f:
                csv.writer(f).writerows(rows)
        return len(rows)

    def write_price_track(self, ts: str, markets: Iterable[Market]) -> int:
        # token_id -> (market, outcome) lookup over the current scan.
        lookup: Dict[str, Tuple[Market, Outcome]] = {}
        for m in markets:
            for o in m.outcomes:
                lookup[o.token_id] = (m, o)

        rows: List[List] = []
        resolution_rows: List[List] = []
        resolved_ids: List[str] = []
        for tid, t in self.tracked.items():
            entry = lookup.get(tid)
            if entry is None:
                # Token absent from the active-markets feed: either resolved,
                # archived, or below the volume floor. We keep tracking it
                # for a few ticks in case it pops back up; if it stays gone,
                # we record the last-known price as a resolution proxy.
                t.missing_count += 1
                if t.missing_count >= RESOLVE_AFTER_MISSING_TICKS:
                    reason = _classify_resolution(t.last_seen_price)
                    resolution_rows.append([
                        ts, self.tick_id, tid, t.slug, t.outcome_name,
                        t.first_seen_ts, f"{t.first_seen_price:.4f}",
                        t.last_seen_ts, f"{t.last_seen_price:.4f}",
                        t.missing_count, reason,
                    ])
                    resolved_ids.append(tid)
                continue
            m, o = entry
            t.missing_count = 0
            t.last_seen_ts = ts
            t.last_seen_price = o.price
            rows.append([
                ts, self.tick_id, tid, m.slug, o.name,
                f"{o.price:.4f}",
                f"{m.best_bid:.4f}", f"{m.best_ask:.4f}",
                f"{m.spread:.4f}",
                f"{m.volume_24h:.2f}",
                _fmt_days(m.days_to_end),
                t.first_seen_ts, f"{t.first_seen_price:.4f}",
            ])
        if rows:
            with PRICE_TRACK_CSV.open("a", newline="") as f:
                csv.writer(f).writerows(rows)
        if resolution_rows:
            with RESOLUTIONS_CSV.open("a", newline="") as f:
                csv.writer(f).writerows(resolution_rows)
            log.info("Study: recorded %d token resolutions (proxied by last-seen price)",
                     len(resolution_rows))
            for tid in resolved_ids:
                self.tracked.pop(tid, None)
        self._save()
        return len(rows)
