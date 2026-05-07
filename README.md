# Polybot

A modular backtest harness and edge detector for [Polymarket](https://polymarket.com)
prediction markets. Polybot scans live markets, surfaces candidate trades from
four independent detectors, sizes them with fractional-Kelly priors, and either
logs them (study mode) or executes via the CLOB (live mode). Every candidate is
written to disk so you can run the same backtest later against actual market
resolutions.

This is research infrastructure, not a "make money" bot. Most prediction-market
edges available to retail get eaten by fees; the harness here is for measuring
that honestly. If you have a thesis, this lets you test it against two months
of recorded data without writing the plumbing yourself.

## What it looks like

`scripts/dashboard.py` shows live status — open positions, equity curve,
tick-by-tick candidates, paper PnL by detector. Snapshot from a paper-trading
run:

```
╭───────────────────────── System ─────────────────────────╮╭────────────────────── Mode & Risk ───────────────────────╮
│ polybot.service:       ● active                          ││ Mode:      DRY-RUN  /  auto-trade ON  /  wallet NOT      │
│   pid / since:         51633   started 09:05 UTC (-6h    ││            CONFIGURED                                    │
│                        35m)                              ││ Risk caps: budget=$100  per_trade=$7  daily_loss=$10     │
│ polybot-analyze.timer: ● active                          ││            max_exposure=$80                              │
│   last / next:         14:05 UTC (-1h 35m) → 20:05 UTC   ││ Scanner:   min_edge=2.0%  min_vol24h=$50000  every 60s   │
╰──────────────────────────────────────────────────────────╯╰──────────────────────────────────────────────────────────╯
╭─────────────────────── Tick state ───────────────────────╮╭───────────────────────── Equity ─────────────────────────╮
│ Current tick:   #2228  Last tick at:    15:40:47 UTC     ││ Mode:            PAPER (DRY_RUN)                         │
│ Candidates      52     Total candidate  563565           ││ Started:         $100.00                                 │
│ this tick:             rows:                             ││ Wallet / locked: $ 24.19  /  $73.11  (12 open / 1        │
│ Tokens          99     Oldest           2026-04-30 08:12 ││                  closed)                                 │
│ tracked:               first-seen:      UTC (-7d 7h)     ││ Equity:          $ 97.30  ($-2.70, -2.70%)               │
│                                                          ││ Realized PnL:    +$0.61  (from 1 closed)                 │
│                                                          ││ Fees paid:       −$3.31                                  │
╰──────────────────────────────────────────────────────────╯╰──────────────────────────────────────────────────────────╯
╭─────────────────────────────────────────────────── Open positions ───────────────────────────────────────────────────╮
│                                open: 12  closed: 1  realized PnL:   +$0.61  (1W / 0L)                                │
│ ┏━━━━━━━━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓ │
│ ┃ det            ┃ side ┃    entry→live ┃   drift ┃    size ┃  shares ┃     age ┃ market                           ┃ │
│ ┡━━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩ │
│ │ extreme_priced │ Yes  │   0.903→0.915 │  +1.34% │   $6.25 │    6.92 │ -8h 39m │ will-trump-visit-china-by-may-1… │ │
│ │ extreme_priced │ No   │   0.921→0.903 │  -1.91% │   $6.25 │    6.79 │ -8h 39m │ hantavirus-pandemic-in-2026      │ │
│ │ extreme_priced │ No   │   0.926→0.943 │  +1.78% │   $6.25 │    6.75 │ -8h 39m │ will-china-invade-taiwan-before… │ │
│ │ extreme_priced │ No   │   0.958→0.917 │  -4.30% │   $6.25 │    6.52 │ -8h 39m │ will-donald-trump-announce-that… │ │
│ │ extreme_priced │ No   │   0.950→0.955 │  +0.51% │   $6.25 │    6.58 │ -8h 39m │ will-the-iranian-regime-fall-by… │ │
╰──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
                     Paper PnL — first-seen → live (mm_spread uses round-trip spread-capture model)
┏━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━┓
┃ detector              ┃        n ┃      avg drift ┃      win% ┃       deployed ┃      total PnL ┃      avg PnL/trade ┃
┡━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━┩
│ date_expired          │     3516 │         +0.18% │     64.7% │       $8068.64 │        +$42.75 │             +$0.01 │
│ extreme_priced        │     3914 │         +0.17% │     79.1% │      $10308.14 │        +$27.24 │             +$0.01 │
│ bundle_arb            │        3 │        -99.86% │      0.0% │          $9.76 │         $-9.75 │             $-3.25 │
│ mm_spread             │     3393 │       +336.76% │     46.7% │       $7830.75 │      +$2873.94 │             +$0.85 │
└───────────────────────┴──────────┴────────────────┴───────────┴────────────────┴────────────────┴────────────────────┘
```

The Paper PnL table is "if you bought at first-seen price, where are you now"
and is mode-dependent — `mm_spread` reports a round-trip spread-capture model
rather than realized fills, so its huge `+336% avg drift` is unrealistic as
return. For realized numbers, see the [What the data looks like](#what-the-data-looks-like)
section below or run `scripts/backtest.py`.

## What's in the box

**Four edge detectors** (`polybot/scanner.py`)

| Detector | Signal | Default win-rate prior |
|---|---|---|
| `date_expired` | Markets past their declared end date that still trade non-extremely; UMA hasn't fired yet | 0.80 |
| `extreme_priced` | One side ≥ 0.90 with ≥ $50k 24h volume — high-confidence resolution buys | 0.986 |
| `bundle_arb` | Binary markets where `ask_yes + ask_no < 1` — risk-free if both legs fill | 0.50 |
| `mm_spread` | Liquid markets with wide bid-ask spreads — surfaces but does not auto-trade | 0.50 (study only) |

The detectors are intentionally cheap and conservative. Each returns a
`Candidate` with an explicit `score = edge_pct × confidence` so the scanner
can rank without needing a global threshold.

**Backtest harness** (`scripts/backtest.py`)

Joins recorded candidates against subsequent market resolutions, applies the
real Polymarket fee schedule (categorised per slug — sports 3%, politics/finance
4%, crypto 7.2%), and produces a per-detector and per-(detector, category)
breakdown. Includes a "REALIZED SUBSET" view that filters to candidates whose
markets have actually resolved during the test window, so you don't double-count
open positions.

**Position lifecycle** (`polybot/positions.py`, `polybot/orders.py`)

Open → tracked → soft-resolved (when Polymarket leaves a market `closed=False`
but the price hits 0.999/0.001) → closed. Closed positions feed back into
`RiskManager` for exposure / daily-loss accounting. Soft-resolution detection
catches tournament events that resolve before the declared `endDate`.

**Sizing** (`polybot/risk.py`)

Four modes selectable via `SIZING_MODE`:

- `flat` — every trade = `MAX_PER_TRADE`
- `per_detector` — fixed size per detector
- `edge_scaled` — proportional to `edge_pct / EDGE_SCALE_REF`
- `fractional_kelly` — `f = p − (1−p)·P/(1−P)` capped at `0.5 × KELLY_FRACTION`
   of `TOTAL_BUDGET`. Per-(detector, category) priors loaded from
   `logs/category_priors.json` if present (generated by
   `scripts/category_priors.py`); otherwise falls back to the detector-level
   `WINRATE_*` defaults from `.env`.

**Paper trading** (`polybot/sim_wallet.py`)

When `SIMULATION_MODE=1`, fills are simulated against a virtual USDC wallet
with deterministic slippage seeded by `(token_id, tick_id)` and per-slug fee
classification. State persists to `logs/sim_wallet.json` so you can compare
runs across config changes.

**Risk gates** (`polybot/risk.py`)

`TOTAL_BUDGET`, `MAX_PER_TRADE`, `MAX_TOTAL_EXPOSURE`, `MAX_DAILY_LOSS`, and a
`MAX_DRAWDOWN_PCT` lifetime kill-switch. All gates are suppressed in dry-run
mode on purpose — the point of dry-run is to see what *would* trip them.

## Quickstart

```bash
git clone <your-fork> polybot && cd polybot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
chmod 600 .env
# Leave DRY_RUN=1 and PRIVATE_KEY blank for first run.

python -m polybot.main
```

The first scan will print candidates to stdout and append them to
`logs/candidates.csv`. Let it run for a few hours, then:

```bash
python scripts/dashboard.py        # live status TUI
python scripts/backtest.py         # historical PnL on resolved markets
python scripts/category_priors.py  # rebuild Kelly priors from your data
```

Don't want to wait weeks to collect data? `examples/` ships ~5,000 candidates
+ matching resolutions from the project author's run. Copy them into `logs/`
and the backtest harness produces output immediately:

```bash
cp examples/sample_candidates.csv logs/candidates.csv
cp examples/sample_resolutions.csv logs/resolutions.csv
python scripts/backtest.py
```

## What the data looks like

The headline finding from running these detectors against two months of
Polymarket data: **the surface-level edge gets eaten by fees.** Below are
real `scripts/backtest.py` numbers from the project author's run
(Polygon mainnet, March-May 2026, 5,822 confidently resolved directional
trades).

**Baseline — every candidate, no filter:**

| | gross ROI | fees paid | net ROI |
|---|---:|---:|---:|
| All resolved directional trades (n=5,822) | +1.34% | $645.95 | **−3.15%** |

Win rate is 90.1% — the detectors *do* pick winners. They just don't pick
winners by enough margin to clear the 3-7% taker fee. This is the central
challenge for retail directional trading on Polymarket.

**Per-detector baseline:**

| Detector | n | win | gross ROI | net ROI |
|---|---:|---:|---:|---:|
| `extreme_priced` | 3,280 | 98.2% | +1.50% | −3.45% |
| `date_expired` | 2,542 | 79.7% | +1.12% | −2.73% |
| `mm_spread` | — | — | study only | — |
| `bundle_arb` | — | n=3 in window | — | excluded |

**Best filtered cell (from a 1,608-cell grid sweep):**

| Filter | n | win | gross ROI | net ROI |
|---|---:|---:|---:|---:|
| both detectors, vol ≥ $250k, edge 5-50% | 115 | 81.7% | +4.50% | **+1.21%** |
| `date_expired` alone, vol ≥ $250k | 105 | 80.0% | +4.06% | +0.80% |

Two things to take away. First, volume is the only filter axis that meaningfully
moves net ROI: cutting the universe to vol ≥ $250k is what moves things from
−3% to ~breakeven. Second, the surviving cells are *small* — 115 trades over
two months on a $5 unit means about $3 of net PnL. This is research, not yield.

Numbers depend on your `.env` config and the data you've collected. Re-run
`scripts/backtest.py` after any config change to see your own version of this
table. Each run appends a snapshot of the validated cell to
`logs/backtest_history.jsonl` so you can track how filter changes move the
needle over time.

## Architecture

```
polybot/
  client.py        — async aiohttp wrapper for Polymarket Gamma + CLOB APIs
  config.py        — env-var-driven Settings dataclass (single source of truth)
  scanner.py       — the four detectors; produces ranked Candidate list
  risk.py          — sizing + risk gates; loads category priors from logs/
  positions.py     — open → resolution → close lifecycle
  orders.py        — order submission + fill tracking against CLOB
  mm_exit.py       — sell-side posting for mm_spread positions
  sim_wallet.py    — paper-trading wallet with deterministic slippage
  equity.py        — equity curve sampler for the dashboard
  study.py         — append-only CSV logging for offline analysis
  main.py          — async loop: scan → score → trade → sleep

scripts/
  dashboard.py     — TUI with live equity sparkline
  backtest.py      — joins candidates with resolutions, computes PnL
  category_priors.py — derives per-(detector, category) win rates
  analyze.py       — point-in-time aggregate stats
  simulate.py      — replay candidates.csv through sim_wallet
  probe_*.py       — exploratory scripts for cross-arb, news jumps, etc.

deploy/            — example systemd units for hands-off operation
data/              — curated config (e.g. cross_arb pairs)
```

## Status

- [x] Detectors implemented and tested in dry-run
- [x] Position lifecycle (open → soft-resolved → closed)
- [x] Order tracking with CLOB fill events
- [x] Paper-trading simulator with realistic fees + slippage
- [x] Backtest harness with per-category priors
- [ ] Live trading — code path exists, has not been run with funded wallet
- [ ] L2 cross-event correlation arb (`probe_l2.py`) — research stage
- [ ] News-driven repricing detector (`probe_news.py`) — research stage

## Configuration

All knobs live in `.env`. See `.env.example` for the full list with
explanations. Key things to know:

- `DRY_RUN=1` is the default and will not submit any orders even with a
  funded wallet. Flip to `0` only after reviewing your `candidates.csv`.
- `AUTO_TRADE=0` adds a second safety: even with `DRY_RUN=0`, the bot
  surfaces candidates without trading until you set this to 1.
- `SIMULATION_MODE=1` runs paper-trading against a virtual wallet — useful
  for testing sizing changes without waiting for real resolutions.
- `SOCKS5_PROXY` is optional. Set it if Polymarket is geoblocked from your
  network (e.g. `socks5://user:pass@host:port`).

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgements

Built on top of [py-clob-client](https://github.com/Polymarket/py-clob-client)
and the Polymarket Gamma API (`gamma-api.polymarket.com`). All detector
heuristics, sizing logic, and fee modelling are original.

Polymarket itself has no affiliation with this project.
