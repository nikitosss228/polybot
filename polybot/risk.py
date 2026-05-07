"""Risk manager: per-trade caps, total exposure, daily loss kill-switch.

State is held in memory plus an on-disk JSON snapshot so the daily-loss
counter survives restarts within the same UTC day.

As of 2026-05-04, open-position state lives in :mod:`polybot.positions`
(``PositionRegistry``). RiskManager queries the registry for
``open_exposure`` and ``has_open`` rather than tracking its own copy —
single source of truth, no chance of drift between the two views.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .config import PROJECT_ROOT, SETTINGS
from .logger import get_logger
from .models import Candidate
from .positions import PositionRegistry

log = get_logger(__name__)

STATE_FILE = PROJECT_ROOT / "logs" / "risk_state.json"
CATEGORY_PRIORS_FILE = PROJECT_ROOT / "logs" / "category_priors.json"


# ---------- slug → category classifier (matches scripts/category_priors.py) -

_SPORTS_PREFIXES = (
    "lol-", "cs2-", "dota2-", "val-", "atp-", "wta-", "nba-", "mlb-",
    "nfl-", "nhl-", "epl-", "ucl-", "uel-", "sea-", "rusrp-", "bra-",
    "f1-", "fra-", "ger-", "esp-", "ita-", "ned-", "tur-", "rus-",
    "ukr1-", "fra1-", "ger1-", "esp1-", "ita1-", "rugby-",
    "boxing-", "ufc-", "mma-", "cricket-", "ipl-", "tennis-",
    "golf-", "snooker-", "darts-", "pga-", "mar1-", "spl-",
    "per1-", "arg-", "r6siege-",
)
_CRYPTO_KW = ("bitcoin", "ethereum", "btc-", "eth-", "sol-", "xrp-", "bnb-",
              "doge", "ada-", "matic-", "avax-", "trx-", "ltc-", "linkup",
              "shib", "polygon", "chainlink", "memecoin", "cryptocurrency",
              "fdv-", "token-launch")
_WEATHER_KW = ("temperature-in-", "rain-in-", "snow-in-", "weather-",
               "hottest-on-record", "lowest-temperature")
_FINANCE_KW = ("wti-", "xauusd", "xagusd", "spy-", "qqq-", "amzn-", "tsla-",
               "aapl-", "msft-", "googl-", "meta-", "nvda-", "stock-",
               "gas-", "oil-", "gold-", "silver-")
_POLITICS_KW = ("trump", "biden", "harris", "putin", "election", "primary",
                "nominee", "presidency", "presidential", "governor", "senate",
                "congress", "house-republican", "iran", "russia-x", "ukraine",
                "israel", "china-invade", "nuclear-deal")
_MACRO_KW = ("fed-", "fed-rate", "inflation", "recession", "gdp-",
             "rate-cut", "rate-hike", "ecb-", "interest-rates")
_TRUMP_DAILY_KW = ("trump-publicly-insult", "truth-social-posts", "trump-tweets",
                   "trump-says", "elon-musk")


def category_for_slug(slug: str) -> str:
    """Classify a slug into a category bucket. Order = priority."""
    s = slug.lower()
    for p in _SPORTS_PREFIXES:
        if s.startswith(p):
            return "sports"
    for kw in _WEATHER_KW:
        if kw in s:
            return "weather"
    for kw in _MACRO_KW:
        if kw in s:
            return "macro"
    for kw in _TRUMP_DAILY_KW:
        if kw in s:
            return "trump_daily"
    for kw in _FINANCE_KW:
        if kw in s:
            return "finance"
    for kw in _CRYPTO_KW:
        if kw in s:
            return "crypto"
    for kw in _POLITICS_KW:
        if kw in s:
            return "politics"
    return "other"


# Empirical (detector, category) → win rate priors, computed offline by
# scripts/category_priors.py. Loaded at module import time. Falls back to
# detector-level WINRATE_* settings when a (detector, category) cell is
# missing or under-sampled.
_CATEGORY_PRIORS: Dict[str, Dict[str, float]] = {}


def _load_category_priors() -> None:
    global _CATEGORY_PRIORS
    if not CATEGORY_PRIORS_FILE.exists():
        log.info("category_priors.json not found — using detector-level WINRATE_* fallbacks only")
        return
    try:
        blob = json.loads(CATEGORY_PRIORS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("category_priors.json load failed (%s); using fallbacks", exc)
        return
    pdc = blob.get("per_detector_category", {})
    if isinstance(pdc, dict):
        _CATEGORY_PRIORS = {k: dict(v) for k, v in pdc.items() if isinstance(v, dict)}
    n_cells = sum(len(v) for v in _CATEGORY_PRIORS.values())
    log.info("category_priors loaded: %d (detector, category) cells from %d total resolutions",
             n_cells, blob.get("generated_at_n_total", 0))


_load_category_priors()


def winrate_for_candidate(c: Candidate) -> float:
    """Look up Kelly prior for a specific candidate.

    Order: (detector, category) → SETTINGS WINRATE_<DET> → 0.5.
    Empirical (detector, category) cells from realized data outweigh
    the per-detector fallback when sample size warranted them at
    calibration time (script enforces min_n_for_category=30).
    """
    cat = category_for_slug(c.market.slug)
    cell = _CATEGORY_PRIORS.get(c.detector, {})
    if cat in cell:
        return cell[cat]
    # Fallback: detector-level prior from .env / SETTINGS
    attr = _DETECTOR_ATTR.get(("wr", c.detector))
    if attr:
        return getattr(SETTINGS, attr, 0.5)
    return 0.5


@dataclass
class RiskState:
    day: str = ""             # YYYY-MM-DD UTC
    daily_pnl: float = 0.0    # USDC realised today (negative = loss)
    cumulative_pnl: float = 0.0  # USDC realised over the lifetime of the bot
    peak_pnl: float = 0.0       # highest cumulative_pnl ever seen — drawdown reference

    def reset_if_new_day(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if self.day != today:
            log.info("New UTC day %s — resetting daily PnL counter", today)
            self.day = today
            self.daily_pnl = 0.0

    @property
    def drawdown_usd(self) -> float:
        """Distance from peak cumulative PnL, in USDC. Always >= 0."""
        return max(0.0, self.peak_pnl - self.cumulative_pnl)


# Per-detector base sizes and win-rate priors — resolved via SETTINGS at
# access time so a test that monkey-patches SETTINGS sees the new values.
_DETECTOR_ATTR = {
    ("size", "date_expired"):   "size_date_expired",
    ("size", "extreme_priced"): "size_extreme_priced",
    ("size", "mm_spread"):      "size_mm_spread",
    ("size", "bundle_arb"):     "size_bundle_arb",
    ("wr",   "date_expired"):   "winrate_date_expired",
    ("wr",   "extreme_priced"): "winrate_extreme_priced",
    ("wr",   "mm_spread"):      "winrate_mm_spread",
    ("wr",   "bundle_arb"):     "winrate_bundle_arb",
}


class _DetectorView:
    """``view.get(detector_name, default)`` → SETTINGS attribute lookup."""
    def __init__(self, kind: str) -> None:
        self._kind = kind
    def get(self, detector: str, default: float = 0.0) -> float:
        attr = _DETECTOR_ATTR.get((self._kind, detector))
        return getattr(SETTINGS, attr, default) if attr else default


_DETECTOR_BASE = _DetectorView("size")
_DETECTOR_WINRATE = _DetectorView("wr")


class RiskManager:
    def __init__(self, registry: Optional[PositionRegistry] = None) -> None:
        self.state = self._load()
        # Registry is the single source of truth for open positions /
        # open exposure. We accept None for ergonomic test/CLI use, but
        # production main() always passes one in.
        self.registry: Optional[PositionRegistry] = registry

    # ---- persistence ---------------------------------------------------

    def _load(self) -> RiskState:
        # In dry-run we want a clean slate every restart so candidates aren't
        # gated by stale simulated exposure from a prior process.
        if SETTINGS.dry_run:
            return RiskState(day=datetime.now(timezone.utc).date().isoformat())
        if STATE_FILE.exists():
            try:
                blob = json.loads(STATE_FILE.read_text())
                # Tolerant load: pre-2026-05-04 risk_state.json may have
                # ``open_trades`` and ``open_exposure`` fields that no
                # longer belong on RiskState (registry owns them now).
                # Drop any unknown keys instead of TypeError-ing.
                allowed = {f for f in RiskState.__dataclass_fields__}
                clean = {k: v for k, v in blob.items() if k in allowed}
                return RiskState(**clean)
            except (json.JSONDecodeError, TypeError) as exc:
                log.warning("Failed to load risk state (%s); starting fresh", exc)
        return RiskState(day=datetime.now(timezone.utc).date().isoformat())

    def _save(self) -> None:
        if SETTINGS.dry_run:
            return
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(self.state.__dict__, indent=2))
        except OSError as exc:
            log.warning("Failed to persist risk state: %s", exc)

    # ---- registry-derived helpers --------------------------------------

    @property
    def open_exposure(self) -> float:
        # Single source of truth: registry. If nothing wired (tests / CLI),
        # return 0 — gates that consult this still pass through.
        return self.registry.open_exposure_usd if self.registry is not None else 0.0

    def _has_open_token(self, token_id: str) -> bool:
        return self.registry.has_open(token_id) if self.registry is not None else False

    # ---- decisions -----------------------------------------------------

    def gate(self, c: Candidate) -> Optional[str]:
        """Return None if trade is allowed; otherwise a string reason."""
        self.state.reset_if_new_day()

        if c.entry_price <= 0 or c.entry_price >= 1:
            return f"price {c.entry_price} out of bounds"
        if c.edge_pct < SETTINGS.min_edge_pct:
            return f"edge {c.edge_pct:.2f}% < min {SETTINGS.min_edge_pct}%"
        # Net-of-fees gate: defaults to gross edge if detector didn't compute net.
        net = c.edge_pct_net if c.edge_pct_net is not None else c.edge_pct
        if net < SETTINGS.min_edge_after_fees_pct:
            return f"net edge {net:.2f}% < min {SETTINGS.min_edge_after_fees_pct}%"
        if self.state.daily_pnl <= -SETTINGS.max_daily_loss:
            return f"daily loss {self.state.daily_pnl:.2f} hit kill-switch"
        # Drawdown kill-switch — pauses trading once we're far enough below peak.
        # Measured against TOTAL_BUDGET so a fresh bot (peak=0) has no drawdown.
        dd_pct = self.state.drawdown_usd / SETTINGS.total_budget * 100.0
        if dd_pct >= SETTINGS.max_drawdown_pct:
            return (
                f"drawdown {self.state.drawdown_usd:.2f} ({dd_pct:.1f}% of budget) "
                f">= cap {SETTINGS.max_drawdown_pct:.1f}%"
            )
        if self.open_exposure + SETTINGS.max_per_trade > SETTINGS.max_total_exposure:
            return (
                f"total exposure {self.open_exposure:.2f} + "
                f"{SETTINGS.max_per_trade} > cap {SETTINGS.max_total_exposure}"
            )
        if self._has_open_token(c.outcome.token_id):
            return f"already have open position on token {c.outcome.token_id[:12]}..."
        return None

    def position_size(self, c: Candidate) -> float:
        """USDC to deploy on this candidate. Dispatches on SIZING_MODE.

        All modes are bounded above by ``MAX_PER_TRADE × 2`` and the
        remaining ``MAX_TOTAL_EXPOSURE - open_exposure`` headroom.
        """
        size = self._raw_size(c)
        # Hard caps applied uniformly to every mode.
        size = min(size, SETTINGS.max_per_trade * 2.0)
        headroom = max(0.0, SETTINGS.max_total_exposure - self.open_exposure)
        return round(min(size, headroom), 2)

    def _raw_size(self, c: Candidate) -> float:
        mode = SETTINGS.sizing_mode
        if mode == "flat":
            # Legacy: 0.5 * cap floor, scales with confidence to full cap.
            confidence_scaling = max(0.5, c.confidence)
            return SETTINGS.max_per_trade * confidence_scaling
        if mode == "per_detector":
            base = _DETECTOR_BASE.get(c.detector, SETTINGS.max_per_trade)
            # Linear in confidence (no 0.5 floor — low-conf trades genuinely
            # smaller than high-conf ones).
            return base * max(0.1, c.confidence)
        if mode == "edge_scaled":
            # ``edge_pct`` is in percent units (e.g. 5.0 = 5%). Divide by
            # ``EDGE_SCALE_REF`` so an edge of REF = 1× MAX_PER_TRADE.
            scale = c.edge_pct / SETTINGS.edge_scale_ref
            scale = max(0.1, min(2.0, scale))
            return SETTINGS.max_per_trade * scale * max(0.5, c.confidence)
        if mode == "fractional_kelly":
            # Per-(detector, category) prior from realized data, with
            # detector-level WINRATE_* fallback for under-sampled cells.
            p = winrate_for_candidate(c)
            # Kelly: f* = p - (1 - p) * P / (1 - P) where P = entry_price.
            # Bounded below at 0 (don't take negative-EV trades — gate
            # already filters most). Bounded above at 0.5 (nothing makes
            # sense to risk >50% on one trade even at "100% Kelly").
            P = c.entry_price
            if P <= 0 or P >= 1:
                return 0.0
            f_full = p - (1 - p) * P / (1 - P)
            if f_full <= 0:
                return 0.0
            f_full = min(0.5, f_full)
            f = f_full * SETTINGS.kelly_fraction
            return SETTINGS.total_budget * f
        # Unreachable — config validates the mode at startup.
        return SETTINGS.max_per_trade

    def record_open(self, c: Candidate, usd_size: float) -> None:
        # Position registration goes through the registry now (called by
        # main.run_tick after a successful place_buy_limit). RiskManager
        # only needs to know "a position was opened" for logging context;
        # exposure is read from registry on demand.
        if SETTINGS.dry_run:
            return
        log.info(
            "Risk: opened token=%s size=$%.2f total_exposure=$%.2f",
            c.outcome.token_id[:12], usd_size, self.open_exposure,
        )

    def record_close(self, token_id: str, realised_pnl: float) -> None:
        # Called from PositionRegistry.check_resolutions on_close callback.
        # The registry has already moved the position to ``closed`` and
        # computed PnL; we just update the daily / cumulative counters
        # and persist.
        self.state.daily_pnl += realised_pnl
        self.state.cumulative_pnl += realised_pnl
        if self.state.cumulative_pnl > self.state.peak_pnl:
            self.state.peak_pnl = self.state.cumulative_pnl
        self._save()
        log.info(
            "Risk: closed token=%s pnl=$%.2f day_pnl=$%.2f cum_pnl=$%.2f "
            "peak=$%.2f dd=$%.2f exposure=$%.2f",
            token_id[:12], realised_pnl, self.state.daily_pnl,
            self.state.cumulative_pnl, self.state.peak_pnl,
            self.state.drawdown_usd, self.open_exposure,
        )
