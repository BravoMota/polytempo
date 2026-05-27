# Open-Meteo historical forecast retrieval for daily Tmax calibration

## What Open-Meteo can give you for this task

Your target is not “past weather” in the generic sense. You want, for each settlement day, the forecast that existed earlier, at multiple different lead times, and you want this separately by model so you can compute model- and lead-specific error statistics for daily Tmax.

In Open-Meteo’s product split, that requirement maps to preserving model-run identity. The ordinary Forecast API is a live, stitched product, and the Historical Forecast API is an archived stitched product; both are designed to give you a seamless series, not a clean ledger of “what run X predicted for day Y.” Open-Meteo’s own docs steer exact run-by-run reconstruction to the Single Runs API, fixed 1–7 day lead analysis to the Previous Runs API, and “closest reconstruction of past conditions” to the Historical Forecast API.

There is one major archive-depth constraint that matters immediately. Single Runs has the exact structure you want, but for most models its archive starts only in September 2025; the main exception is ECMWF IFS HRES 9 km, which Open-Meteo says is archived from March 14, 2024. Previous Runs goes back further for many models: most are archived from January 2024, with explicit longer exceptions such as GFS 2 m temperature from March 2021 and JMA GSM/MSM from 2018. Historical Forecast coverage starts around 2021–2022, depending on model and public archive availability.

That means a recent 30-day backfill as of May 24, 2026 is fully feasible with Single Runs for most models, but if you later decide you want, say, a one-year or multi-year sub-daily lead-time history for non-ECMWF models, Open-Meteo will not give you that exact-run archive for most models; you would need either the coarser Previous Runs product or your own rolling archive.

## Which endpoint to use

### Single Runs API (primary)

