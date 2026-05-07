#!/usr/bin/env python3
"""Compute empirical win rates per (detector, category) from realized data.

Reads candidates.csv + resolutions.csv, classifies each candidate by slug
into a category (sports/weather/politics/crypto/finance/macro/other), and
emits a table of (detector, category) → win rate, sample size, total
deployed PnL. Output goes to ``logs/category_priors.json`` for use by
risk.py at runtime.

Sample sizes vary widely by category — small-n cells fall back to the
detector-level overall prior at runtime.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Optional, Tuple

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
CAND_CSV = LOG_DIR / "candidates.csv"
RES_CSV = LOG_DIR / "resolutions.csv"
OUT_FILE = LOG_DIR / "category_priors.json"

MIN_N_FOR_CATEGORY = 30  # below this, fall back to detector-level prior


# ---------- slug → category classifier --------------------------------------

# Same prefixes/keywords as in backtest.py / sim_wallet.py for consistency.
SPORTS_PREFIXES = (
    "lol-", "cs2-", "dota2-", "val-", "atp-", "wta-", "nba-", "mlb-",
    "nfl-", "nhl-", "epl-", "ucl-", "uel-", "sea-", "rusrp-", "bra-",
    "f1-", "fra-", "ger-", "esp-", "ita-", "ned-", "tur-", "rus-",
    "ukr1-", "fra1-", "ger1-", "esp1-", "ita1-", "rugby-",
    "boxing-", "ufc-", "mma-", "cricket-", "ipl-", "tennis-",
    "golf-", "snooker-", "darts-", "pga-", "mar1-", "spl-",
    "per1-", "arg-", "r6siege-",
)
CRYPTO_KEYWORDS = (
    "bitcoin", "ethereum", "btc-", "eth-", "sol-", "xrp-", "bnb-",
    "doge", "ada-", "matic-", "avax-", "trx-", "ltc-", "linkup",
    "shib", "polygon", "chainlink", "memecoin", "cryptocurrency",
    "fdv-", "token-launch",
)
WEATHER_KEYWORDS = (
    "temperature-in-", "rain-in-", "snow-in-", "weather-",
    "hottest-on-record", "lowest-temperature",
)
FINANCE_KEYWORDS = (
    "wti-", "xauusd", "xagusd", "spy-", "qqq-", "amzn-", "tsla-",
    "aapl-", "msft-", "googl-", "meta-", "nvda-", "stock-",
    "gas-", "oil-", "gold-", "silver-",
)
POLITICS_KEYWORDS = (
    "trump", "biden", "harris", "putin", "election", "primary",
    "nominee", "presidency", "presidential", "governor", "senate",
    "congress", "house-republican", "iran", "russia-x", "ukraine",
    "israel", "china-invade", "nuclear-deal",
)
MACRO_KEYWORDS = (
    "fed-", "fed-rate", "inflation", "recession", "gdp-",
    "rate-cut", "rate-hike", "ecb-", "interest-rates",
)
TRUMP_DAILY_KEYWORDS = (
    "trump-publicly-insult", "truth-social-posts", "trump-tweets",
    "trump-says", "elon-musk",
)


def category_for_slug(slug: str) -> str:
    """First-match wins. Order chosen so sports prefix beats crypto kw, etc."""
    s = slug.lower()
    for p in SPORTS_PREFIXES:
        if s.startswith(p):
            return "sports"
    for kw in WEATHER_KEYWORDS:
        if kw in s:
            return "weather"
    for kw in MACRO_KEYWORDS:
        if kw in s:
            return "macro"
    for kw in TRUMP_DAILY_KEYWORDS:
        if kw in s:
            return "trump_daily"
    for kw in FINANCE_KEYWORDS:
        if kw in s:
            return "finance"
    for kw in CRYPTO_KEYWORDS:
        if kw in s:
            return "crypto"
    for kw in POLITICS_KEYWORDS:
        if kw in s:
            return "politics"
    return "other"


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


def _f(v, default=0.0):
    if v in (None, ""):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------- main ------------------------------------------------------------


def main() -> int:
    # Load resolutions: confident only (near_one → 1.0, near_zero → 0.0)
    resolutions: Dict[str, float] = {}
    with RES_CSV.open(newline="") as f:
        for row in csv.DictReader(f):
            if not row.get("last_seen_ts"):
                continue
            r = row.get("resolution_reason", "")
            if r == "near_one":
                resolutions[row["token_id"]] = 1.0
            elif r == "near_zero":
                resolutions[row["token_id"]] = 0.0

    print(f"loaded {len(resolutions)} confident resolutions")

    # First-seen candidate per token
    first: Dict[str, Dict[str, str]] = {}
    with CAND_CSV.open(newline="") as f:
        for row in csv.DictReader(f):
            tid = row["token_id"]
            existing = first.get(tid)
            if existing is None or int(row["tick_id"]) < int(existing["tick_id"]):
                first[tid] = row

    # Aggregate per (detector, category)
    cells: Dict[Tuple[str, str], Dict[str, float]] = defaultdict(
        lambda: {"n": 0, "wins": 0, "sum_pnl": 0.0, "sum_size": 0.0}
    )
    for tid, terminal in resolutions.items():
        c = first.get(tid)
        if c is None:
            continue
        det = _classify_detector(c.get("reason", ""))
        if det == "unknown":
            continue
        cat = category_for_slug(c.get("slug", ""))
        entry = _f(c["entry_price"])
        if entry <= 0:
            continue
        size = _f(c["size_usd"])
        # Directional PnL only — we're estimating prediction-quality, not exec.
        pnl = size * (terminal - entry) / entry
        is_win = terminal > entry
        cell = cells[(det, cat)]
        cell["n"] += 1
        if is_win: cell["wins"] += 1
        cell["sum_pnl"] += pnl
        cell["sum_size"] += size

    # Per-detector overall (fallback prior)
    by_det: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"n": 0, "wins": 0, "sum_pnl": 0.0, "sum_size": 0.0}
    )
    for (det, cat), s in cells.items():
        by_det[det]["n"] += s["n"]
        by_det[det]["wins"] += s["wins"]
        by_det[det]["sum_pnl"] += s["sum_pnl"]
        by_det[det]["sum_size"] += s["sum_size"]

    # Print table
    print(f"\n=== per-detector overall (fallback) ===")
    print(f"  {'detector':<14} {'n':>5} {'wins':>5} {'win_rate':>9} {'gross_roi':>10}")
    detector_priors: Dict[str, float] = {}
    for det in sorted(by_det):
        s = by_det[det]
        if s["n"] == 0: continue
        wr = s["wins"] / s["n"]
        roi = s["sum_pnl"] / s["sum_size"] if s["sum_size"] > 0 else 0
        print(f"  {det:<14} {s['n']:>5} {s['wins']:>5} {wr*100:>8.1f}% {roi*100:>+9.2f}%")
        detector_priors[det] = wr

    print(f"\n=== per (detector, category) — n>={MIN_N_FOR_CATEGORY} actionable ===")
    print(f"  {'detector':<14} {'category':<14} {'n':>5} {'wins':>5} {'win_rate':>9} {'gross_roi':>10}")
    cat_priors: Dict[str, Dict[str, float]] = defaultdict(dict)
    for (det, cat), s in sorted(cells.items()):
        if s["n"] < MIN_N_FOR_CATEGORY:
            continue
        wr = s["wins"] / s["n"]
        roi = s["sum_pnl"] / s["sum_size"] if s["sum_size"] > 0 else 0
        marker = "★" if abs(wr - detector_priors.get(det, wr)) > 0.05 else " "
        print(f"  {det:<14} {cat:<14} {s['n']:>5} {s['wins']:>5} {wr*100:>8.1f}% {roi*100:>+9.2f}% {marker}")
        cat_priors[det][cat] = wr

    # Write JSON for risk.py to load
    out = {
        "generated_at_n_total": sum(s["n"] for s in cells.values()),
        "min_n_for_category": MIN_N_FOR_CATEGORY,
        "fallback_per_detector": detector_priors,
        "per_detector_category": dict(cat_priors),
    }
    OUT_FILE.write_text(json.dumps(out, indent=2))
    print(f"\nwrote {OUT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
