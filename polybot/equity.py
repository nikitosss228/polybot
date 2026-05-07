"""Equity time-series tracker for live and paper modes.

Records (timestamp, wallet_balance, realized_pnl_cumulative, open_exposure)
samples to ``logs/equity_history.jsonl``. Two record paths:

* ``record_live_sample``: async, polls the wallet's USDC balance via
  PolyClient + reads cumulative realized PnL from RiskState. Used when
  ``DRY_RUN=0`` and a PRIVATE_KEY is configured.

* ``record_paper_sample``: synchronous, computes a synthetic equity
  curve from the study CSVs — starting capital (``TOTAL_BUDGET``) plus
  the paper PnL of all confidently-resolved candidates so far.
  Used in DRY_RUN so the dashboard chart still moves with study data.

Sampling cadence: once per ``run_tick`` (every 600s by default). The
dashboard reads the file each refresh and renders the last N samples
as a Unicode sparkline.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional

from .config import PROJECT_ROOT, SETTINGS
from .logger import get_logger

log = get_logger(__name__)

EQUITY_FILE = PROJECT_ROOT / "logs" / "equity_history.jsonl"
CAND_CSV = PROJECT_ROOT / "logs" / "candidates.csv"
RES_CSV = PROJECT_ROOT / "logs" / "resolutions.csv"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Sample:
    ts: str
    mode: str               # "paper" or "live"
    wallet_balance: float   # cash on hand
    realized_pnl: float     # P&L from CLOSED positions only (sum of close pnl)
    open_exposure: float    # USDC locked in open positions
    n_open_positions: int
    n_closed_positions: int
    starting_balance: float = 100.0
    fees_paid: float = 0.0

    @property
    def equity(self) -> float:
        return self.wallet_balance + self.open_exposure


def _f(v: Any, default: float = 0.0) -> float:
    if v in (None, ""):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
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


def _compute_paper_pnl(entry: float, terminal: float, size: float, detector: str,
                       edge_pct: float) -> float:
    """Same model as scripts/analyze.py:compute_pnl — directional for non-mm,
    round-trip-spread-capture (size * edge/100) for mm_spread."""
    if entry <= 0:
        return 0.0
    if detector == "mm_spread":
        return size * edge_pct / 100.0
    return size * (terminal - entry) / entry


def _compute_paper_pnl_cumulative() -> float:
    """Sum paper PnL across ALL confidently-resolved candidates in study CSVs.
    Mirrors the REALIZED block of analyze.py — only counts trades whose
    market has resolved (near_one → terminal=1.0, near_zero → 0.0).
    """
    if not RES_CSV.exists() or not CAND_CSV.exists():
        return 0.0
    # Load resolutions (token_id → terminal)
    resolutions: dict = {}
    with RES_CSV.open(newline="") as f:
        for row in csv.DictReader(f):
            if not row.get("last_seen_ts"):
                continue
            r = row.get("resolution_reason", "")
            if r == "near_one":
                resolutions[row["token_id"]] = 1.0
            elif r == "near_zero":
                resolutions[row["token_id"]] = 0.0
    if not resolutions:
        return 0.0
    # First-seen candidates per token
    first: dict = {}
    with CAND_CSV.open(newline="") as f:
        for row in csv.DictReader(f):
            tid = row["token_id"]
            existing = first.get(tid)
            if existing is None or int(row["tick_id"]) < int(existing["tick_id"]):
                first[tid] = row
    total = 0.0
    for tid, terminal in resolutions.items():
        c = first.get(tid)
        if c is None:
            continue
        det = _classify_detector(c.get("reason", ""))
        # Exclude mm_spread from paper equity. Its paper PnL formula
        # ``size * edge/100`` assumes round-trip 100% fill rate which is
        # the unanswered question — including it inflates the equity
        # curve by ~$1900 of phantom PnL that won't survive a live test.
        # Directional-only paper PnL is a more honest "what would the
        # bot have made" estimate.
        if det == "mm_spread":
            continue
        total += _compute_paper_pnl(
            entry=_f(c["entry_price"]),
            terminal=terminal,
            size=_f(c["size_usd"]),
            detector=det,
            edge_pct=_f(c["edge_pct"]),
        )
    return total


class EquityTracker:
    def __init__(self, path: Path = EQUITY_FILE) -> None:
        self.path = path

    def append(self, sample: Sample) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(asdict(sample), separators=(",", ":")) + "\n")
        except OSError as exc:
            log.warning("equity_history.jsonl append failed: %s", exc)

    def record_paper_sample(self, registry, rm, sim_wallet=None) -> Sample:
        """Synthetic equity for DRY_RUN.

        If a SimulatedWallet is provided (SIMULATION_MODE), use its actual
        balance — that's the realistic paper-trading wallet decremented
        on each entry+fee and incremented on each resolution payoff.

        Otherwise (pure study mode, no simulation), fall back to
        ``TOTAL_BUDGET + cumulative paper PnL`` excluding mm_spread —
        a coarser estimate from the study CSVs.
        """
        if sim_wallet is not None:
            wallet = sim_wallet.balance
            starting = sim_wallet.state.starting_balance
            fees = sim_wallet.state.cumulative_fees_paid
        else:
            paper_pnl = _compute_paper_pnl_cumulative()
            wallet = SETTINGS.total_budget + paper_pnl
            starting = SETTINGS.total_budget
            fees = 0.0
        # Realized PnL = sum of pnl on closed positions. Positive when wins
        # exceed losses on resolved markets. Independent of currently-locked
        # exposure or wallet drift.
        realized = 0.0
        if registry is not None:
            realized = sum((p.realized_pnl or 0.0) for p in registry.closed_positions)
        s = Sample(
            ts=_now_iso(),
            mode="paper",
            wallet_balance=round(wallet, 4),
            realized_pnl=round(realized, 4),
            open_exposure=round(registry.open_exposure_usd if registry else 0.0, 4),
            n_open_positions=len(registry.open_positions) if registry else 0,
            n_closed_positions=len(registry.closed_positions) if registry else 0,
            starting_balance=round(starting, 4),
            fees_paid=round(fees, 4),
        )
        self.append(s)
        return s

    async def record_live_sample(
        self,
        get_balance: Callable[[], Awaitable[float]],
        registry,
        rm,
    ) -> Sample:
        """Real equity — async wallet balance fetch + RiskManager cumulative."""
        try:
            wallet = await get_balance()
        except Exception as exc:  # noqa: BLE001
            log.warning("get_balance failed for equity sample: %s", exc)
            wallet = 0.0
        realized = rm.state.cumulative_pnl if rm and rm.state else 0.0
        s = Sample(
            ts=_now_iso(),
            mode="live",
            wallet_balance=round(wallet, 4),
            realized_pnl=round(realized, 4),
            open_exposure=round(registry.open_exposure_usd if registry else 0.0, 4),
            n_open_positions=len(registry.open_positions) if registry else 0,
            n_closed_positions=len(registry.closed_positions) if registry else 0,
            starting_balance=0.0,  # live: no fixed starting reference
            fees_paid=0.0,         # live: fees auto-deducted from wallet
        )
        self.append(s)
        return s

    def load_recent(self, n: int = 200) -> List[Sample]:
        if not self.path.exists():
            return []
        # Tail-read: read last ~256KB of file (ample for 200 samples)
        try:
            with self.path.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 256 * 1024))
                tail = f.read().decode("utf-8", errors="ignore")
        except OSError:
            return []
        out: List[Sample] = []
        for line in tail.splitlines()[-n:]:
            try:
                d = json.loads(line)
                out.append(Sample(**d))
            except (json.JSONDecodeError, TypeError):
                continue
        return out


# ---------- Sparkline rendering (no external deps) -------------------------

SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(values: List[float], width: int = 40) -> str:
    """Unicode block-char sparkline. Resamples to ``width`` if needed."""
    if not values:
        return ""
    if len(values) > width:
        step = (len(values) - 1) / max(1, width - 1)
        sampled = [values[int(round(i * step))] for i in range(width)]
    else:
        sampled = list(values)
    lo, hi = min(sampled), max(sampled)
    if hi - lo < 1e-9:
        return SPARK_CHARS[len(SPARK_CHARS) // 2] * len(sampled)
    out = []
    last = len(SPARK_CHARS) - 1
    for v in sampled:
        idx = int(round((v - lo) / (hi - lo) * last))
        out.append(SPARK_CHARS[max(0, min(last, idx))])
    return "".join(out)
