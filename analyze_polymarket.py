"""The twelve Kalshi-style research sections, adapted to Polymarket dollar volume."""
import argparse
from contextlib import redirect_stdout
import json
from pathlib import Path

import pandas as pd

from collect_polymarket import FOLDER


def header(text):
    print("\n" + "=" * 90 + "\n" + text + "\n" + "=" * 90)


def show(frame):
    display = frame.copy()
    numeric = display.select_dtypes(include="number").columns
    display[numeric] = display[numeric].round(3)
    print("No qualifying data." if frame.empty else display.to_string())


def ratio(numerator, denominator):
    return numerator / denominator if denominator else float("nan")


def load(path):
    df = pd.read_csv(path, low_memory=False, dtype={"id": "string", "event_id": "string", "series_id": "string"})
    required = {"id", "title", "event_id", "event_title", "series_id", "series_title",
                "category", "category_source", "volume_usd", "closed", "active",
                "accepting_orders", "closed_time", "start_time", "end_time",
                "inferred_result", "resolution_status", "uma_resolution_status"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing collector columns: {', '.join(sorted(missing))}")
    df = df.drop_duplicates("id", keep="last").copy()
    df["volume_usd"] = pd.to_numeric(df["volume_usd"], errors="coerce")
    df = df[df["volume_usd"].gt(0) & df["volume_usd"].lt(float("inf"))].copy()
    for column in ("closed_time", "start_time", "end_time"):
        df[column] = pd.to_datetime(df[column], utc=True, errors="coerce", format="mixed")
    for column in ("closed", "active", "accepting_orders"):
        df[column] = df[column].astype("string").str.lower().map({"true": True, "false": False}).astype("boolean")
    for column in ("category", "title", "event_title", "series_title"):
        df[column] = df[column].fillna("Unknown")
    for column in ("inferred_result", "confirmed_result", "resolution_status", "uma_resolution_status"):
        if column in df:
            df[column] = df[column].astype("string").fillna("")
    # Missing IDs stay separate; never merge all orphaned markets into one event.
    df["event_key"] = df["event_id"].where(df["event_id"].notna() & df["event_id"].ne(""), "market:" + df["id"])
    df["state"] = "unknown"
    df.loc[df["closed"].eq(True).fillna(False), "state"] = "closed"
    df.loc[(df["closed"].eq(False) & df["active"].eq(True) & df["accepting_orders"].eq(True)).fillna(False), "state"] = "open"
    df.loc[(df["closed"].eq(False) & df["state"].ne("open")).fillna(False), "state"] = "not closed / not accepting"
    return df


def analyze(df, years, now, out):
    start = now - pd.DateOffset(years=years)
    finished = df[df["closed"].eq(True).fillna(False) & df["closed_time"].between(start, now)].copy()
    finished["year"] = finished["closed_time"].dt.year
    finished["month"] = finished["closed_time"].dt.tz_localize(None).dt.to_period("M")
    finished["day"] = finished["closed_time"].dt.date
    finished["days_open"] = (finished["closed_time"] - finished["start_time"]).dt.total_seconds() / 86400
    def save(name, frame):
        frame.to_csv(out / f"{name}.csv")
        return frame
    header("DATASET OVERVIEW")
    print(f"Markets with positive reported dollar volume: {len(df):,}")
    print(f"Closed in the last {years} years: {len(finished):,}; window {start.date()} to {now.date()}")
    print(f"Accepting orders and active: {df['state'].eq('open').sum():,}")
    print(f"Closed markets without an actual closedTime (excluded from time tables): {(df['closed'].eq(True) & df['closed_time'].isna()).sum():,}")
    print(f"API category missing; assigned broad tag or Unknown: {df['category_source'].ne('api_category').sum():,}")
    print(f"Missing event IDs (treated as individual markets): {df['event_id'].isna().sum():,}")
    print(f"Closed-window volume: ${finished['volume_usd'].sum():,.2f}")
    print("Volume is lifetime reported USD volume grouped by actual market close, NOT trades occurring that month.")
    print("Dollar volume is not a contract count, liquidity, open interest, profit, or unique money invested.")
    print("endDate is a scheduled date; it is never used as proof that a market closed or resolved.")
    print("Top-market/event/series tables use all collected dates; time tables use the selected window.")
    print("Current year/month are partial; boundary years may be partial. Growth is descriptive, not like-for-like.")

    header("1. YEARLY TOTALS")
    yearly = finished.groupby("year").agg(markets=("id", "count"), events=("event_key", "nunique"),
                dollar_volume=("volume_usd", "sum"), avg_per_market=("volume_usd", "mean"))
    # Include missing calendar years/months so growth never skips a period.
    yearly = yearly.reindex(range(start.year, now.year + 1), fill_value=0)
    yearly.index.name = "year"
    yearly["growth_vs_prior_year"] = yearly["dollar_volume"].div(yearly["dollar_volume"].shift(1).replace(0, float("nan"))) - 1
    yearly["share_of_window_total"] = yearly["dollar_volume"] / (yearly["dollar_volume"].sum() or float("nan"))
    show(save("yearly", yearly))
    nonzero = yearly.loc[yearly["dollar_volume"].gt(0), "dollar_volume"]
    if not nonzero.empty:
        print(f"Latest observed nonzero year / first: {ratio(nonzero.iloc[-1], nonzero.iloc[0]):,.2f}x")

    header("2. MONTHLY VOLUME AND SPIKE DETECTION")
    monthly = finished.groupby("month").agg(markets=("id", "count"), dollar_volume=("volume_usd", "sum"))
    monthly = monthly.reindex(pd.period_range(start.tz_localize(None), now.tz_localize(None), freq="M"), fill_value=0)
    monthly.index.name = "month"
    monthly["mom_growth"] = monthly["dollar_volume"].div(monthly["dollar_volume"].shift(1).replace(0, float("nan"))) - 1
    monthly["rolling_6mo_avg"] = monthly["dollar_volume"].rolling(6, min_periods=3).mean().shift(1)
    monthly["vs_6mo_avg"] = monthly["dollar_volume"].div(monthly["rolling_6mo_avg"].replace(0, float("nan")))
    growth = monthly["mom_growth"]
    std = growth.std()
    monthly["growth_z"] = (growth - growth.mean()) / (std if pd.notna(std) and std > 0 else float("nan"))
    show(save("monthly", monthly))
    print("Zero rows mean no observed volume in this dataset; they do not prove API coverage.")
    spikes = monthly[monthly["growth_z"].gt(2)].sort_values("growth_z", ascending=False)
    print("\nSpike months (growth z > 2):")
    show(save("spikes", spikes))
    print("\nTop 10 months:")
    show(monthly.nlargest(10, "dollar_volume"))
    drivers = finished.groupby(["month", "category", "event_title"])["volume_usd"].sum().to_frame("dollar_volume")
    save("monthly_drivers", drivers)
    for month in spikes.index[:5]:
        print(f"\nDrivers of {month}:")
        show(drivers.loc[month].nlargest(5, "dollar_volume"))

    header("3. CATEGORY MIX OVER TIME")
    cat_year = finished.pivot_table(index="category", columns="year", values="volume_usd", aggfunc="sum", fill_value=0)
    cat_year = cat_year.reindex(columns=yearly.index, fill_value=0)
    cat_year = cat_year.loc[cat_year.sum(axis=1).sort_values(ascending=False).index]
    shares = cat_year.div(cat_year.sum().replace(0, float("nan")))
    print("Share by category:")
    show(save("category_shares", shares))
    print("\nDollar volume by category:")
    show(save("category_yearly", cat_year))
    if not finished.empty:
        first, last = int(finished["year"].min()), int(finished["year"].max())
        print(f"\nCategory growth multiples, {first} to {last} (may include partial years):")
        show((cat_year[last] / cat_year[first].replace(0, float("nan"))).dropna().sort_values(ascending=False).to_frame("multiple"))

    header("4. BIGGEST SINGLE MARKETS (ALL COLLECTED DATES)")
    show(save("top_markets", df.nlargest(25, "volume_usd")[["id", "category", "title", "closed_time", "inferred_result", "volume_usd", "state"]]))

    header("5. BIGGEST EVENTS (SUM OF UNIQUE MARKET VOLUME)")
    events = df.groupby("event_key").agg(title=("event_title", "first"), markets=("id", "count"),
                dollar_volume=("volume_usd", "sum"), close=("closed_time", "max"),
                states=("state", lambda x: ", ".join(sorted(set(x)))))
    show(save("events", events.sort_values("dollar_volume", ascending=False)).head(25))

    header("6. SPORTS EVENTS AND SERIES")
    sports = df[df["category"].str.casefold().eq("sports")]
    sport_events = sports.groupby("event_key").agg(title=("event_title", "first"), markets=("id", "count"), dollar_volume=("volume_usd", "sum"))
    show(save("sports_events", sport_events.sort_values("dollar_volume", ascending=False)).head(25))
    sports_series = sports[sports["series_id"].notna()].groupby(["series_id", "series_title"]).agg(events=("event_key", "nunique"), dollar_volume=("volume_usd", "sum"))
    print("\nSports recurring series:")
    show(save("sports_series", sports_series.sort_values("dollar_volume", ascending=False)).head(20))
    sports_month = finished[finished["category"].str.casefold().eq("sports")].groupby("month")["volume_usd"].sum().reindex(monthly.index, fill_value=0)
    print("\nSports share of observed monthly volume:")
    show(save("sports_monthly_share", (sports_month / monthly["dollar_volume"].replace(0, float("nan"))).to_frame("share")))

    header("7. BIGGEST RECURRING SERIES")
    with_series = df[df["series_id"].notna() & df["series_id"].ne("")]
    print(f"Markets without a recurring series: {len(df) - len(with_series):,}; these are excluded here.")
    series = with_series.groupby(["series_id", "series_title", "category"]).agg(events=("event_key", "nunique"), markets=("id", "count"), dollar_volume=("volume_usd", "sum"))
    show(save("series", series.sort_values("dollar_volume", ascending=False)).head(25))

    header("8. BUSIEST CLOSE DAYS (LIFETIME VOLUME ASSIGNED TO CLOSE DAY)")
    daily = finished.groupby("day").agg(markets=("id", "count"), dollar_volume=("volume_usd", "sum"))
    show(save("daily", daily.sort_values("dollar_volume", ascending=False)).head(15))
    for day in daily.nlargest(15, "dollar_volume").index:
        biggest = finished[finished["day"].eq(day)].nlargest(1, "volume_usd").iloc[0]
        print(f"{day}: biggest market: {biggest['title']}")

    header("9. RESOLUTION STATUS AND OUTCOMES")
    if "confirmed_result" in finished:
        confirmed = finished[finished["confirmed_result"].fillna("").str.casefold().isin(["yes", "no"])].copy()
        confirmed["yes"] = confirmed["confirmed_result"].str.casefold().eq("yes")
        print("Data API v2 resolved payouts (exact 1/0 binary settlement):")
        show(save("confirmed_resolution", confirmed.groupby("category").agg(markets=("id", "count"), yes_rate=("yes", "mean"))))
        print(f"Markets with a confirmed binary payout: {len(confirmed):,} of {len(finished):,}")
        print("Missing results can be unqueried, unavailable, unresolved, split, or nonbinary. See --with-resolutions.")
    print("\nSeparate Gamma price-based inference:")
    print("A closed flag alone is not proof of settlement. Results below require a resolved API status")
    print("and an exact 1/0 outcome price vector. They are price-inferred, not verified on-chain payouts.")
    print("Split, missing, disputed, and nonbinary results are not forced into YES/NO.")
    status = finished.groupby(["resolution_status", "uma_resolution_status"], dropna=False).agg(markets=("id", "count"), dollar_volume=("volume_usd", "sum"))
    show(save("resolution_status", status))
    binary = finished[finished["inferred_result"].fillna("").str.casefold().isin(["yes", "no"])].copy()
    binary["yes"] = binary["inferred_result"].str.casefold().eq("yes")
    results = binary.groupby("category").agg(markets=("id", "count"), inferred_yes_rate=("yes", "mean"))
    show(save("inferred_resolution", results.sort_values("markets", ascending=False)))
    print(f"Included in inferred YES/NO analysis: {len(binary):,} of {len(finished):,} closed-window markets")
    show(binary.groupby("inferred_result")["volume_usd"].sum().to_frame("dollar_volume"))

    header("10. TIME FROM REPORTED START TO ACTUAL CLOSE")
    life = finished[finished["days_open"].ge(0)].copy()
    show(save("lifetimes", life.groupby("category")["days_open"].agg(["count", "median", "mean", "max"]).sort_values("count", ascending=False)))
    print(f"Missing or negative durations excluded: {len(finished) - len(life):,}")
    life["life_bucket"] = pd.cut(life["days_open"], bins=[0, 1, 7, 30, 180, float("inf")], right=False,
                                labels=["under 1 day", "1-7 days", "1-4 weeks", "1-6 months", "6+ months"])
    show(save("lifetime_buckets", life.groupby("life_bucket", observed=False).agg(markets=("id", "count"), dollar_volume=("volume_usd", "sum"))))

    header("11. VOLUME CONCENTRATION")
    volume = finished["volume_usd"].sort_values(ascending=False)
    concentration = []
    for fraction in (0.001, 0.01, 0.1):
        count = min(len(volume), max(1, int(len(volume) * fraction)))
        share = ratio(volume.head(count).sum(), volume.sum())
        concentration.append({"top_fraction": fraction, "markets": count, "volume_share": share})
        print(f"Top {fraction:.1%} ({count:,} markets): {share:.1%} of observed volume")
    save("concentration", pd.DataFrame(concentration))
    print(f"Median: ${volume.median():,.2f}; mean: ${volume.mean():,.2f}")
    for year, frame in finished.groupby("year"):
        values = frame["volume_usd"].sort_values(ascending=False)
        print(f"{year}: top 1% share: {ratio(values.head(max(1, int(len(values) * .01))).sum(), values.sum()):.1%}")

    header("12. WITHIN-CATEGORY VOLUME OUTLIERS (Z > 2)")
    finished["cat_mean"] = finished.groupby("category")["volume_usd"].transform("mean")
    finished["cat_std"] = finished.groupby("category")["volume_usd"].transform("std")
    finished["vol_z"] = (finished["volume_usd"] - finished["cat_mean"]) / finished["cat_std"].replace(0, float("nan"))
    outliers = finished[finished["vol_z"].gt(2)].sort_values("vol_z", ascending=False)
    print(f"Markets flagged: {len(outliers):,}")
    show(save("outliers", outliers[["id", "category", "title", "volume_usd", "vol_z"]]))
    print("\nMost extreme per category:")
    show(outliers.groupby("category").head(1)[["title", "volume_usd", "vol_z"]])
    print("Z scores flag descriptive outliers; sparse groups and heavy tails limit interpretation.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=FOLDER / "data")
    parser.add_argument("--output-dir", type=Path, default=FOLDER / "reports")
    parser.add_argument("--years", type=int, default=5)
    args = parser.parse_args()
    if args.years < 1:
        parser.error("--years must be positive")
    path = args.data_dir / "polymarket_markets.csv"
    if not path.exists():
        parser.error("No polymarket_markets.csv found. Run collect_polymarket.py first.")
    df = load(path)
    manifest_path = args.data_dir / "polymarket_manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.max_colwidth", 80)
    report = args.output_dir / "polymarket_analysis.txt"
    with report.open("w", encoding="utf-8") as stream, redirect_stdout(stream):
        print("Polymarket analysis; exported at:", manifest.get("exported_at", "unknown"))
        if not manifest.get("complete"):
            print("WARNING: collection incomplete or coverage unknown. This report is a partial sample.")
        analyze(df, args.years, pd.Timestamp.now(tz="UTC"), args.output_dir)
    print(report.read_text(encoding="utf-8"))
    print(f"Report and CSV tables saved to {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
