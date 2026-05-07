# Sample data

Two CSVs from the project author's run, downsampled so you can clone the repo
and immediately run a backtest without waiting weeks to collect your own data.

| File | Rows | Size | Source |
|---|---:|---:|---|
| `sample_candidates.csv` | 5,000 | 2.0 MB | First N rows of `logs/candidates.csv` from a paper-trading run starting 2026-04-30 |
| `sample_resolutions.csv` | 468 | 110 KB | Subset of `logs/resolutions.csv` filtered to tokens that appear in the candidates above |

All fields are public Polymarket data: condition IDs, token IDs, market slugs,
prices, edges, timestamps. No wallet addresses or other identifying info.

## Try it

`scripts/backtest.py` reads from `logs/`, so the simplest way to run it on the
sample is to copy these files into place:

```bash
cp examples/sample_candidates.csv logs/candidates.csv
cp examples/sample_resolutions.csv logs/resolutions.csv

python scripts/backtest.py
```

You should see something like:

```
Loaded 454 resolved directional trades (date_expired + extreme_priced).

Baseline (no filter, all directional resolved trades):
  n=454  win=...  deployed=$...  gross PnL=$...  fees paid=$...  NET PnL=$...

Marginal effect — one filter axis at a time (no other filters):
  ...
```

`scripts/category_priors.py` works the same way and emits
`logs/category_priors.json` for the Kelly-sizing path in `risk.py`.

## What this sample does *not* cover

- Live scanner ticks — for that you need to actually run `polybot.main`
  against the live Gamma API. The sample is purely for the backtest harness.
- Long-tailed resolutions. The full `candidates.csv` covers ~2 months and
  563k rows; the sample is the first ~5k. Many tokens here resolve outside
  the sample window, which is why `sample_resolutions.csv` is smaller than
  a proportional slice would be.
- mm_spread sells — the simulator records first-seen prices only; closed
  trades are computed by `sim_wallet.py` at runtime.

## Reproducing your own version

After running the bot for any period of time, your own `logs/candidates.csv`
and `logs/resolutions.csv` will accumulate. Re-create the sample with:

```bash
head -5001 logs/candidates.csv > examples/sample_candidates.csv
# then filter resolutions.csv to matching tokens — see scripts/ for awk one-liner
```
