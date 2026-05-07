"""Centralised configuration for Polybot.

All values come from environment variables (with `.env` for local dev).
Validation happens at import time so the process fails fast if misconfigured.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default) or ""


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Env var {name} must be int, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"Env var {name} must be float, got {raw!r}") from exc


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv(name: str, default: str = "") -> Tuple[str, ...]:
    raw = os.getenv(name, default) or ""
    return tuple(x.strip().lower() for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    dry_run: bool
    auto_trade: bool

    private_key: str
    signature_type: int
    funder_address: str
    polygon_rpc_url: str

    total_budget: float
    max_per_trade: float
    max_total_exposure: float
    max_daily_loss: float
    max_drawdown_pct: float
    min_edge_pct: float
    min_edge_after_fees_pct: float
    gas_estimate_per_order: float
    min_24h_volume: float
    max_price: float
    min_price: float

    bundle_arb_enabled: bool
    bundle_arb_min_edge_pct: float

    mm_flagger_enabled: bool
    mm_min_spread: float
    mm_min_volume: float

    scan_interval_sec: int
    category_filter: Tuple[str, ...]

    log_level: str
    log_file: Path
    log_max_bytes: int
    log_backup_count: int

    # Optional SOCKS5 proxy for outbound HTTPS to Polymarket. Set when
    # the bot's egress IP is in a Polymarket-blocked jurisdiction
    # (entire EU/EEA + US/UK/AU/CA/SG/RU/TW + others). Format:
    # ``socks5://user:pass@host:port`` (use socks5 to resolve DNS
    # locally, socks5h to resolve via the proxy — prefer socks5h to
    # avoid DNS leak). Empty / unset = direct connection.
    # py-clob-client (sync, uses requests) picks this up via the
    # HTTPS_PROXY env var; aiohttp (PolyClient) needs it injected
    # via aiohttp_socks.ProxyConnector.
    socks5_proxy: str

    # Paper-trading simulation. When True (and DRY_RUN=1), the bot
    # opens *paper* positions in the registry with a $100 virtual
    # wallet (`SIMULATION_STARTING_USDC`), realistic taker fees, and
    # deterministic slippage. Equity panel in the dashboard reflects
    # the simulated wallet drift instead of the cumulative-paper-PnL
    # accumulator. Mutually exclusive with DRY_RUN=0 (which is real
    # trading and ignores the simulation flag).
    simulation_mode: bool
    simulation_starting_usdc: float

    # Sizing strategy. ``flat`` is the legacy behavior:
    # ``MAX_PER_TRADE × max(0.5, confidence)``.
    # ``per_detector`` looks up base size from SIZE_DATE_EXPIRED /
    # SIZE_EXTREME_PRICED / SIZE_MM_SPREAD / SIZE_BUNDLE_ARB and scales
    # by confidence (no 0.5 floor).
    # ``edge_scaled`` is ``MAX_PER_TRADE × min(edge_pct / EDGE_SCALE_REF, 2.0)``
    # — bigger predicted edge → bigger size, capped at 2× MAX_PER_TRADE.
    # ``fractional_kelly`` uses Kelly with KELLY_FRACTION safety multiplier
    # and per-detector win-rate priors (DETECTOR_WINRATE_*).
    sizing_mode: str
    edge_scale_ref: float       # edge_pct value at which size = MAX_PER_TRADE
    kelly_fraction: float       # 0..1, e.g. 0.125 for 1/8 Kelly
    size_date_expired: float
    size_extreme_priced: float
    size_mm_spread: float
    size_bundle_arb: float
    winrate_date_expired: float
    winrate_extreme_priced: float
    winrate_mm_spread: float
    winrate_bundle_arb: float

    @property
    def has_wallet(self) -> bool:
        return bool(self.private_key)

    @property
    def has_socks5_proxy(self) -> bool:
        return bool(self.socks5_proxy)


def load_settings() -> Settings:
    log_file_raw = _str("LOG_FILE", "logs/polybot.log")
    log_file = Path(log_file_raw)
    if not log_file.is_absolute():
        log_file = PROJECT_ROOT / log_file
    log_file.parent.mkdir(parents=True, exist_ok=True)

    sig_type = _int("SIGNATURE_TYPE", 0)
    if sig_type not in {0, 1, 2}:
        raise RuntimeError("SIGNATURE_TYPE must be 0, 1, or 2")

    settings = Settings(
        dry_run=_bool("DRY_RUN", True),
        auto_trade=_bool("AUTO_TRADE", False),
        private_key=_str("PRIVATE_KEY"),
        signature_type=sig_type,
        funder_address=_str("FUNDER_ADDRESS"),
        polygon_rpc_url=_str("POLYGON_RPC_URL"),
        total_budget=_float("TOTAL_BUDGET", 100.0),
        max_per_trade=_float("MAX_PER_TRADE", 5.0),
        max_total_exposure=_float("MAX_TOTAL_EXPOSURE", 80.0),
        max_daily_loss=_float("MAX_DAILY_LOSS", 10.0),
        max_drawdown_pct=_float("MAX_DRAWDOWN_PCT", 25.0),
        min_edge_pct=_float("MIN_EDGE_PCT", 1.5),
        min_edge_after_fees_pct=_float("MIN_EDGE_AFTER_FEES_PCT", 0.5),
        gas_estimate_per_order=_float("GAS_ESTIMATE_PER_ORDER", 0.001),
        min_24h_volume=_float("MIN_24H_VOLUME", 1000.0),
        max_price=_float("MAX_PRICE", 0.99),
        min_price=_float("MIN_PRICE", 0.01),
        bundle_arb_enabled=_bool("BUNDLE_ARB_ENABLED", True),
        bundle_arb_min_edge_pct=_float("BUNDLE_ARB_MIN_EDGE_PCT", 1.0),
        mm_flagger_enabled=_bool("MM_FLAGGER_ENABLED", True),
        mm_min_spread=_float("MM_MIN_SPREAD", 0.05),
        mm_min_volume=_float("MM_MIN_VOLUME", 5000.0),
        scan_interval_sec=_int("SCAN_INTERVAL_SEC", 600),
        category_filter=_csv("CATEGORY_FILTER"),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
        log_file=log_file,
        log_max_bytes=_int("LOG_MAX_BYTES", 5 * 1024 * 1024),
        log_backup_count=_int("LOG_BACKUP_COUNT", 5),
        socks5_proxy=_str("SOCKS5_PROXY"),
        simulation_mode=_bool("SIMULATION_MODE", False),
        simulation_starting_usdc=_float("SIMULATION_STARTING_USDC", 100.0),
        sizing_mode=_str("SIZING_MODE", "flat").lower(),
        edge_scale_ref=_float("EDGE_SCALE_REF", 10.0),
        kelly_fraction=_float("KELLY_FRACTION", 0.125),
        size_date_expired=_float("SIZE_DATE_EXPIRED", 3.00),
        size_extreme_priced=_float("SIZE_EXTREME_PRICED", 5.00),
        size_mm_spread=_float("SIZE_MM_SPREAD", 2.50),
        size_bundle_arb=_float("SIZE_BUNDLE_ARB", 5.00),
        # Win-rate priors from realized data (analyze.py 2026-05-04 REALIZED block)
        winrate_date_expired=_float("WINRATE_DATE_EXPIRED", 0.80),
        winrate_extreme_priced=_float("WINRATE_EXTREME_PRICED", 0.986),
        winrate_mm_spread=_float("WINRATE_MM_SPREAD", 0.50),  # unknown, conservative
        winrate_bundle_arb=_float("WINRATE_BUNDLE_ARB", 0.50),  # too few samples
    )

    # --- sanity checks -------------------------------------------------------
    if not (0 < settings.max_per_trade <= settings.max_total_exposure <= settings.total_budget):
        raise RuntimeError(
            "Risk caps inconsistent: need 0 < MAX_PER_TRADE <= MAX_TOTAL_EXPOSURE <= TOTAL_BUDGET",
        )
    if not (0 < settings.min_price < settings.max_price < 1):
        raise RuntimeError("Need 0 < MIN_PRICE < MAX_PRICE < 1")
    if settings.min_edge_pct <= 0:
        raise RuntimeError("MIN_EDGE_PCT must be > 0")
    if settings.min_edge_after_fees_pct < 0:
        raise RuntimeError("MIN_EDGE_AFTER_FEES_PCT must be >= 0")
    if settings.gas_estimate_per_order < 0:
        raise RuntimeError("GAS_ESTIMATE_PER_ORDER must be >= 0")
    if not (0 <= settings.max_drawdown_pct <= 100):
        raise RuntimeError("MAX_DRAWDOWN_PCT must be in [0, 100]")
    if settings.mm_min_spread <= 0:
        raise RuntimeError("MM_MIN_SPREAD must be > 0")
    if settings.scan_interval_sec < 30:
        raise RuntimeError("SCAN_INTERVAL_SEC must be >= 30 to be gentle on the API")
    if settings.sizing_mode not in {"flat", "per_detector", "edge_scaled", "fractional_kelly"}:
        raise RuntimeError(
            f"SIZING_MODE must be one of: flat, per_detector, edge_scaled, fractional_kelly "
            f"(got {settings.sizing_mode!r})"
        )
    if not (0 < settings.kelly_fraction <= 1):
        raise RuntimeError("KELLY_FRACTION must be in (0, 1]")
    if settings.edge_scale_ref <= 0:
        raise RuntimeError("EDGE_SCALE_REF must be > 0")
    if settings.auto_trade and settings.dry_run:
        # Permitted, but worth noting: in DRY_RUN we still log "would-execute".
        pass
    if settings.auto_trade and not settings.dry_run and not settings.has_wallet:
        raise RuntimeError("AUTO_TRADE=1 with DRY_RUN=0 requires PRIVATE_KEY")

    return settings


SETTINGS = load_settings()


# Bridge SOCKS5_PROXY → HTTPS_PROXY/HTTP_PROXY env vars for the synchronous
# code paths (py-clob-client uses ``requests``, which auto-picks up these
# env vars when ``requests[socks]`` / PySocks is installed). aiohttp does
# NOT respect HTTPS_PROXY for SOCKS5 — it gets a ProxyConnector explicitly
# in PolyClient.connect. We only set the env vars if the user hasn't
# already chosen a different proxy upstream.
if SETTINGS.socks5_proxy:
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        if not os.environ.get(var):
            os.environ[var] = SETTINGS.socks5_proxy
    # Keep loopback / localhost off the proxy so internal calls don't loop.
    if not os.environ.get("NO_PROXY"):
        os.environ["NO_PROXY"] = "localhost,127.0.0.1"
