#!/usr/bin/env python3
"""News/event-driven price-jump probe for live sports markets.

Runs as a long-lived daemon. Every POLL_INTERVAL seconds:
  1. Fetches the current midpoint for every tracked token via
     ``clob.polymarket.com/midpoints`` (batch POST).
  2. Compares to the previous snapshot for that token.
  3. If |Δprice| ≥ JUMP_THRESHOLD over a window ≤ MAX_JUMP_WINDOW_SEC,
     logs a Jump event to ``logs/news_jumps.jsonl``.

Every REFRESH_MARKETS_INTERVAL seconds:
  4. Re-pulls the list of currently-live sports markets from Gamma's
     /markets endpoint (filtered by slug prefix + days_to_end < 0.5 +
     accepting_orders + min volume). Old / settled markets drop out;
     new ones (next game window) join.

Purpose: measure whether sports markets actually move in jumps during
live play. If we see frequent ≥3¢ jumps inside 30-60s windows, then a
news-driven detector with a sports-data API can race the market.
If jumps are rare or small, the strategy is dead and we save days of
build time.

Standalone — does not touch the running polybot.service. Reads-only.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
JUMPS_FILE = LOG_DIR / "news_jumps.jsonl"
TICKS_FILE = LOG_DIR / "news_ticks.jsonl"

POLL_INTERVAL = 30                  # seconds between midpoint polls
REFRESH_MARKETS_INTERVAL = 300      # 5min — refetch live-sports list
JUMP_THRESHOLD = 0.03               # 3¢ minimum to log
MAX_JUMP_WINDOW_SEC = 90            # only count as a jump if Δt ≤ this
MIN_VOL_24H = 5_000                 # don't probe sub-$5k markets (too noisy)
MAX_TOKENS_TRACKED = 200            # cap to bound API load
HTTP_TIMEOUT = 8

# Slug prefixes for currently-traded sports on Polymarket. Compiled from
# candidates.csv survey on 2026-05-04: NBA / MLB / NFL / NHL dominate
# in US hours; LoL / CS2 / Dota / Valorant in evening EU/Asia hours;
# global soccer leagues throughout the day. Tennis is sparse but
# present (ATP/WTA tournaments).
SPORT_PREFIXES = (
    "lol-", "cs2-", "dota2-", "val-",
    "atp-", "wta-",
    "nba-", "mlb-", "nfl-", "nhl-",
    "epl-", "ucl-", "uel-",
    "sea-",  # Saudi pro league
    "rusrp-",  # Russian premier
    "bra-", "ned-", "tur-",
    "f1-", "fra-", "ger-", "esp-", "ita-",
    "ukr1-", "fra1-", "ger1-", "esp1-", "ita1-", "mar1-",
    "per1-", "arg-", "rus-",
    "spl-",  # Saudi pro league alt
    "boxing-", "ufc-", "mma-", "tennis-", "rugby-",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class TokenSnapshot:
    token_id: str
    slug: str
    sport: str            # parsed prefix
    last_price: float
    last_ts: float        # unix seconds
    outcome_name: str = ""
    vol24h: float = 0.0   # snapshot at last market refresh


def is_sports_slug(slug: str) -> Optional[str]:
    """Return the matched sport prefix (without trailing dash) or None."""
    for p in SPORT_PREFIXES:
        if slug.startswith(p):
            return p[:-1]
    return None


def fetch_live_sports_markets() -> List[Dict[str, Any]]:
    """Pull the currently-tradable sports markets from Gamma. Filters:
    accepting_orders, days_to_end ≤ 0.5 (live or just-played), volume
    floor, sports slug.

    Caps at MAX_TOKENS_TRACKED tokens (each market has 2 outcomes; we
    track both for full directional coverage, halved if needed).
    """
    out: List[Dict[str, Any]] = []
    offset = 0
    limit = 100
    seen_tokens = 0
    while seen_tokens < MAX_TOKENS_TRACKED:
        params = {
            "closed": "false", "active": "true", "archived": "false",
            "limit": limit, "offset": offset,
            "order": "volume24hr", "ascending": "false",
        }
        try:
            r = requests.get(f"{GAMMA_HOST}/markets", params=params, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            page = r.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"[{_now_iso()}] gamma fetch failed: {exc}", file=sys.stderr)
            break
        if not isinstance(page, list) or not page:
            break
        for m in page:
            slug = m.get("slug", "")
            sport = is_sports_slug(slug)
            if sport is None:
                continue
            if not m.get("acceptingOrders") or m.get("closed"):
                continue
            vol = float(m.get("volume24hr") or 0)
            if vol < MIN_VOL_24H:
                continue
            # Filter by end-date proximity. Some sports markets keep
            # ``endDate`` in the past after game-ends but stay tradable
            # briefly during settlement — those are still interesting
            # because their final reprice can be sharp.
            try:
                end_str = m.get("endDate") or m.get("endDateIso") or ""
                if end_str:
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    days_to_end = (end_dt - datetime.now(timezone.utc)).total_seconds() / 86400.0
                    if days_to_end > 1.0:  # not live yet
                        continue
            except (ValueError, TypeError):
                pass
            try:
                token_ids = json.loads(m.get("clobTokenIds") or "[]")
                outcomes = json.loads(m.get("outcomes") or "[]")
            except (ValueError, TypeError):
                continue
            if len(token_ids) != len(outcomes):
                continue
            # Take both legs of each binary market.
            for tid, name in zip(token_ids, outcomes):
                out.append({
                    "token_id": str(tid),
                    "slug": slug,
                    "outcome_name": name,
                    "sport": sport,
                    "vol24h": vol,
                })
                seen_tokens += 1
                if seen_tokens >= MAX_TOKENS_TRACKED:
                    break
            if seen_tokens >= MAX_TOKENS_TRACKED:
                break
        if len(page) < limit:
            break
        offset += limit
    return out


def fetch_midpoints(token_ids: List[str]) -> Dict[str, float]:
    """Batch-fetch midpoints from CLOB. Same trick as the dashboard:
    POST a JSON list of ``{"token_id": ...}`` records, get a dict back.
    Returns empty dict on any HTTP / parse failure."""
    if not token_ids:
        return {}
    payload = json.dumps([{"token_id": t} for t in token_ids]).encode()
    req = urllib.request.Request(
        f"{CLOB_HOST}/midpoints",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "polybot-news-probe/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            raw = json.loads(r.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError) as exc:
        print(f"[{_now_iso()}] midpoints fetch failed: {exc}", file=sys.stderr)
        return {}
    out: Dict[str, float] = {}
    if isinstance(raw, dict):
        for tid, val in raw.items():
            try:
                out[str(tid)] = float(val)
            except (TypeError, ValueError):
                continue
    return out


def write_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true",
                    help="Run a single poll cycle and exit (debug).")
    ap.add_argument("--no-tick-log", action="store_true",
                    help="Skip the per-tick JSONL (only log jumps). "
                         "Saves disk if you only care about events.")
    args = ap.parse_args()

    state: Dict[str, TokenSnapshot] = {}
    last_market_refresh = 0.0
    stop = False

    def _signal_handler(_sig, _frame):
        nonlocal stop
        print(f"[{_now_iso()}] received signal, shutting down", flush=True)
        stop = True

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    print(f"[{_now_iso()}] news probe starting — poll={POLL_INTERVAL}s "
          f"jump_threshold={JUMP_THRESHOLD*100:.0f}¢ "
          f"window={MAX_JUMP_WINDOW_SEC}s", flush=True)

    while not stop:
        loop_t0 = time.time()

        # Refresh market list periodically.
        if loop_t0 - last_market_refresh > REFRESH_MARKETS_INTERVAL:
            markets = fetch_live_sports_markets()
            current_ids = {m["token_id"] for m in markets}
            # Drop tokens no longer live.
            for tid in list(state.keys()):
                if tid not in current_ids:
                    state.pop(tid)
            # Update vol24h / outcome_name on existing entries; new entries
            # added on first observation in the poll loop below.
            for m in markets:
                tid = m["token_id"]
                if tid in state:
                    state[tid].vol24h = m.get("vol24h", 0.0)
                    state[tid].outcome_name = m.get("outcome_name", "")
                    state[tid].slug = m.get("slug", state[tid].slug)
                    state[tid].sport = m.get("sport", state[tid].sport)
            print(f"[{_now_iso()}] market refresh: {len(markets)} live sports tokens "
                  f"({len({m['slug'] for m in markets})} unique slugs, "
                  f"sports={sorted({m['sport'] for m in markets})})", flush=True)
            last_market_refresh = loop_t0
            market_meta = {m["token_id"]: m for m in markets}
        else:
            # Reuse the per-token meta we already cached in state. This
            # was the source of the all-zero-vol24h bug in v1.
            market_meta = {
                tid: {"slug": s.slug, "sport": s.sport,
                      "outcome_name": s.outcome_name, "vol24h": s.vol24h}
                for tid, s in state.items()
            }

        token_ids = list(market_meta.keys())
        if not token_ids:
            time.sleep(POLL_INTERVAL)
            continue

        prices = fetch_midpoints(token_ids)
        now_ts = time.time()
        n_new, n_jumps = 0, 0
        for tid, p in prices.items():
            meta = market_meta.get(tid, {})
            prev = state.get(tid)
            if prev is None:
                state[tid] = TokenSnapshot(
                    token_id=tid, slug=meta.get("slug", ""),
                    sport=meta.get("sport", ""),
                    last_price=p, last_ts=now_ts,
                    outcome_name=meta.get("outcome_name", ""),
                    vol24h=float(meta.get("vol24h", 0) or 0),
                )
                n_new += 1
                continue
            dt = now_ts - prev.last_ts
            dp = p - prev.last_price
            if abs(dp) >= JUMP_THRESHOLD and dt <= MAX_JUMP_WINDOW_SEC:
                n_jumps += 1
                write_jsonl(JUMPS_FILE, {
                    "ts": _now_iso(),
                    "token_id": tid,
                    "slug": prev.slug,
                    "sport": prev.sport,
                    "outcome_name": prev.outcome_name,
                    "from_price": round(prev.last_price, 4),
                    "to_price": round(p, 4),
                    "delta": round(dp, 4),
                    "duration_sec": round(dt, 1),
                    "vol24h": prev.vol24h,
                })
            if not args.no_tick_log:
                write_jsonl(TICKS_FILE, {
                    "ts_unix": round(now_ts, 1),
                    "token_id": tid,
                    "slug": prev.slug,
                    "price": round(p, 4),
                })
            prev.last_price = p
            prev.last_ts = now_ts

        elapsed = time.time() - loop_t0
        print(f"[{_now_iso()}] poll: {len(prices)}/{len(token_ids)} mids fetched, "
              f"{n_new} new, {n_jumps} jumps; cycle={elapsed:.1f}s", flush=True)

        if args.once:
            return 0

        sleep_for = max(1.0, POLL_INTERVAL - elapsed)
        # Sleep in 1-second slices so SIGTERM is responsive.
        slept = 0.0
        while slept < sleep_for and not stop:
            time.sleep(min(1.0, sleep_for - slept))
            slept += 1.0

    print(f"[{_now_iso()}] news probe stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
