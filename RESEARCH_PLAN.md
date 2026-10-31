# Questions this dataset can answer

## Measurements and coverage

- Volume is reported lifetime dollar trading volume, not deposits, profit, or platform revenue.
- Existing yearly/monthly tables assign lifetime volume to actual market close dates. They are not transaction-date trading totals.
- Completed duration is reported start to actual close, in days. Open markets have no completed duration. Start timestamps are not necessarily first-trade timestamps.
- The market CSV includes positive-volume markets only. Studying whether a listing attracts any trading requires extracting zero-volume records from the saved SQLite database too.
- Historical coverage, category labels, and market formats can change. The current year is incomplete, and settlement enrichment is incomplete.
- Several questions in one event, and recurring questions in one series, are related observations rather than independent bets.

## Useful descriptive projects using the existing data

1. **Growth decomposition:** separate market-count growth from average volume per market. Total volume equals market count times average market volume for the same population. Use the same closing-year definition throughout.
2. **Typical versus exceptional markets:** compare category medians, quartiles, averages, and top-1% shares. A few huge markets can move the average substantially.
3. **Recurring markets versus one-off events:** compare sports/crypto series with major elections, using consistent event and market groupings.
4. **Duration and volume:** compare distributions of completed duration and lifetime volume within categories and closing years. Lifetime volume divided by duration is an average-turnover proxy, not observed daily volume.
5. **Concentration over time:** measure how much volume the largest markets account for within each category and year.
6. **Data-quality audit:** count missing dates, negative durations, unknown categories, repeated titles, and settlement-label coverage before interpreting unusual results.

## Dependent and independent variables

The **dependent variable** is the outcome being explained or predicted. **Independent variables** are model inputs; their name does not establish that they cause the outcome.

| Research question | Dependent variable | Possible inputs | What is possible now? |
| --- | --- | --- | --- |
| What is associated with larger markets? | Lifetime volume, or log(volume + 1) | Category, series, closing year, completed duration | Retrospective association using the full market CSV |
| What is associated with longer markets? | Completed duration in days | Category, series, reported start month/year | Retrospective association; exclude open markets from completed-duration labels |
| Will a new market attract trading? | Any trading: yes/no | Features recorded at listing | Requires zero-volume records and a defined observation period |
| Which markets will attract future volume? | Volume added in a fixed future period | Earlier volume, quoted spread/depth, category, time remaining | Requires repeated timestamped snapshots or historical transactions |
| Are market probabilities accurate? | Resolved outcome | Probability recorded before resolution | Requires historical prices and dependable outcome labels |
| Would a strategy earn money? | Realized net profit or return | Executable prices, holdings, exits, fees | Requires a defined strategy and transaction/quote history |

## Recommended first model

Start with a retrospective model of **log(lifetime volume + 1)** using category, closing year, and log(completed duration + 1). Compare category distributions and report effect sizes; do not interpret the model as causal or as a trading strategy.

To make a genuine forecast, define the prediction time and horizon first. Use only information available at that time. Actual close dates, completed duration, final prices, settlement status, and final lifetime volume cannot be used as if they were known when a market opened.

Evaluate on later data, compare with simple category-median baselines, and keep related event/series observations from creating misleading train/test overlap. Report errors, uncertainty, and changes in coverage. A model that explains historical volume has not demonstrated profitable betting.

## Low-cost next collection step

Track a selected set of markets at regular intervals, recording timestamped prices, cumulative volume, liquidity, and executable quote depth where available. This can provide a defensible future-volume or price-calibration study without requesting settlement information for every historical condition again. Backfilled historical availability must be checked separately.
