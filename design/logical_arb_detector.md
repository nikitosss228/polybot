# Logical / correlation arb detector — design

Status: design only, no implementation. Drafted 2026-05-01; revised same day after Gamma API structure was empirically verified. **2026-05-03: re-probed economics with correct fee model (zero positive-edge taker arbs); resolved Q5 by inspecting historical resolutions — dormant legs CAN resolve Yes ("Other" wins ~2% of closed neg-risk events). L1 partition arb is structurally dead. First milestone repointed to L1.5.** Implementation begins after the 2026-05-07 checkpoint validates that current detectors lack live-deployable edge (expected outcome).

## Polymarket structure (verified 2026-05-01 via Gamma API)

Three findings that drive the design:

1. **All Polymarket markets are binary** (`len(outcomes) == 2`, always Yes/No or Team1/Team2). There is no native N-outcome categorical market. Categorical events are realised as N separate binary markets, grouped under an Event.

2. **The `/events` endpoint pre-groups related markets.** No NLP/text clustering needed — Polymarket already gives us the linkage. Each event has a `markets[]` array; member markets share an `event.id`/`slug` and (for neg-risk) a `negRiskMarketID`.

3. **Two distinct kinds of events:**

| Kind | Detection field | Guarantee | Example |
|---|---|---|---|
| **α neg-risk** | `event.negRiskMarketID` set; member markets have `negRisk: true` | Exactly one member resolves Yes | "2026 FIFA World Cup Winner" (60 teams), "2026 NBA Champion" (30 teams), "Elon Musk #tweets bucket" (30 ranges) |
| **β themed series** | `negRiskMarketID: None`; members differ only in a parameter encoded in `groupItemTitle` | Logical monotonicity over the parameter, but not mutually exclusive | "BTC above $X on May 1" (11 thresholds), "WTI in May" (20 thresholds), "Trump ends ops by date Y" (12 dates) |

This restructures the detector layers below.

## Empirical probe (2026-05-01, 200 top events)

Pulled `/events` and computed `Σ outcomePrices[Yes]` (using displayed prices as a proxy for ask) across all neg-risk events:

- **84 active neg-risk events** in the top 200 by 24h volume — neg-risk is a common Polymarket structure.
- Most events show `Σ ≈ 1.00 to 1.05` — the expected band given that displayed price ≈ mid and ask sums are slightly higher.
- **7 events showed displayed Σ < 0.995**, four of them with gaps >1%:
  - `venezuela-leader-end-of-2026` — Σ=0.932, gap=+0.068 (6.8%)
  - `next-prime-minister-of-israel` — Σ=0.961, gap=+0.039 (3.9%)
  - `xrp-price-on-may-1` — Σ=0.970, gap=+0.030 (3.0%)
  - `sea-pis-lec-2026-05-01` (IPL match) — Σ=0.985, gap=+0.015 (1.5%)

Caveats: displayed price is not the same as ask; real ask Σ will be higher, eroding much of the gap. Liquidity per leg is unverified — apparent arbs may only fill at small size before the gap closes. Capital is locked until resolution (Venezuela 2026 = ~8 months). Fees still TBD. But the existence of multiple gaps >3% on displayed prices is enough signal to justify building the detector — even after a 50–70% haircut, edge remains.

Also note the **opposite** outliers (Σ much greater than 1, e.g. Republican 2028 at 1.488, Colombia 1.508): these aren't directly arbable in the same way (you'd need to short Yes on each, equivalently buy No), but they confirm that displayed sums can drift far from 1.0 — a useful sanity-check for the detector's threshold tuning.

## Re-probe (2026-05-03, top 400 events) — corrected economics

The 2026-05-01 probe was systematically optimistic. Three corrections from the API:

1. **Fees are not zero.** Each market carries `feeType` and `feeSchedule` fields. By category:
   - Sports (`sports_fees_v2`): **3% taker**, 25% maker rebate, `takerOnly: true`
   - Politics (`politics_fees`): **4% taker**, 25% rebate
   - Culture/weather (`culture_fees`, `weather_fees`): **5% taker**, 25% rebate

   The `client.get_fee_rate_bps(token_id) → 1000` value is a max-fee cap; `feeSchedule.rate` is the realised cost.

