#!/usr/bin/env python3
"""Polybot live dashboard — TUI for SSH sessions.

Run on the server:

    /root/polybot/venv/bin/python /root/polybot/scripts/dashboard.py

Refreshes every 5 seconds. Ctrl+C to exit.

Sections:
  * System status — polybot.service + polybot-analyze.timer health and timestamps
  * Mode panel — DRY-RUN/LIVE, wallet status, key risk caps from .env
  * Last tick — tick number, candidate count, tracking size, oldest first-seen
  * Top candidates — latest tick top 15 by score, with live drift since first seen
  * Paper PnL — by detector, totals (drift × size_usd as proxy for realised PnL)

The dashboard is purely read-only: it reads CSVs / JSON state / systemctl
status. It never writes anywhere and never talks to Polymarket directly.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from time import sleep
from typing import Dict, Iterable, List, Optional, Tuple

from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

import urllib.error
import urllib.request

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
CAND_CSV = LOG_DIR / "candidates.csv"
TRACK_CSV = LOG_DIR / "price_track.csv"
STUDY_STATE = LOG_DIR / "study_state.json"
POSITIONS_FILE = LOG_DIR / "positions.json"
EQUITY_FILE = LOG_DIR / "equity_history.jsonl"
ENV_FILE = PROJECT_ROOT / ".env"

# Unicode block-char sparkline alphabet (8 levels low-to-high)
SPARK_CHARS = "▁▂▃▄▅▆▇█"

REFRESH_SEC = 2
CLOB_HOST = "https://clob.polymarket.com"
LIVE_FETCH_TIMEOUT = 3.0  # seconds — dashboard tolerates a slow API; we'll fall back to track CSV.


# ---------- subprocess helpers ----------------------------------------------


def _systemctl(*args: str) -> str:
    try:
        r = subprocess.run(
            ["systemctl", *args],
            capture_output=True, text=True, timeout=3,
        )
        return r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def unit_active(unit: str) -> str:
    out = _systemctl("is-active", unit)
    return out or "unknown"


def unit_property(unit: str, prop: str) -> str:
    return _systemctl("show", unit, f"--property={prop}", "--value")


def timer_next_run(unit: str) -> Optional[datetime]:
    """Parse the next-trigger wall-clock time for a timer.

    `NextElapseUSecRealtime` is empty when the timer uses monotonic
    intervals (OnUnitActiveSec), so we fall back to `systemctl list-timers`
    output, which always shows a wall-clock time.
    """
    out = _systemctl("list-timers", unit, "--all", "--no-pager")
    import re
    for m in re.finditer(
        r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\d{4}-\d{2}-\d{2}) "
        r"(\d{2}:\d{2}:\d{2}) UTC",
        out,
    ):
        try:
            return datetime.strptime(
                f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ---------- file readers ----------------------------------------------------


def read_env() -> Dict[str, str]:
    if not ENV_FILE.exists():
        return {}
    out: Dict[str, str] = {}
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip()
    return out


def load_study_state() -> dict:
    if not STUDY_STATE.exists():
        return {"tick_id": 0, "tracked_tokens": {}}
    try:
        return json.loads(STUDY_STATE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"tick_id": 0, "tracked_tokens": {}}


def load_equity_samples(n: int = 200) -> List[dict]:
    """Tail-read up to last ``n`` samples from logs/equity_history.jsonl.
    Returns [] if the file is missing or unreadable.
    """
    if not EQUITY_FILE.exists():
        return []
    try:
        with EQUITY_FILE.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return []
    out: List[dict] = []
    for line in tail.splitlines()[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def sparkline(values: List[float], width: int = 50) -> str:
    """Unicode-block sparkline. Resamples to ``width`` if needed."""
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


def load_positions() -> dict:
    """Read PositionRegistry's persisted state. Returns ``{open: [], closed: []}``
    when the file is missing or unreadable — the panel just shows empty.

    File only appears once a real (non-dry-run) order has filled, so on
    the current DRY_RUN=1 box this returns the empty fallback every time.
    """
    if not POSITIONS_FILE.exists():
        return {"open": [], "closed": []}
    try:
        blob = json.loads(POSITIONS_FILE.read_text())
        return {"open": blob.get("open", []), "closed": blob.get("closed", [])}
    except (json.JSONDecodeError, OSError):
        return {"open": [], "closed": []}


def stream_candidates(path: Path) -> Tuple[List[Dict[str, str]],
                                            Dict[str, Dict[str, str]],
                                            int]:
    """Single-pass scan of candidates.csv. Returns ``(latest_tick_rows,
    first_per_token, total_rows)``. Avoids holding the entire CSV in
    memory — at 268k+ rows the full read is ~150MB and OOMs the 2GB box.

    Algorithm: stream once, track max tick_id seen so far, accumulate
    rows belonging to that tick (drop earlier-tick rows when a new max
    appears), and the first-seen-per-token map. Memory stays bounded
    by ``unique_tokens × row_size`` regardless of file growth.
    """
    if not path.exists():
        return [], {}, 0
    first: Dict[str, Dict[str, str]] = {}
    max_tick = -1
    latest_rows: List[Dict[str, str]] = []
    total = 0
    try:
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                total += 1
                # Track first-seen per token (smallest tick_id).
                tid = row["token_id"]
                try:
                    t = int(row["tick_id"])
                except (TypeError, ValueError):
                    continue
                existing = first.get(tid)
                if existing is None or t < int(existing["tick_id"]):
                    first[tid] = row
                # Track latest tick rows.
                if t > max_tick:
                    max_tick = t
                    latest_rows = [row]
                elif t == max_tick:
                    latest_rows.append(row)
    except OSError:
        pass
    return latest_rows, first, total


def stream_latest_per_token(path: Path) -> Dict[str, Dict[str, str]]:
    """Single-pass scan of price_track.csv → latest-per-token. Memory
    bounded by unique tokens (currently ~1100), not file size (223MB).
    """
    if not path.exists():
        return {}
    out: Dict[str, Dict[str, str]] = {}
    try:
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                tid = row.get("token_id")
                if not tid:
                    continue
                existing = out.get(tid)
                try:
                    t = int(row["tick_id"])
                except (TypeError, ValueError):
                    continue
                if existing is None or t > int(existing["tick_id"]):
                    out[tid] = row
    except OSError:
        pass
    return out


# ---------- formatting helpers ----------------------------------------------


def _f(value: str, default: float = 0.0) -> float:
    try:
        return float(value) if value not in ("", None) else default
    except ValueError:
        return default


def _detector(reason: str) -> str:
    if reason.startswith("end_date"):
        return "date_expired"
    if reason.startswith("extreme price"):
        return "extreme_priced"
    if reason.startswith("bundle "):
        return "bundle_arb"
    if reason.startswith("mm "):
        return "mm_spread"
    return "unknown"


def fetch_live_midpoints(token_ids: List[str]) -> Dict[str, float]:
    """Batch-fetch current midpoints from CLOB for the given tokens.

    Returns a dict ``{token_id: midpoint}`` for tokens the API returned
    (closed / unknown tokens silently absent). Failure modes (timeout,
    HTTP error, JSON decode) all return an empty dict — the caller falls
    back to the latest price_track row, so the dashboard stays usable
    even if the CLOB API is unreachable.

    Used by the candidates table to show drift against the current
    market mid, not the bot's last-tick proxy (the scanner ticks every
    600s; this fetch keeps the dashboard's drift column live between
    ticks).
    """
    if not token_ids:
        return {}
    payload = json.dumps([{"token_id": t} for t in token_ids]).encode()
    # CLOB returns 403 to requests without a recognisable User-Agent
    # (default urllib UA "Python-urllib/3.12" is rejected). Anything that
    # looks like a normal client passes.
    req = urllib.request.Request(
        f"{CLOB_HOST}/midpoints",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "polybot-dashboard/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=LIVE_FETCH_TIMEOUT) as r:
            raw = json.loads(r.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
            json.JSONDecodeError, OSError):
        return {}
    out: Dict[str, float] = {}
    if isinstance(raw, dict):
        for tid, val in raw.items():
            try:
                out[str(tid)] = float(val)
            except (TypeError, ValueError):
                continue
    return out


def _compute_pnl(reason: str, entry: float, current: float, size: float,
                 edge_pct: float) -> float:
    """PnL accounting matched to ``analyze.compute_pnl``.

    Directional formula for non-mm detectors. mm_spread uses the
    round-trip-spread-capture model ``size * edge_pct / 100`` — see
    ``analyze.compute_pnl`` docstring for why the directional formula
    produces phantom PnL on mm_spread (e.g. illiquid 0.03 longshots
    that resolve 1.0 booking ~3200% directional return that no MM
    strategy would have captured).
    """
    if entry <= 0:
        return 0.0
    if _detector(reason) == "mm_spread":
        return size * edge_pct / 100.0
    return size * (current - entry) / entry


def fmt_status(status: str) -> Text:
    if status == "active":
        return Text("● active", style="bold green")
    if status in ("inactive", "dead"):
        return Text("○ inactive", style="dim")
    if status == "failed":
        return Text("● failed", style="bold red")
    if status == "activating":
        return Text("● activating", style="yellow")
    return Text(status or "unknown", style="yellow")


def _parse_systemd_ts(value: str) -> Optional[datetime]:
    if not value or value == "n/a":
        return None
    # systemd format example: "Thu 2026-04-30 08:19:47 UTC"
    try:
        return datetime.strptime(value, "%a %Y-%m-%d %H:%M:%S %Z").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def fmt_relative(dt: Optional[datetime], now: datetime) -> str:
    if dt is None:
        return "—"
    delta = dt - now
    secs = abs(delta.total_seconds())
    sign = "+" if delta.total_seconds() >= 0 else "-"
    if secs < 60:
        body = f"{int(secs)}s"
    elif secs < 3600:
        body = f"{int(secs // 60)}m {int(secs % 60)}s"
    elif secs < 86400:
        body = f"{int(secs // 3600)}h {int((secs % 3600) // 60)}m"
    else:
        body = f"{int(secs // 86400)}d {int((secs % 86400) // 3600)}h"
    return f"{sign}{body}"


def drift_text(entry: float, current: float) -> Text:
    if entry <= 0:
        return Text("—", style="dim")
    pct = (current - entry) / entry * 100.0
    style = "green" if pct > 0 else ("red" if pct < 0 else "dim")
    sign = "+" if pct >= 0 else ""
    return Text(f"{sign}{pct:.2f}%", style=style)


def pnl_text(value: float, width: int = 7) -> Text:
    style = "green" if value > 0 else ("red" if value < 0 else "dim")
    sign = "+" if value >= 0 else ""
    return Text(f"{sign}${value:.2f}".rjust(width), style=style)


# ---------- panels ----------------------------------------------------------


def system_panel(now: datetime) -> Panel:
    bot_active = unit_active("polybot.service")
    bot_pid = unit_property("polybot.service", "MainPID") or "—"
    bot_started = _parse_systemd_ts(
        unit_property("polybot.service", "ActiveEnterTimestamp")
    )

    timer_active = unit_active("polybot-analyze.timer")
    next_run = timer_next_run("polybot-analyze.timer")
    last_run = _parse_systemd_ts(
        unit_property("polybot-analyze.timer", "LastTriggerUSec")
    )

    tbl = Table.grid(padding=(0, 1))
    tbl.add_column(style="bold")
    tbl.add_column()
    tbl.add_row("polybot.service:", fmt_status(bot_active))
    tbl.add_row(
        "  pid / since:",
        Text(
            f"{bot_pid}   started "
            f"{bot_started.strftime('%H:%M UTC') if bot_started else '—'} "
            f"({fmt_relative(bot_started, now)})"
        ),
    )
    tbl.add_row("polybot-analyze.timer:", fmt_status(timer_active))
    tbl.add_row(
        "  last / next:",
        Text(
            f"{last_run.strftime('%H:%M UTC') if last_run else '—'} "
            f"({fmt_relative(last_run, now)}) → "
            f"{next_run.strftime('%H:%M UTC') if next_run else '—'} "
            f"({fmt_relative(next_run, now)})"
        ),
    )
    # Heartbeat line — clock ticks every refresh so user sees the panel
    # is alive even when no underlying numbers happen to move.
    tbl.add_row(
        "Dashboard refresh:",
        Text(
            f"{now.strftime('%H:%M:%S UTC')}  (every {REFRESH_SEC}s)",
            style="bold cyan",
        ),
    )
    return Panel(tbl, title="System", border_style="cyan")


def mode_panel(env: Dict[str, str]) -> Panel:
    dry_run = env.get("DRY_RUN", "1") in ("1", "true", "True", "yes", "on")
    auto_trade = env.get("AUTO_TRADE", "0") in ("1", "true", "True", "yes", "on")
    has_key = bool(env.get("PRIVATE_KEY"))

    mode_text = (
        Text("DRY-RUN", style="bold yellow") if dry_run
        else Text("LIVE", style="bold red")
    )
    auto_text = (
        Text("ON", style="bold magenta") if auto_trade
        else Text("OFF", style="dim")
    )
    wallet_text = (
        Text("configured", style="green") if has_key
        else Text("NOT CONFIGURED", style="bold yellow")
    )

    tbl = Table.grid(padding=(0, 1))
    tbl.add_column(style="bold")
    tbl.add_column()
    mode_line = Text.assemble(mode_text, "  /  auto-trade ", auto_text,
                              "  /  wallet ", wallet_text)
    tbl.add_row("Mode:", mode_line)
    tbl.add_row(
        "Risk caps:",
        Text(
            f"budget=${env.get('TOTAL_BUDGET', '?')}  "
            f"per_trade=${env.get('MAX_PER_TRADE', '?')}  "
            f"daily_loss=${env.get('MAX_DAILY_LOSS', '?')}  "
            f"max_exposure=${env.get('MAX_TOTAL_EXPOSURE', '?')}"
        ),
    )
    tbl.add_row(
        "Scanner:",
        Text(
            f"min_edge={env.get('MIN_EDGE_PCT', '?')}%  "
            f"min_vol24h=${env.get('MIN_24H_VOLUME', '?')}  "
            f"every {env.get('SCAN_INTERVAL_SEC', '?')}s"
        ),
    )
    return Panel(tbl, title="Mode & Risk", border_style="cyan")


def tick_panel(state: dict, latest_cands: List[Dict[str, str]],
               cands_total: int, now: datetime) -> Panel:
    tick_id = state.get("tick_id", 0)
    tracked = state.get("tracked_tokens", {})
    n_track = len(tracked)

    last_tick_ts: Optional[datetime] = None
    if latest_cands:
        try:
            last_tick_ts = datetime.fromisoformat(latest_cands[0]["ts"])
        except ValueError:
            pass

    oldest_ts: Optional[datetime] = None
    for tok in tracked.values():
        try:
            t = datetime.fromisoformat(tok.get("first_seen_ts", ""))
        except ValueError:
            continue
        if oldest_ts is None or t < oldest_ts:
            oldest_ts = t

    tbl = Table.grid(padding=(0, 2))
    tbl.add_column(style="bold")
    tbl.add_column()
    tbl.add_column(style="bold")
    tbl.add_column()
    tbl.add_row(
        "Current tick:", Text(f"#{tick_id}"),
        "Last tick at:",
        Text(
            f"{last_tick_ts.strftime('%H:%M:%S UTC') if last_tick_ts else '—'} "
            f"({fmt_relative(last_tick_ts, now)})"
        ),
    )
    tbl.add_row(
        "Candidates this tick:", Text(f"{len(latest_cands)}"),
        "Total candidate rows:", Text(f"{cands_total}"),
    )
    tbl.add_row(
        "Tokens tracked:", Text(f"{n_track}"),
        "Oldest first-seen:",
        Text(
            f"{oldest_ts.strftime('%Y-%m-%d %H:%M UTC') if oldest_ts else '—'} "
            f"({fmt_relative(oldest_ts, now)})"
        ),
    )
    return Panel(tbl, title="Tick state", border_style="cyan")


def candidates_table(latest_cands: List[Dict[str, str]],
                     first: Dict[str, Dict[str, str]],
                     latest_prices: Dict[str, Dict[str, str]],
                     live_mids: Dict[str, float]) -> Table:
    n_live = sum(1 for c in latest_cands[:15] if c["token_id"] in live_mids)
    title = (
        f"Top candidates this tick (top 15 of {len(latest_cands)})  "
        f"— price = first-seen → live  [{n_live}/15 live]"
        if live_mids else
        f"Top candidates this tick (top 15 of {len(latest_cands)})  "
        "— price = first-seen → tick (live API unreachable)"
    )
    tbl = Table(
        title=title,
        title_style="bold",
        border_style="green",
        expand=True,
        padding=(0, 1),
        show_lines=False,
    )
    tbl.add_column("#", justify="right", no_wrap=True, min_width=2)
    tbl.add_column("det", no_wrap=True, min_width=14)
    tbl.add_column("side", no_wrap=True, min_width=4)
    tbl.add_column("price", justify="right", no_wrap=True, min_width=13)
    tbl.add_column("drift", justify="right", no_wrap=True, min_width=7)
    tbl.add_column("edge%", justify="right", no_wrap=True, min_width=6)
    tbl.add_column("conf", justify="right", no_wrap=True, min_width=4)
    tbl.add_column("size", justify="right", no_wrap=True, min_width=6)
    tbl.add_column("end", justify="right", no_wrap=True, min_width=6)
    tbl.add_column("market", overflow="ellipsis", no_wrap=True, ratio=1)

    sorted_cands = sorted(
        latest_cands, key=lambda r: _f(r["score"]), reverse=True
    )[:15]
    for i, c in enumerate(sorted_cands, 1):
        tid = c["token_id"]
        first_row = first.get(tid, c)
        entry = _f(first_row["entry_price"])
        if tid in live_mids:
            current = live_mids[tid]
            price_str = f"{entry:.3f}→{current:.3f}"
            price_text: object = Text(price_str, style="bold")
        else:
            track = latest_prices.get(tid)
            current = _f(track["outcome_price"]) if track else entry
            price_text = f"{entry:.3f}→{current:.3f}"
        days_raw = c.get("days_to_end", "")
        end_str = (f"{_f(days_raw):+.1f}d" if days_raw not in ("", None) else "—")
        tbl.add_row(
            str(i),
            _detector(c.get("reason", "")),
            c.get("outcome_name", "")[:6],
            price_text,
            drift_text(entry, current),
            f"{_f(c['edge_pct']):.2f}",
            f"{_f(c['confidence']):.2f}",
            f"${_f(c['size_usd']):.2f}",
            end_str,
            c.get("slug", ""),
        )
    return tbl


def pnl_table(first: Dict[str, Dict[str, str]],
              latest: Dict[str, Dict[str, str]],
              live_mids: Dict[str, float]) -> Table:
    by_det: Dict[str, Dict[str, float]] = defaultdict(
        lambda: {"n": 0, "pnl": 0.0, "size": 0.0, "wins": 0, "drift": 0.0}
    )
    overall = {"n": 0, "pnl": 0.0, "size": 0.0, "wins": 0, "drift": 0.0}
    for tid, c in first.items():
        if tid in live_mids:
            current = live_mids[tid]
        else:
            track = latest.get(tid)
            if track is None:
                continue
            current = _f(track["outcome_price"])
        entry = _f(c["entry_price"])
        size = _f(c["size_usd"])
        if entry <= 0:
            continue
        pnl = _compute_pnl(c.get("reason", ""), entry, current, size,
                           _f(c.get("edge_pct", "0")))
        drift = (current - entry) / entry * 100.0
        det = _detector(c.get("reason", ""))
        for bucket in (by_det[det], overall):
            bucket["n"] += 1
            bucket["pnl"] += pnl
            bucket["size"] += size
            bucket["drift"] += drift
            if current > entry:
                bucket["wins"] += 1

    tbl = Table(
        title=(
            "Paper PnL — first-seen → live (mm_spread uses round-trip "
            "spread-capture model)"
            if live_mids else
            "Paper PnL — first-seen → last tick (live API unreachable)"
        ),
        title_style="bold",
        border_style="magenta",
        expand=True,
        padding=(0, 1),
    )
    tbl.add_column("detector", width=15)
    tbl.add_column("n", justify="right", width=5)
    tbl.add_column("avg drift", justify="right", width=10)
    tbl.add_column("win%", justify="right", width=6)
    tbl.add_column("deployed", justify="right", width=10)
    tbl.add_column("total PnL", justify="right", width=10)
    tbl.add_column("avg PnL/trade", justify="right", width=14)

    def _row(label: str, b: Dict[str, float]) -> None:
        n = int(b["n"])
        if n == 0:
            tbl.add_row(label, "0", "—", "—", "—", "—", "—")
            return
        tbl.add_row(
            label,
            str(n),
            f"{b['drift'] / n:+.2f}%",
            f"{b['wins'] / n * 100:.1f}%",
            f"${b['size']:.2f}",
            pnl_text(b["pnl"], width=8),
            pnl_text(b["pnl"] / n, width=10),
        )

    for det in ("date_expired", "extreme_priced", "bundle_arb", "mm_spread", "unknown"):
        if det in by_det:
            _row(det, by_det[det])
    tbl.add_section()
    _row("ALL", overall)
    return tbl


def positions_panel(positions: dict, live_mids: Dict[str, float],
                    now: datetime) -> Panel:
    """Open-position view + realized PnL summary from PositionRegistry."""
    open_pos = positions.get("open", [])
    closed_pos = positions.get("closed", [])
    realized = sum((p.get("realized_pnl") or 0.0) for p in closed_pos)
    wins = sum(1 for p in closed_pos if p.get("status") == "resolved_won")
    n_closed = len(closed_pos)

    summary_text = Text.assemble(
        Text(f"open: ", style="bold"),
        Text(f"{len(open_pos)}", style="bold cyan"),
        "  ",
        Text("closed: ", style="bold"),
        Text(f"{n_closed}", style="bold"),
        "  ",
        Text("realized PnL: ", style="bold"),
        pnl_text(realized, width=8),
        "  ",
        Text(f"({wins}W / {n_closed - wins}L)", style="dim"),
    )

    if not open_pos:
        # Compact "no open" view — just the summary line. Useful in DRY_RUN
        # where the registry is always empty.
        body = Text.assemble(
            summary_text, "\n",
            Text("No open positions. ", style="dim"),
            Text(
                "Empty in DRY_RUN — populates after first real fill."
                if not POSITIONS_FILE.exists() else "",
                style="dim italic",
            ),
        )
        return Panel(body, title="Open positions", border_style="blue")

    tbl = Table(
        title=summary_text,
        title_style="",
        border_style="blue",
        expand=True,
        padding=(0, 1),
        show_lines=False,
    )
    tbl.add_column("det", no_wrap=True, min_width=14)
    tbl.add_column("side", no_wrap=True, min_width=4)
    tbl.add_column("entry→live", justify="right", no_wrap=True, min_width=13)
    tbl.add_column("drift", justify="right", no_wrap=True, min_width=7)
    tbl.add_column("size", justify="right", no_wrap=True, min_width=7)
    tbl.add_column("shares", justify="right", no_wrap=True, min_width=7)
    tbl.add_column("age", justify="right", no_wrap=True, min_width=7)
    tbl.add_column("market", overflow="ellipsis", no_wrap=True, ratio=1)

    for p in open_pos:
        tid = p.get("token_id", "")
        entry = float(p.get("entry_price") or 0)
        live = live_mids.get(tid)
        if live is not None:
            price_str = f"{entry:.3f}→{live:.3f}"
            drift_t = drift_text(entry, live)
        else:
            price_str = f"{entry:.3f}→  ?"
            drift_t = Text("—", style="dim")
        # entry_ts is ISO; compute "X h Y m ago" relative to now.
        ts_raw = p.get("entry_ts", "")
        try:
            entry_dt = datetime.fromisoformat(ts_raw)
        except ValueError:
            entry_dt = None
        age_str = fmt_relative(entry_dt, now) if entry_dt else "—"

        tbl.add_row(
            p.get("detector", "?"),
            (p.get("outcome_name") or "")[:6],
            price_str,
            drift_t,
            f"${float(p.get('entry_size_usd') or 0):.2f}",
            f"{float(p.get('shares') or 0):.2f}",
            age_str,
            p.get("slug", ""),
        )

    return Panel(tbl, title="Open positions", border_style="blue")


def equity_panel(samples: List[dict], chart_width: int = 50) -> Panel:
    """Equity curve + current state. Live in live mode, paper-equity in DRY_RUN."""
    if not samples:
        body = Text.assemble(
            Text("no equity samples yet — bot tick hasn't fired since restart",
                 style="dim italic"),
        )
        return Panel(body, title="Equity", border_style="green")

    latest = samples[-1]
    mode = latest.get("mode", "?")
    wallet = float(latest.get("wallet_balance") or 0)
    realized = float(latest.get("realized_pnl") or 0)
    exposure = float(latest.get("open_exposure") or 0)
    fees = float(latest.get("fees_paid") or 0)
    starting = float(latest.get("starting_balance") or 0)
    n_open = int(latest.get("n_open_positions") or 0)
    n_closed = int(latest.get("n_closed_positions") or 0)

    equity_series = [
        float(s.get("wallet_balance") or 0) + float(s.get("open_exposure") or 0)
        for s in samples
    ]

    cur_equity = equity_series[-1]
    if starting > 0:
        net_change = cur_equity - starting
        net_pct = net_change / starting * 100
    else:
        # live mode — compare to first sample
        first_eq = equity_series[0] if equity_series else cur_equity
        net_change = cur_equity - first_eq
        net_pct = (net_change / first_eq * 100) if first_eq > 0 else 0.0

    lo = min(equity_series)
    hi = max(equity_series)

    # Render a sparkline only if there's actual variation. With a fresh
    # bot waiting for first resolution, all samples are identical and a
    # "flat line of ▅ chars" reads as broken to the eye. Show explicit
    # message instead so the user knows the bot is just waiting.
    if hi - lo < 0.005:  # less than half a cent variation
        spark = f"flat — no movement yet ({len(equity_series)} same samples, awaiting first close)"
    else:
        spark = sparkline(equity_series, width=chart_width)

    mode_color = "yellow" if mode == "paper" else "green"
    mode_label = Text("PAPER (DRY_RUN)" if mode == "paper" else "LIVE",
                      style=f"bold {mode_color}")
    net_color = "green" if net_change >= 0 else "red"
    net_sign = "+" if net_change >= 0 else ""

    tbl = Table.grid(padding=(0, 1))
    tbl.add_column(style="bold")
    tbl.add_column()
    tbl.add_row("Mode:", mode_label)
    if starting > 0:
        tbl.add_row("Started:", Text(f"${starting:.2f}", style="dim"))
    tbl.add_row(
        "Wallet / locked:",
        Text(f"${wallet:>6.2f}  /  ${exposure:>5.2f}  ({n_open} open / {n_closed} closed)"),
    )
    tbl.add_row(
        "Equity:",
        Text.assemble(
            Text(f"${cur_equity:>6.2f}", style="bold"),
            Text(f"  ({net_sign}${net_change:.2f}, {net_sign}{net_pct:.2f}%)",
                 style=net_color),
        ),
    )
    tbl.add_row(
        "Realized PnL:",
        Text.assemble(
            Text(f"{'+' if realized >= 0 else ''}${realized:.2f}",
                 style="green" if realized >= 0 else "red"),
            Text(f"  (from {n_closed} closed)", style="dim"),
        ),
    )
    if fees > 0:
        tbl.add_row("Fees paid:", Text(f"−${fees:.2f}", style="dim"))
    tbl.add_row(
        f"Trend ({len(equity_series)}):",
        Text(spark, style=net_color),
    )
    tbl.add_row(
        "Range:",
        Text(f"low ${lo:.2f}  ↔  high ${hi:.2f}", style="dim"),
    )
    return Panel(tbl, title="Equity", border_style=mode_color)


# ---------- main loop -------------------------------------------------------


def render(now: datetime) -> Layout:
    env = read_env()
    state = load_study_state()
    # Streaming reads — at file sizes of 130-220MB the old "load to list"
    # approach OOM-killed the dashboard on 2GB box.
    latest_cands, first, cand_total = stream_candidates(CAND_CSV)
    latest_prices = stream_latest_per_token(TRACK_CSV)
    positions = load_positions()

    # Live midpoints — top-15 candidates AND every open position. Both
    # are bounded sets (top15 by definition, positions practically <50);
    # one batch POST covers both. The PnL aggregate over thousands of
    # historical candidates still uses tick CSV — refreshing all of them
    # every 5s would mean a multi-MB POST for marginal value.
    sorted_cands = sorted(latest_cands, key=lambda r: _f(r["score"]), reverse=True)
    top15_tids = [c["token_id"] for c in sorted_cands[:15]]
    pos_tids = [p.get("token_id") for p in positions.get("open", []) if p.get("token_id")]
    live_targets = list({*top15_tids, *pos_tids})
    live_mids = fetch_live_midpoints(live_targets)

    has_positions = bool(positions.get("open") or positions.get("closed"))
    pos_size = 12 if positions.get("open") else 4
    equity_samples = load_equity_samples(n=200)

    layout = Layout()
    rows: List[Layout] = [
        Layout(name="top", size=8),
        Layout(name="midrow", size=10),  # tick + equity side-by-side
    ]
    if has_positions:
        rows.append(Layout(name="positions", size=pos_size))
    rows.extend([
        Layout(name="cands", size=20),
        Layout(name="pnl", size=10),
    ])
    layout.split_column(*rows)

    layout["top"].split_row(Layout(name="sys"), Layout(name="mode"))
    layout["midrow"].split_row(Layout(name="tick"), Layout(name="equity"))
    layout["sys"].update(system_panel(now))
    layout["mode"].update(mode_panel(env))
    layout["tick"].update(tick_panel(state, latest_cands, cand_total, now))
    layout["equity"].update(equity_panel(equity_samples))
    if has_positions:
        layout["positions"].update(positions_panel(positions, live_mids, now))
    layout["cands"].update(candidates_table(latest_cands, first, latest_prices, live_mids))
    layout["pnl"].update(pnl_table(first, latest_prices, live_mids))
    return layout


def main() -> int:
    once = "--once" in sys.argv[1:]
    if once:
        # Single snapshot to stdout — useful for cron, piping, smoke-tests.
        Console().print(render(datetime.now(timezone.utc)))
        return 0

    if not sys.stdout.isatty():
        sys.stderr.write(
            "dashboard.py needs an interactive terminal — run it directly, "
            "or pass --once for a single snapshot.\n"
        )
        return 2

    console = Console()
    try:
        with Live(
            render(datetime.now(timezone.utc)),
            console=console,
            screen=True,
            refresh_per_second=2,
        ) as live:
            while True:
                sleep(REFRESH_SEC)
                live.update(render(datetime.now(timezone.utc)))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
