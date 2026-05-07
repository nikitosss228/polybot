"""Paper-trading wallet simulator with realistic frictions.

Activated by ``SIMULATION_MODE=1`` env var. Stores state in
``logs/sim_wallet.json`` (persists across restarts so the equity curve
stays continuous).

Frictions modeled:

* **Fees** — per-slug heuristic estimate of Polymarket taker rate
  (sports 0.75%, politics/finance/tech 1.0%, culture/weather 1.25%,
  economics 1.5%, crypto 1.8% — per Polymarket's per-category schedule
  effective 2026-03-23). Maker fills cost 0% and earn a rebate on
  counterparty fees, but this simulator models taker execution.
  Deducted from wallet on each entry. We
  assume hold-to-resolution → no exit fee (UMA redemption is free).

* **Slippage** — entry-fill price is the candidate's quoted entry
  plus a deterministic ±0.5¢ noise term, seeded by ``(token_id,
  tick_id)`` so simulations are reproducible. Worst-case slippage
  caps at ±1¢ on either side.

* **Spread** — already baked in: ``Candidate.entry_price`` is the
  best-ask we'd cross as a taker, so the bid-ask spread is paid via
  the entry price itself, not as a separate term.

What is NOT modeled (intentionally):

* mm_spread round-trip fill rate — the unanswered question we'd
  measure with a real $50 live test. In simulation we treat
  mm_spread entries as directional buy-and-hold (the conservative
  worst-case interpretation), which means the simulator gives a
  *floor* on mm_spread economics, not an estimate of its potential.

* Partial fills — we assume our limit orders fill fully or not at
  all per tick. Real markets have partials but modeling them adds
  noise without changing the headline.

* Front-running / adverse selection — out of scope for a small-cap
  simulation.

Wallet math invariants (per trade):
    entry_cost_usd  = size_usd                           # gross deployed
    fee_paid        = entry_cost_usd * fee_rate(slug)    # taker fee
    wallet         -= (entry_cost_usd + fee_paid)        # locked + fee out
    shares          = size_usd / fill_price              # at simulated fill
    # ... time passes, market resolves ...
    payoff          = shares * terminal_price            # UMA settlement
    wallet         += payoff                             # back to wallet
    realized_pnl    = payoff - entry_cost_usd - fee_paid

Sum of realized_pnl over all closes == wallet - starting_balance.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import PROJECT_ROOT, SETTINGS
from .logger import get_logger

log = get_logger(__name__)

WALLET_FILE = PROJECT_ROOT / "logs" / "sim_wallet.json"


# ---------- fee inference --------------------------------------------------

# Mirrors backtest.py fee heuristics. Order matters — first match wins.
SPORTS_PREFIXES = (
    "lol-", "cs2-", "dota2-", "val-", "atp-", "wta-", "nba-", "mlb-",
    "nfl-", "nhl-", "epl-", "ucl-", "uel-", "sea-", "rusrp-", "bra-",
    "f1-", "fra-", "ger-", "esp-", "ita-", "ned-", "tur-", "rus-",
    "ukr1-", "fra1-", "ger1-", "esp1-", "ita1-", "f-", "rugby-",
    "boxing-", "ufc-", "mma-", "cricket-", "ipl-", "tennis-",
    "golf-", "snooker-", "darts-", "pga-", "mar1-", "spl-",
    "per1-", "arg-",
)
CRYPTO_KEYWORDS = (
    "bitcoin", "ethereum", "btc-", "eth-", "sol-", "xrp-", "bnb-",
    "doge", "ada-", "matic-", "avax-", "trx-", "ltc-", "linkup",
    "shib", "polygon", "chainlink", "memecoin", "cryptocurrency",
    "fdv-", "token-launch",
)
WEATHER_KEYWORDS = ("temperature-in-", "rain-in-", "snow-in-", "weather-")
CULTURE_KEYWORDS = ("eurovision", "oscar", "grammy", "emmy", "tweets",
                    "tour-de-france", "song-contest")
FINANCE_KEYWORDS = (
    "wti-", "xauusd", "xagusd", "spy-", "qqq-", "amzn-", "tsla-",
    "aapl-", "msft-", "googl-", "meta-", "nvda-", "stock-",
    "gas-", "oil-", "gold-", "silver-",
)


def fee_rate_for_slug(slug: str) -> float:
    s = slug.lower()
    for p in SPORTS_PREFIXES:
        if s.startswith(p):
            return 0.0075
    for kw in CRYPTO_KEYWORDS:
        if kw in s:
            return 0.018
    for kw in WEATHER_KEYWORDS:
        if kw in s:
            return 0.0125
    for kw in CULTURE_KEYWORDS:
        if kw in s:
            return 0.0125
    for kw in FINANCE_KEYWORDS:
        if kw in s:
            return 0.01
    return 0.01  # default = politics


# ---------- slippage -------------------------------------------------------


def deterministic_slippage(token_id: str, tick_id: int, max_cents: float = 0.005) -> float:
    """Reproducible signed slippage in price units. Seeded by (token_id, tick).

    Returns a value in [-max_cents, +max_cents]. Average over many trades
    cancels to zero, but per-trade the bot may have paid 0.5¢ more than
    quoted. Mimics queue-position / latency effects we can't measure
    precisely.
    """
    h = hashlib.sha256(f"{token_id}:{tick_id}".encode()).digest()
    # Use first 4 bytes as a uniform [0, 1) → scale to [-max, max]
    raw = int.from_bytes(h[:4], "big") / 0xFFFFFFFF
    return (raw * 2 - 1) * max_cents


# ---------- wallet ---------------------------------------------------------


@dataclass
class WalletState:
    starting_balance: float = 100.0
    balance: float = 100.0
    cumulative_fees_paid: float = 0.0
    n_trades_opened: int = 0
    n_trades_closed: int = 0
    last_update_ts: str = ""
    insufficient_funds_skips: int = 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SimulatedWallet:
    """USDC wallet for paper-trading simulation. Disk-backed."""

    def __init__(self, path: Path = WALLET_FILE,
                 starting_balance: float = 100.0) -> None:
        self.path = path
        self.state = self._load(starting_balance)

    def _load(self, starting_balance: float) -> WalletState:
        if not self.path.exists():
            log.info("SimulatedWallet: fresh start at $%.2f", starting_balance)
            return WalletState(starting_balance=starting_balance, balance=starting_balance)
        try:
            blob = json.loads(self.path.read_text())
            allowed = {f for f in WalletState.__dataclass_fields__}
            clean = {k: v for k, v in blob.items() if k in allowed}
            ws = WalletState(**clean)
            log.info("SimulatedWallet: loaded balance=$%.2f (started $%.2f, "
                     "%d opened / %d closed, fees=$%.2f)",
                     ws.balance, ws.starting_balance,
                     ws.n_trades_opened, ws.n_trades_closed,
                     ws.cumulative_fees_paid)
            return ws
        except (json.JSONDecodeError, OSError, TypeError) as exc:
            log.warning("SimulatedWallet load failed (%s); fresh start", exc)
            return WalletState(starting_balance=starting_balance, balance=starting_balance)

    def _save(self) -> None:
        self.state.last_update_ts = _now_iso()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(asdict(self.state), indent=2))
        except OSError as exc:
            log.warning("sim_wallet.json save failed: %s", exc)

    # ---- transaction primitives -------------------------------------------

    def can_open(self, size_usd: float, fee: float) -> bool:
        return self.state.balance >= size_usd + fee

    def record_open(self, size_usd: float, fee: float, slug: str) -> None:
        if not self.can_open(size_usd, fee):
            self.state.insufficient_funds_skips += 1
            self._save()
            raise ValueError(f"sim wallet insufficient: ${self.state.balance:.2f} "
                             f"< ${size_usd:.2f} + ${fee:.2f} fee")
        self.state.balance -= (size_usd + fee)
        self.state.cumulative_fees_paid += fee
        self.state.n_trades_opened += 1
        self._save()
        log.info("sim_wallet OPEN: -$%.2f (size %.2f + fee %.4f) = $%.2f balance "
                 "(slug=%s)", size_usd + fee, size_usd, fee, self.state.balance, slug)

    def record_close(self, payoff: float, slug: str) -> None:
        self.state.balance += payoff
        self.state.n_trades_closed += 1
        self._save()
        log.info("sim_wallet CLOSE: +$%.2f payoff = $%.2f balance (slug=%s)",
                 payoff, self.state.balance, slug)

    # ---- queries ----------------------------------------------------------

    @property
    def balance(self) -> float:
        return self.state.balance

    @property
    def total_pnl(self) -> float:
        return self.state.balance - self.state.starting_balance

    def summary(self) -> dict:
        return {
            "starting": self.state.starting_balance,
            "balance": self.state.balance,
            "pnl": self.total_pnl,
            "fees_paid": self.state.cumulative_fees_paid,
            "n_opened": self.state.n_trades_opened,
            "n_closed": self.state.n_trades_closed,
            "insufficient_skips": self.state.insufficient_funds_skips,
        }


# ---------- fill simulation ------------------------------------------------


def simulate_entry(
    candidate_entry_price: float,
    token_id: str,
    tick_id: int,
    slug: str,
    size_usd: float,
) -> dict:
    """Compute simulated entry fill: filled_price (with slippage), fee, shares.

    Returns dict ``{filled_price, fee, shares, total_cost}``.
    """
    slip = deterministic_slippage(token_id, tick_id)
    filled_price = max(0.001, min(0.999, candidate_entry_price + slip))
    fee_rate = fee_rate_for_slug(slug)
    fee = size_usd * fee_rate
    shares = size_usd / filled_price if filled_price > 0 else 0.0
    return {
        "filled_price": round(filled_price, 6),
        "fee": round(fee, 6),
        "fee_rate": fee_rate,
        "shares": round(shares, 6),
        "total_cost": round(size_usd + fee, 6),
        "slippage": round(slip, 6),
    }
