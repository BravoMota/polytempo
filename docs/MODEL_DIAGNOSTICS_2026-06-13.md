# Model Diagnostics — Session 2026-06-13

Findings from a live diagnostic session against the production paper ledger
(`polytempo_paper`) and weather DB (`polytempo`) on 2026-06-13, ~00:15–01:00 UTC.
All PnL numbers are snapshots: daily events settle ~00:15 UTC and a single
settle can move totals by thousands (see §2), so treat figures as
point-in-time, not a track record.

## 1. Connectivity: friend's Mac got a NEW Tailscale IP

The Mac dropped off the tailnet on 2026-06-12 and reappeared re-shared with a
**new CGNAT IP: `100.76.158.32`** (was `100.74.116.100` — permanently dead).
Verified: ping ~49 ms via DERP "mad", port 5432 open, passwordless `jnlow`
auth works on all four DBs. All four `POLYTEMPO_*_DATABASE_URL` User-scope env
vars on olivesurface were updated to the new IP. If the node is ever re-shared
again, expect another new IP — check `tailscale status` before debugging.

The paper bot on the Mac kept running through the outage (ticks every 6 h at
00/06/12/18 UTC) and runs the new 14-strategy code.

Cosmetic bug found: `polytempo live` crashes at the end with
`UnicodeEncodeError` ('→' under cp1252) when printing the run summary on a
Windows console. The Markdown report under `reports\` is written before the
crash. Workaround: `$env:PYTHONIOENCODING='utf-8'`.

## 2. Results snapshot (after the 2026-06-12 event settled)

The June 12 event (578854, winner 23°C) settled at ~00:15 UTC mid-session and
flipped bh from −$909 to **+$4,897** — a +$5.8k swing from one event. Earlier
session numbers (total −$8,076) and these disagree only because of that settle.

By model (1,552 settled trades; profiles start at $1,000 each):

| model | settled | win% | realized |
| --- | ---: | ---: | ---: |
| bh  | 542 | 53% | **+$4,897** |
| bhu | 462 | 36% | −$4,718 |
| es  | 548 | 36% | −$4,971 |
| total | 1,552 | 42% | −$4,791 |

This is a **paired comparison**: bhu and es run identical strategies, events,
and sizing — only the model differs. The system-level loss is bhu+es drag,
not bh.

By strategy (all models): `dist_arb_kelly` +555, `argmax_yes` +205,
`mid_band` −54, `topk_no` −74, `dist_arb_tight` −104, `max_roi` −161,
`argmax_no` −245, `max_edge` −461, `edge_band` −634, `topk_yes` −1,901,
`dist_arb` −1,918. `book_arb` and `tail_fade` have zero trades (gates never
triggered); `coverage_band` has 48 open, none settled.

## 3. Calibration: the model's probabilities vs reality

Model probability per trade reconstructed from OPEN rows:
`p_model = yes_ask + edge_pp/100` (YES) or `yes_bid − edge_pp/100` (NO).
Pre-settle snapshot (820 settled trades, all models):

| model claims | n | market said | realized | PnL |
| --- | ---: | ---: | ---: | ---: |
| 5–10% | 84 | 2.7% | **0.0%** | −$1,561 |
| 10–20% | 129 | 8.2% | **3.1%** | −$2,134 |
| 20–35% | 47 | 17.9% | 17.0% | +$485 |
| 50–75% | 191 | 59.1% | 47.1% | −$1,349 |
| 95–100% | 99 | 97.7% | 100% | +$53 |

- When the model claims 5–20%, reality delivers ~2%. The **market mid was
  closer to realized in every bin below 35%**.
- YES trades won 3% of 424 settles (−66% ROI); the YES side buys
  neighbor/tail buckets whose "edge" is manufactured by an over-wide Gaussian.
- Claimed edges >20pp actually **won** (+39.5% ROI, n=35) — contradicts
  `edge_band`'s >25pp "too good to be true" cap. Trades with 0–2pp edge ran
  −49% ROI (spread eats them) — the min-edge floor is too low.

### Anatomy of bh's PnL by distance from the winning bucket (542 settled)

| | on the winner | 1–2°C away | 3°C+ away |
| --- | ---: | ---: | ---: |
| YES buys | **+$7,981** | −$3,553 | −$933 |
| NO buys | −$1,286 | +$2,548 | +$141 |

All profit = "YES on the bucket the forecast points at, NO on its neighbors."
All loss = "YES on neighbors/tails" (fake σ edge) plus "NO on the winner"
(fading its own modal bucket because the too-flat distribution undersells it).
Even profitable bh pays a ~$5.8k σ tax.

## 4. bh prediction per lead vs actual

Reconstructed from bh OPEN rows (the bot's real-time view at each tick).
`*` = matched winner, `~` = modal uncertain (untraded buckets could hold more
mass), `-` = no bh trades at that tick.

| lead | Jun 08 | Jun 09 | Jun 10 | Jun 11 | Jun 12 | Jun 13 | Jun 14 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 54h | - | - | 16° 29% | - | 23° 22%~ | 22° 27% | 19° 28% |
| 48h | - | - | 16° 29% | 16° 30% | 21° 23%~ | 19° 33% | 20° 29% |
| 42h | - | - | 16° 28% | 17° 29%\* | 22° 29% | 21° 28% | - |
| 36h | - | 18° 30% | 16° 29% | 17° 30%\* | - | 19° 35% | - |
| 30h | - | 19° 33%\* | - | 18° 33% | 22° 32% | 22° 32% | - |
| 24h | - | 18° 33% | 18° 33% | 17° 33%\* | 21° 27%~ | 22° 32% | - |
| 18h | - | 18° 36% | 17° 37%\* | 16° 29%~ | 22° 37% | - | - |
| 12h | 16° 34%~ | 19° 41%\* | 16° 43% | - | 22° 39% | - | - |
| **won** | **16°** | **19°** | **17°** | **17°** | **23°** | open | open |

- The mean tracks within ~1°C of the winner at almost every lead (one 2°C
  miss), but the modal bucket flip-flops between adjacent integers, and at the
  final tick bh picked the right bucket only 2/5 times — roughly consistent
  with its claimed ~34–43%, so the *modal* probability is honest; the
  miscalibration lives 2°C+ out where claimed 5–20% realizes ~2%.
- Jun 14 view at time of writing: bh says 19–20°C; the market favors 21°C
  (43¢). bh's distribution (frozen CSV, icon_eu selected, bias-corrected):
  mean 19.81°C, σ 1.35°C.

## 5. Pipeline faults found

1. **Nightly calibration job dead since 2026-06-09** (`calibration_job_state`
   last success 2026-06-09T12:01Z; `calibration_forecast_records` has no
   short-lead rows for June 10+; `calibration_observed_tmax` stops at June 8).
   bhu traded −$4.7k on a "nightly updated" CSV that is actually 4 days stale.
2. **Scraped WU observations disagree with market resolution.** WU snapshot
   max was 20.5°C on Jun 9 and 20.4°C on Jun 10; the markets resolved 19°C and
   17°C. A 3.4°C gap cannot be the resolution sensor — and all 8 weather
   models agreed with the market, not with WU. The snapshot stream
   (`observation_snapshots`) and the daily-summary product
   (`calibration_observed_tmax`, which DID match the Jun 8 winner) are
   different WU products; whichever feeds calibration must be validated
   against actual market resolutions. Wrong truth labels inflate σ and bend
   bias_c far more than the °F artifact below.
3. **°F round-trip artifact** (known since 2026-06-11, fix on hold):
   `fetch_wunderground_observed_tmax` requests `units=e`; the source is whole
   °C (METAR / Polymarket resolves whole °C); Weather.com converts to °F and
   rounds to integer; converting back fabricates decimals in a deterministic
   5-degree sawtooth (17°C→63°F→17.22°C):

   | true °C | 16 | 17 | 18 | 19 | 20 |
   | --- | ---: | ---: | ---: | ---: | ---: |
   | stored | 16.11 | 17.22 | 17.78 | 18.89 | 20.00 |
   | offset | +0.11 | +0.22 | −0.22 | −0.11 | 0.00 |

   Measured across all 128 stored EGLC observations: every value is integer
   °F; offsets are exactly {0, ±0.11, ±0.22}; mean −0.017°C, std 0.157°C.
   Impact: contaminates `bias_c` (corrections are ±0.05–0.12°C — same order as
   the per-day artifact, and seasonal clustering keeps it from averaging out
   in short windows like bhu's), inflates σ by ~2%, and fakes precision.
   It can NOT flip a bucket outcome (0.28 < 0.5 half-width) and barely affects
   model selection (common-mode across models). The round trip is lossless, so
   `round()` recovers the truth exactly — fix is trivial but needs the
   friend's sign-off (collectors run on his Mac), must only feed the updated
   CSV, and must NOT regenerate the frozen `calibration_stats.csv` (bh is the
   experiment control).
4. **Ceiling-row quirk:** `select_ceiling_row` gives a 48.4 h forecast the
   54 h σ row (smallest lead ≥ current). Minor conservative bias; interpolate
   instead.
5. **Snapshot timing gotcha:** events settle ~00:15 UTC; PnL queries minutes
   apart can disagree by thousands. Re-run everything after a settle; never
   mix pre/post-settle numbers.

## 6. Recommendations (priority order)

1. **Pause or fix bhu and es** — they are the entire loss (−$9.7k combined,
   paired against bh's +$4.9k). bhu needs the calibration job restarted plus
   the °F fix; es needs bias correction (it uses raw values) and empirical
   per-lead σ instead of the hard-coded 1.2/1.6/2.0 floors.
2. **Shrink σ empirically.** Fit a single scale factor so claimed
   probabilities match realized hit rates on settled history; longer term,
   calibrate against the integer bucket that actually resolved (the market's
   target), which also sidesteps the °F artifact and the obs-source question.
3. **Validate the obs source against market resolutions** (finding §5.2)
   before trusting any recalibration.
4. **Blend with the market** in logit space
   (`p_final = w·p_model + (1−w)·p_market`); the market beat the model in
   every bin below 35%, while bh's modal disagreement with the market is real
   alpha — a blend keeps the latter and kills the fake tail edge.
5. **Strategy stopgaps until σ is fixed:** stop buying YES at claimed 5–20%
   (≈−$3.7k of losses); raise the min-edge floor (0–2pp trades ran −49% ROI);
   reconsider `edge_band`'s >25pp cap (those trades won); cap
   `dist_arb_kelly`'s tail NO entries (it risks ~3.4% of bankroll at 0.999 to
   win 3¢ exactly where the model is least calibrated).
6. **Use the ensemble, not winner-takes-all** — best-model selection on n≈88
   flip-flops between ukmo_2km and icon_eu per lead (sampling noise);
   inverse-variance-weight the bias-corrected models instead.
7. **More calibration data** — 88 days is thin; Open-Meteo previous-runs API
   goes back years.

## 7. Reproduction notes

Ad-hoc scripts (in `C:\Users\olive\AppData\Local\Temp\`, not preserved):
`pt_perf.py` (per-strategy/model rollup), `pt_diag.py` (calibration bins),
`pt_diag3.py` (bh anatomy), `pt_leadtable.py` (per-lead table),
`pt_artifact.py` (°F offsets). Key reconstruction: OPEN rows carry
`yes_bid/yes_ask/edge_pp/side/lead_hours`; SETTLE/CLOSE rows carry
`payout_usd` and `winning_label`; join on `trade_id`. Event target date =
`date(ts_utc + lead_hours − ε)` (lead is measured to the UTC day-end
boundary).