For your exact goal, the endpoint family to prioritize is the Single Runs API, explicitly documented at [https://single-runs-api.open-meteo.com/v1/forecast](https://single-runs-api.open-meteo.com/v1/forecast). Open-Meteo describes it as the archive that preserves the structure of each individual model run and lets you request a specific run by its UTC initialization time through the required `run` parameter. It accepts the same forecast-style parameters as the normal Forecast API, including `daily`, `hourly`, `timezone`, `start_date`, `end_date`, and `models`, so it fits your “for each run, get the target day’s Tmax” workflow directly.

A minimal Single Runs request pattern for your use case looks like this:

```http
GET https://single-runs-api.open-meteo.com/v1/forecast
  ?latitude=51.5053
  &longitude=0.0553
  &models=MODEL_ID
  &run=2026-05-20T00:00
  &daily=temperature_2m_max
  &timezone=Europe/London
  &forecast_days=7
```

This mirrors the documented Single Runs endpoint and uses the documented daily Tmax field `temperature_2m_max`. Open-Meteo also requires `timezone` when you request daily variables so that the returned day boundaries are local-day boundaries rather than raw UTC buckets.

### Previous Runs API (fallback)

The Previous Runs API is the right fallback when you do not need exact individual runs and can tolerate coarser lead buckets. Open-Meteo documents it on the `previous-runs-api.open-meteo.com` host and says it exposes values at fixed offsets such as `_previous_day1`, `_previous_day2`, and so on up to day 7, where `_previous_day1` means “the value predicted 24 hours before valid time.” That is excellent for daily 24/48/72-hour skill curves, but it is not the endpoint to use if you want 6-hour or 3-hour lead spacing or if you care about specific 00Z/06Z/12Z/18Z runs.

### Historical Forecast API

The Historical Forecast API is useful, but for a different purpose. Open-Meteo says it builds a continuous series by stitching together the initial hours of successive model runs and that this is useful for machine-learning/post-processing workflows and for tracking past conditions closely. It is not the endpoint to use for “all forecasts that existed at lead time X,” because the run identity has already been collapsed into a stitched series.

### Ensemble API (optional)

An optional side route is the Ensemble API, which exposes ensemble members and daily variables, including daily max temperature, but Open-Meteo says individual ensemble-member history is retained for only up to three days, with longer retention reserved for ensemble means and spreads. That makes it useful for uncertainty work, but not the main historical backfill tool for a 30-day deterministic per-run Tmax archive.

## The precision you can achieve

The “precision” you can get out of Open-Meteo for this task has three different layers, and it is worth separating them clearly.

### Lead-time precision

Lead-time precision means how finely you can sample different forecast issuance times. In Single Runs, this is limited by the model’s actual run/update cadence. Open-Meteo documents that:

- ECMWF IFS HRES runs every 6 hours
- GFS every 6 hours
- UKMO Global every 6 hours
- UKMO UKV every hour
- DWD ICON Europe every 3 hours
- DWD ICON D2 every 3 hours

So if you want the finest possible lead curve, you should not force all models into the same cadence; instead, fetch each model at its native run cadence and bucket later into common lead bins if you need comparability. Open-Meteo’s metadata/update docs are built to support exactly this view, exposing fields such as `update_interval_seconds` and `temporal_resolution_seconds`.

### Intra-run time precision

Intra-run time precision means how fine the forecast timeline inside a single run actually is. Open-Meteo normalizes output to a one-hourly series, but the docs stress that the underlying model may only provide native output every 3 or 6 hours after a certain horizon. This matters directly for daily Tmax, because Open-Meteo defines daily Tmax as a simple 24-hour aggregation from hourly values. So a daily Tmax forecast is only as temporally informative as the underlying hourly series behind it.

For example:

- ECMWF IFS HRES stays hourly through 90 hours
- GFS stays hourly through 120 hours
- DWD ICON Europe stays hourly through 78 hours
- UKMO Global switches from hourly to 3-hourly after 54 hours and to 6-hourly after 144 hours

For a 72-hour Tmax case, that means ECMWF HRES, GFS, and ICON Europe are still operating on native hourly output, while UKMO Global is already beyond its native hourly window and Open-Meteo is interpolating from coarser data.

### Archive precision

Archive precision means how far back you can get this exact structure. This is where Open-Meteo’s coverage varies the most by endpoint. If you need true sub-daily lead buckets before September 2025, the practical Open-Meteo answer is mostly ECMWF IFS HRES via Single Runs; if you can tolerate daily lead offsets like 24/48/72 hours, the Previous Runs archive is much deeper for some models, especially GFS temperature.

### UK/London model mix

For a UK/London-style workflow specifically, the model mix is interesting. Open-Meteo exposes UKMO UKV 2 km for the UK and Ireland with hourly updates and a 2-day horizon, which is the finest lead-time spacing you can get there for short-range work. It also exposes UKMO Global 10 km with a 7-day horizon and 6-hour cycles, but Open-Meteo notes that UKMO open data has an additional 4-hour delay and that UKMO Global falls back to coarser temporal output after 54 hours.

Meanwhile, ECMWF IFS HRES 9 km gives you a strong 6-hour global backbone with native hourly output through 90 hours, and DWD ICON Europe 7 km gives you a 3-hour cycle with hourly output through 78 hours. For a 0–48h short-range London study, UKV gives you the densest cadence. For 48–72h, the most attractive Open-Meteo choices on lead granularity and hourly integrity are often ICON Europe and ECMWF HRES; UKMO Global remains useful, but its long-side hourly series is already partly interpolated by then.

## The most accurate retrieval design

### 1. Request explicit models, not blends

Avoid `best_match` and avoid “seamless” provider blends if your end product is a per-model RMSE dictionary. Open-Meteo’s forecast docs say that `best_match` automatically picks the highest-resolution applicable model for the requested location, and that “seamless” modes combine multiple models from the same provider into one continuous prediction. That is great for end-user forecasting, but it pollutes model attribution in a calibration pipeline.

If you want a clean dictionary like `model -> lead bucket -> RMSE`, request an explicit model, not a blended product. If later you want a provider-level skill table, then using a seamless product is defensible—but it is a different object.

### 2. Use daily Tmax with the settlement timezone

Request `daily=temperature_2m_max` with an explicit local timezone matching the settlement convention, for example `Europe/London` for a London contract. Open-Meteo says daily variables require a timezone, and daily Tmax is the documented 24-hour maximum of 2 m air temperature. This keeps your storage compact and aligns your target day with the settlement-style calendar day.

### 3. Crawl by unique run, not by target day

This is where the big efficiency saving lives: crawl by unique run, not by target day. A Single Runs request returns the full forecast horizon of one model run, typically 7–16 days. So if you are backfilling a 30-day analysis window, you do not need “30 target days × all lead buckets” as separate HTTP calls. You only need each unique run once, then you extract all the target dates that fall inside that run’s returned horizon.

For a 30-day window and a 72-hour maximum lookback, that works out to roughly:

- **132 calls** for a 6-hourly model
- **264 calls** for a 3-hourly model
- **792 calls** for a 1-hourly model

Those volumes fit comfortably under Open-Meteo’s documented free-tier limits of 600 per minute, 5,000 per hour, and 10,000 per day, although retries and multi-model expansion can still add up.

### 4. Define lead time carefully

Open-Meteo is explicit that the Single Runs `run` parameter identifies the model’s initialization time, not the time the forecast became usable on the API. The docs say global runs are typically accessible 4–6 hours after initialization, regional runs 1–3 hours after initialization, and UKMO open-data carries an additional 4-hour delay. That means your “72-hour lead” can mean two different things:

- roughly 72 hours since model initialization, or
- a materially shorter “hours before target available to my trading system”

If your calibration is meant to mirror a live workflow, bucketing by availability-time lead is often more honest than bucketing by pure initialization lead.

For current/live operations, Open-Meteo’s metadata/update tooling is useful here. The model updates docs say you can retrieve `last_run_initialisation_time`, `last_run_availability_time`, `temporal_resolution_seconds`, and `update_interval_seconds`, and that metadata calls do not count against daily or monthly limits. That makes the metadata API a good companion for deciding when a new run is actually stable enough to consume.

### 5. Fix elevation and cell selection

Two smaller, but still important, knobs are `elevation` and `cell_selection`. Open-Meteo says it uses a 90 m DEM and statistical downscaling by default, and it lets you override elevation or change how grid cells are selected (land, sea, or nearest). For airport-style contracts, coastal locations, or sites next to water, those settings can materially shift a point forecast, so they are worth holding fixed during calibration instead of leaving them as an unexamined default.

## Availability, delays, and service risk

On reliability, Open-Meteo’s own documentation is candid about a few constraints. It says the archive-style products are served from different storage systems, and for Single Runs specifically it notes that responses may be slower than the real-time forecast API because data comes from a dedicated archive storage layer. The Historical Forecast API also says historical data lives on a different server/storage setup than the live Forecast API.

The model updates page adds an important operational caveat: Open-Meteo runs geographically distributed redundant servers, but they are eventually consistent, not instantly identical. The docs say there can be a window where a model looks updated but not every server has caught up yet, and they recommend waiting 10 extra minutes after availability if you need the most recent forecast. They also say minor delays are fairly common, and delays beyond 20 minutes are specially highlighted on their model update page.

The free and paid services are also not the same operational product. Open-Meteo says the free and commercial APIs run on different servers, paid plans use reserved server instances with a 99.9% uptime target, and the free API carries no uptime guarantee. The current terms also cap free non-commercial usage at 10,000 calls/day, 5,000 calls/hour, and 600 calls/minute, reserve the right to block misuse, and define the free API as non-commercial only. If this becomes a live trading or commercial workflow rather than private/research use, the free tier is not the right contractual footing.

There is also at least one documented sign that archive endpoints can bog down under load. In a February 20, 2025 GitHub issue, a user reported heavy archive API timeouts and said the status page showed a free-API issue while customer API was also affected; the search excerpt for that issue says some API nodes had experienced increased load, causing calls to take longer. That does not prove “hoarding,” but it does support your suspicion that shared-load effects are real, especially for archive-style endpoints.

If this grows into high-volume backfills or stricter production dependence, Open-Meteo itself points to two more robust paths: a paid customer endpoint on dedicated servers, or self-hosting the open-source server. Their pricing page says self-hosting is the practical route for very large workloads, and their home/features pages explicitly pitch self-hosting for high-volume ML and research use cases.

## Practical recommendation for a UK-style Tmax calibration workflow

If your goal is “best accuracy out of Open-Meteo for daily Tmax at multiple lead times”, the most defensible design is this:

1. Use **Single Runs** as the primary source.
2. Request explicit models one at a time.
3. Ask for `daily=temperature_2m_max` in the settlement timezone.
4. Crawl every unique historical run at the model’s native update cadence rather than forcing a universal cadence.
5. Compute your common lead buckets in your own pipeline.

That matches the structure Open-Meteo actually preserves and gives you the finest lead-time curve the service can currently provide.

For a UK/London workflow, I would start with **ECMWF IFS HRES 9 km** as the backbone, because Open-Meteo explicitly calls it the highest-quality global model and it is the only Single Runs archive with materially deeper history, going back to March 2024. I would then add **UKMO UKV 2 km** for the first 0–48 hours, because that is where Open-Meteo gives you the densest UK-native cadence, and add either **DWD ICON Europe** or **UKMO Global** for the 48–72 hour zone depending on whether you care more about 3-hour issuance spacing and native hourly output through 72h (ICON Europe) or UK-provider continuity despite UKMO’s longer delay and post-54h temporal coarsening.

If you later decide that you only need 24h / 48h / 72h buckets rather than exact 3h/6h/1h run spacing, or if you want a much longer historical sample before September 2025 for non-ECMWF models, then switch to the **Previous Runs API** for the backfill stage. That endpoint is explicitly designed for fixed lead-time offsets and has much deeper coverage for some models, especially GFS temperature. For this specific question, though, **Historical Forecast** should stay out of the core calibration path because it discards run identity, which is exactly the thing you need to estimate lead-time error curves correctly.