2. **Use `bestAsk`, not `outcomePrices[Yes]`.** Mid-price (`outcomePrices`) is what the original probe summed. Real taker cost is `Σ_bestAsk × (1 + fee_rate)`. Σ_bestAsk runs 1–5 cents above Σ_mid for events with sparse / illiquid legs.

3. **Dormant legs are still in the partition.** Events like FIFA WC have placeholder member markets ("Team AM", "Team AI", "Other") with `active: false, bestAsk: 1.0, bestBid: 0`. Whether these can structurally resolve Yes is **unverified** — see Q5 below. Conservative math (assume they can) means counting their `bestAsk=1.0` in Σ, which alone pushes most events out of the arb band.

### Result

- **195 active neg-risk events** (top 400 by vol24h, scanned 2026-05-03 ~08:30 UTC).
- **Edge_taker > 0.5% after correct fees, all-legs Σ: 0 events.**
- The best raw `Σ_ask_all` is ~0.99 (`highest-temperature-in-miami`); after 5% weather fee → −4% edge.
- Mainstream sports events (EPL match outcomes, n=3) cluster at Σ_ask ≈ 1.010 + 3% fee → −4% edge. Political n=128 events (presidential nominees) sit at Σ_displayed ≈ 1.0–1.05 + 4% fee → −5% to −10% edge.
- The doc's prior best candidate `venezuela-leader-end-of-2026` re-evaluates to edge_taker = −42.7% under conservative dormant-leg handling (most legs return `bestAsk: null`, counted as 1.0).

### Maker-side picture (for context)

Σ_bid_all is typically 0.99–1.01 across the same set, plus 25% fee rebate on filled maker orders. Construction is viable in principle:

- Post limit buy at `bestBid_i` on every leg simultaneously.
- Wait for fills. If all fill: cost ≈ Σ_bid, plus rebate; profit ≈ 1 − Σ_bid + 0.25 × fee × Σ.
- Realised edge depends entirely on fill rate, which is **unobservable in dry-run** — every order would sit on the book unfilled forever in study mode.

This means maker-arb cannot be validated offline the way taker-arb can. Either (a) accept partial validation (verify Σ_bid math, defer fill-rate to first $20 live trial), or (b) skip partition arb entirely and prioritise L1.5.

### Implications for the rollout plan

