# Polymarket collection and analysis

Python 3.12+ equivalents of the supplied Kalshi downloader and twelve-section
analysis script. Public, read-only requests; no API key or wallet required.

## Install and run

```bash
cd /workspace/__Polywantacracker
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python collect_polymarket.py
.venv/bin/python analyze_polymarket.py
```

On other machines, change into the project directory first. Defaults are relative
to the scripts, not the shell's current directory. The analysis window defaults
to five years and is adjustable with `--years 5`.

For a bounded download, then resume:

```bash
.venv/bin/python collect_polymarket.py --max-pages 2
.venv/bin/python collect_polymarket.py
```

To also collect settlement payouts from the requested Data API v2:

```bash
.venv/bin/python collect_polymarket.py --with-resolutions
```

This adds batched `/v2/resolutions?condition=...` requests (20 conditions each).
`--max-resolution-batches 2` bounds that extra work. Repeating the command resumes
unchecked conditions. Cached unresolved rows and misses are refreshed on the next
`--refresh` scan. Analysis separates exact API payouts from price-inferred results.

## Why there are two API hosts

`https://data-api.polymarket.com/v2` supplies activity, trades, positions and
resolution lookups. It has no market catalogue endpoint. To reproduce the
market-level Kalshi dataset, this collector uses the public Gamma catalogue:

- `https://gamma-api.polymarket.com/events/keyset`
- `https://gamma-api.polymarket.com/markets/keyset`
- Optional: `https://data-api.polymarket.com/v2/resolutions`

Both closed and nonclosed listings are scanned. Gamma's opaque `next_cursor`
is sent back as `after_cursor`. These endpoints and fields were checked against
the live API and the official [Python SDK](https://github.com/Polymarket/py-sdk)
(reference commit `7032ed06175b0bbea77b2d9e898bd9f252af2041`). See the
[Data API v2 specification](https://data-api.polymarket.com/v2/openapi.json).
All applicable protocol versions are requested by leaving the version filter unset.

## Files and recovery

The `data/` directory contains:

- `polymarket.sqlite3`: raw API records and transactional progress; keep this to resume.
- `polymarket_markets.csv`: one row per market ID with positive reported volume.
- `polymarket_events.csv`: event tags and recurring-series metadata.
- `polymarket_manifest.json`: export date, scan progress and completeness.

SQLite saves each page and its next cursor in one transaction. Replayed IDs update
existing records. CSVs are regenerated through temporary files, so interruption
cannot append half a page or duplicate rows. SQLite remains the source of truth;
if interrupted during export, run `--export-only` to regenerate the CSVs.
Do not run multiple collectors against the same data directory simultaneously.

A completed scan is not repeated unless you pass `--refresh`. That option rescans
and updates existing IDs, clears cached resolution lookups, and retains old
records, including records no longer returned by the API. For an entirely fresh
dataset use a new directory, e.g. `--data-dir data/new_snapshot`. A long census is
not an atomic snapshot: market state and volume can change during collection,
and an expired server cursor can require a refresh. API-retained history is not
a guarantee of every market ever created. Sample runs are explicitly incomplete.

`--export-only` needs no network. `--page-size` defaults to 100. Errors and
interruption return a nonzero exit status and preserve committed progress.
Network retries cover connection failures, timeouts, HTTP 429 and 5xx responses;
other HTTP errors fail immediately. TLS verification remains enabled.

## Analysis

The script prints and saves `reports/polymarket_analysis.txt` plus CSV tables for:

1. Yearly totals and growth.
2. Monthly volume, spikes and drivers.
3. Category mix over time.
4. Biggest individual markets.
5. Biggest events.
6. Sports events, series and monthly share.
7. Recurring series.
8. Busiest market-close days.
9. Resolution status, Data API payouts and separate price-based inference.
10. Market lifetimes.
11. Volume concentration.
12. Category outliers.

Polymarket volume is reported dollar volume, **not Kalshi contract count**.
Never multiply it by a $1 notional or label it contracts. Monthly and daily
tables assign each market's **lifetime** volume to its actual `closedTime`.
They are not historical trade-flow totals. Only records flagged `closed=true`
with an actual close timestamp in the selected window enter those time tables;
scheduled `endDate` never substitutes for a missing close. State is derived from
API flags, not whether a scheduled date has passed. Reported start dates may not
equal first-trade dates. Current and boundary periods can be partial.

Category uses a reported market/event category when available. Otherwise it uses
the first matching broad tag in this explicit priority: Sports, Politics, Crypto,
Economics, Finance, Science, Technology, Culture, Weather; otherwise Unknown.
`category_source` distinguishes that fallback. All tags remain in the CSV/raw DB.
Historical API category names are retained, so taxonomy changes can affect trends.

Each market contributes once to totals. Its first associated event and that
event's first recurring series define its primary grouping; full relationships
remain in raw JSON. Markets without event IDs stay separate; missing recurring
series are excluded from series rankings rather than treated as one giant series.
Event totals sum collected market volumes, not API event volume plus market volume.

The Data API result requires `status=resolved` and a payout vector with exactly
one 1,000,000 micro-unit winner and all other payouts zero. Missing/split results
remain unclassified. Gamma price inference separately requires a resolved status
and an exact 1/0 price vector; it is not an independently verified payout. Reports
state the sample sizes and flag incomplete collections. Empty and sparse datasets
produce reports without inventing results or dividing by zero.

## Validation

```bash
.venv/bin/python -m unittest -v
```

Tests are offline and cover cursor resume, duplicate IDs, transaction rollback,
malformed pages, stuck cursors, HTTP retry behavior, Data API enrichment, empty
reports, date/state interpretation and analysis calculations. No orders are placed.