- L1 (`detect_partition_arb`) **as drafted is not deployable** without resolving Q5. With Q5 unresolved, every partition is uncovered → bundle loss risk on dormant resolves; with Q5 resolved (dormant legs structurally can't pay out), the maker-side path becomes the only viable execution model.
- L1.5 (monotonic series, 2-leg) is unaffected by the dormant-leg question and gets the same fee correction. Worth probing as a parallel candidate.

## Goal

Find market configurations where implied probabilities violate logical bounds by enough that, after fees, a risk-free position is available. Output: a candidate to execute (single- or multi-leg bundle).

## Three types of bounds

### A. Partition bound (Σ = 1) — neg-risk events only

For a neg-risk event with N member binary markets (each with own Yes contract): `Σ ask_yes_i ≤ 1 - fees` ⇒ buy Yes on all N members proportionally, locked profit `1 - Σ - fees` per bundle.

Example: "2026 NBA Champion" event has 30 member markets, one per team. If `Σ ask_yes` across all 30 = 0.97, buy Yes on all 30 → exactly one resolves to 1.0, rest are 0. Profit 0.03 per bundle pre-fees.

Structural bound — Polymarket's neg-risk mechanism enforces "exactly one resolves Yes". Most reliable category.

### B. Subset / superset bound (P(A) ≤ P(B) when A ⊆ B)

Where event A entails event B, the price of A must not exceed the price of B.

Examples:
- "Trump wins 2028" ⊆ "Republican wins 2028"
- "Lakers win finals" ⊆ "West conference wins finals"
- "BTC above $100k by April 30" ⊆ "BTC above $100k by May 31" (temporal monotonicity)

If violated: long B-Yes at low price, long A-No at corresponding price. Worst-case profit (when the narrower event resolves Yes) is the floor.

### C. Pairwise / threshold series bounds

Special case of B for series with a numeric parameter. Example: "BTC above $80k by Y", "above $90k by Y", "above $100k by Y" — prices must monotonically decrease with rising threshold. Any "dent" in the series is an arb.

Mechanically detectable from slug parsing (`btc-above-Nk-on-DATE`).

## Three-layer detector architecture

### Layer 1 — `detect_partition_arb` (start here)

Fetch `/events?closed=false&active=true&archived=false` and filter where `negRiskMarketID is not None`. For each such event:
- Sum `ask_yes` across all member markets.
- If `Σ < 1 - fee_threshold`, emit a candidate with N-leg bundle (one Yes leg per member market).

Rationale for going first:
- The grouping is already done in the API — no semantic clustering needed.
- Most reliable arb category (Polymarket's neg-risk mechanism guarantees exactly one resolves Yes).
- Fills a gap in current code: existing `detect_bundle_arb` only handles 2-outcome `Σ < 1` per single market; the inter-market neg-risk version is uncovered.

New client method needed: `PolyClient.fetch_events()` paging through `/events`. Existing `client.py` only uses `/markets`. Small addition (~50 LOC).

### Layer 1.5 — `detect_monotonic_series_arb` (themed events)

For each event with `negRiskMarketID is None` AND members differ in a sortable parameter (price threshold, date), check monotonic ordering of Yes prices:
- "BTC above $X on date Y" — `ask_yes` must monotonically decrease as X increases.
- "X happens by date Y" — `ask_yes` must monotonically increase as Y increases.
- Any out-of-order pair `(market_i, market_j)` where price violates the expected ordering by `> threshold` → 2-leg arb (long the cheap side, long opposite of the expensive side).

Implementation requires parsing `groupItemTitle` to extract the sort key. Two helpers:
- `_parse_threshold(title)` for "$80,000", "↑ $130", "100-119"
- `_parse_date(title)` for "April 30", "by June 30, 2026"
- Event slug or title pattern picks which parser to apply (or skip the event if neither parses cleanly).

This sits between L1 and L2 in difficulty: still uses the API's pre-grouped events, but requires per-event-type parsing. Run after L1 ships.

### Layer 2 — `detect_subset_arb` (cross-event curated)

Reads a curated YAML/JSON of subset → superset links between markets that are NOT in the same event (and therefore not auto-grouped by Polymarket):

```yaml
- subset: "trump-wins-presidency-2028"
  superset: "republican-wins-presidency-2028"
  type: "subset"
- subsets: ["lakers-win-2026-finals", "warriors-win-2026-finals", ...]
  superset: "west-wins-2026-finals"
  type: "subset"
```

For each entry: pull both prices, check `P(subset) > P(superset) + threshold`, emit a 2-leg candidate (long superset, long opposite-of-subset).

Curation: ~50–100 entries to start — major political clusters (US election hierarchy), sports (conference/division/championship), monotonic temporal series.

Why second: edge is larger here (politics often skewed) but requires manual curation and a more involved execution model (two legs across different markets).

### Layer 3 — auto-discovered links (v2, deferred)

NLP embeddings on question text + automated slug parser for monotonic series. Defer until L1 + L2 demonstrate live edge — otherwise premature complexity.

## Integration with existing code

| Existing | What changes |
|---|---|
| `client.py` | Add `fetch_events(...)` paging through `/events?closed=false&active=true&archived=false`. Returns events with their `markets[]` array. ~50 LOC. |
| `models.py` | Add `Event` dataclass: `id`, `slug`, `title`, `neg_risk_market_id: Optional[str]`, `markets: List[Market]`. |
| `models.Candidate` | Add `legs: List[Leg]` for N-leg bundles. Current `pair_outcome` / `pair_price` is the 2-leg special case; migrate. |
| `scanner.DETECTORS` | Add `detect_partition_arb` (L1), `detect_monotonic_series_arb` (L1.5), `detect_subset_arb` (L2). All take a `List[Event]` rather than `List[Market]` — adapt `scan()` signature or add a parallel `scan_events()`. |
| `main.py` | Run both `fetch_active_markets()` (existing) and `fetch_events()` (new) per tick; pass to the right scanners. |
| `study.py` CSVs | `candidates.csv` header is frozen — don't break it. Multi-leg info goes to a new `arb_legs.csv` with FK on `tick_id + token_id` of the primary leg. |
| `analyze.py` | New branch in `compute_pnl` for arbs: `pnl = (1 - Σ_costs) * size_factor`, not directional. |
| `risk.py` | Sizing: for an arb, `size_usd` is **bundle total cost**, not per leg. One bad arb hurts more — capital is locked until resolution. |

## Risk model

| Risk | Mitigation |
|---|---|
| UMA edge cases at resolution (e.g. "Republican wins" ambiguity if independents) | L2 entries pass manual review before being added. L1 unaffected (structural). |
| Partial fill on one leg of the bundle | `post_orders` packages all legs in one HTTP POST but server may accept some / reject others (atomicity unverified). On partial response: immediately `cancel_orders()` on un-filled legs and sell already-filled legs at market. Residual slippage exposure = function of latency between accept and cancel. Cap per-arb size such that worst-case slippage ≤ remaining bundle profit. |
| Fee drag eats the edge | Threshold checked *after* fee accounting, not before. Conservative: 0.5–1% per leg. |
| Mark-to-market swing pre-resolution | Accounting noise, not real risk — at resolution the locked profit is guaranteed if construction was correct. Capital availability over the holding window is the real constraint. |
| Bad L2 curation entry | Per-entry size cap (e.g. ≤5% portfolio); sanity-check new entries by replaying historical CSVs. |

## Open questions

### Resolved 2026-05-01

1. ~~Gamma API structure for categorical markets~~ → No native multi-outcome markets exist; group via `/events` endpoint. **Filter on `event.enableNegRisk == true`** (cleaner than `negRiskMarketID is not None`; both fields are populated together for genuine neg-risk events).

3. ~~Atomic batch execution~~ → `py-clob-client` provides `post_orders(args: list[PostOrdersArgs])`, which packages an array of signed orders and submits them in a **single** `POST /orders` request. **However**, server-side atomicity (all-or-none) is NOT guaranteed by the client wrapper — the server may accept some orders and reject others. The bot must inspect the per-order response and immediately `cancel_orders()` on partial success to limit drift. Acceptable but residual: there's a window where some legs are open and others are being cancelled.

6. ~~Coverage of `/events` vs `/markets`~~ → Top 500 events by vol24h contain 7094 unique member condition_ids; top 500 markets by vol24h contain 500 condition_ids of which only 1 has no event reference (and that one has a populated `events` field — its event is just outside the top-500 window). **Conclusion: `/events` is strictly more complete for the arb detector** — it embeds ALL members of every group regardless of individual member volume. Use `/events` as the primary feed; paginate until events fall below a vol24h floor (~$10k).

### Resolved 2026-05-03

2. **Real fee structure — Polymarket DOES charge trading fees.** Earlier draft (2026-05-01) was wrong; `gas_estimate_per_order = 0.001` is not the full picture. `feeSchedule.rate` per market gives the actual taker fee:

   | Category | feeType | Taker rate | Maker rebate |
   |---|---|---|---|
   | Sports | `sports_fees_v2` | 3.0% | 25% of fee |
   | Politics | `politics_fees` | 4.0% | 25% |
   | Culture (Eurovision, tweets) | `culture_fees` | 5.0% | 25% |
   | Weather (temperature buckets) | `weather_fees` | 5.0% | 25% |

   `takerOnly: true` on all observed schedules — maker-side orders pay 0 fees, get 25% × rate as rebate on fill. Fee is applied per leg fill: total fee on a partition bundle = `fee_rate × Σ_fills` (linear in Σ_cost, NOT multiplied by N legs). For a $100 bundle on FIFA WC: 60 fills × ~$1.67 each × 3% = $3.00 fee. Polygon gas (~$0.001 per leg) is rounding error compared to this.

   **Implication: fee threshold for arb existence is `Σ_ask × (1 + fee_rate) < 1 - gas`. For sports, need Σ_ask < 0.971; politics 0.962; culture/weather 0.952.** The 2026-05-03 re-probe found 0 events meeting this on the taker side.

### Still open

4. **Curation format for L2.** YAML with JSON-schema validation preferred. Defer until L1 ships.

5. ~~**Dormant legs in neg-risk events**~~ → **Resolved 2026-05-03: scenario (b) holds.** Empirical probe of 138 closed neg-risk events (≥3 members each) found **3 events (≈2%) where the "Other" leg resolved Yes**: `next-ceo-of-lululemon`, `next-unc-mens-basketball-head-coach`, `who-will-replace-mullin-as-oklahoma-senator`. All three winners had vol=0, name="Other", active=False at scan time — i.e. matched every dormant-leg signal — and still structurally resolved Yes.

   Implication: `Other` (and similar catch-all placeholder legs) are real partition members. A correct partition bundle MUST cover them. With `bestAsk ≈ 1.0` on those legs, `Σ_ask ≥ 1.0` is guaranteed → **partition-arb on the taker side is structurally infeasible in the general case.** Maker side has the same problem because dormant `bestBid` is typically 0 (no resting bid to lift, and posting our own at 0 doesn't fill).

   Edge case: in events where every leg is a "real" candidate (no `Other`/placeholder), the dormant-leg problem disappears. These are rare in the current event mix but not impossible — e.g. a sports series where every entrant is enumerated. A future detector could whitelist event types with no catch-all member, but that's narrow scope and likely covered better by L1.5.

6. **Per-leg liquidity floor.** For partition arb: every leg must satisfy `accepting_orders=true`, `bestAsk > 0`. Volume floor `volume_24h ≥ X` per leg is TBD — start strict, e.g. $5k, loosen if too few candidates. Less important than Q5.

7. **Maker-side execution model.** Q2 corrections show maker-arb is the only path with positive expected edge on partition events, but fill-rate is unobservable in dry-run. Need a separate decision: accept partial validation and risk a small live trial, or skip maker-arb entirely and rely on L1.5 (monotonic series, where taker math may still close).

## First milestone

**Repointed 2026-05-03 from L1 → L1.5.** Q5 resolution killed L1 partition arb in the general case. L1.5 (`detect_monotonic_series_arb`) is now the entry point.

Why L1.5 doesn't have the same problem:
- Themed series (`enableNegRisk: false`, members differ in a sortable parameter) typically don't include `Other`/catch-all placeholders — the parameter space is enumerated (e.g. price thresholds $80k/$90k/.../$130k), and any value outside the explicit set just doesn't resolve Yes anywhere.
- Detector emits 2-leg trades (long the cheap side, short or buy-No on the expensive side), not N-leg bundles. Fee drag is `2 × fee_rate × leg_size` not `N × …`.
- No structural requirement to "cover all outcomes" — we're trading on a logical inequality, not a partition sum.

Plan for the L1.5 build (study-mode, no execution):
- `client.fetch_events()` (~50 LOC) + `Event` dataclass — same as the original L1 plan.
- `_parse_threshold(title)` and `_parse_date(title)` helpers for `groupItemTitle` strings like "$80,000", "↑ $130", "April 30", "by June 30, 2026". One regex each.
- `detect_monotonic_series_arb` (~150 LOC): for each event with `enableNegRisk: false` AND members parseable to a sort key, sort by key, walk pairs, flag when `ask_yes` violates the expected monotone ordering by ≥ `2 × fee_rate + threshold`.
- Pre-flight probe before coding: pull current themed events, count how many parse cleanly and how many would have flagged a violation today. Decide on threshold from the distribution.
- Log to `arb_legs.csv` (or new `monotonic_legs.csv`) with both legs of the candidate pair.
- Update `analyze.py` with the 2-leg PnL model.

Estimated: 2 days, with the pre-flight probe first (1–2h) to confirm signal density before committing to the full build.

If a week of study mode shows ≥10 real violations with honestly positive EV after fees → validates the approach, move to L2 (curated cross-event YAML). If 0 violations → the parameter-series mechanism is also efficiently arbed; that's a real signal to drop the logical-arb direction entirely and reconsider.
