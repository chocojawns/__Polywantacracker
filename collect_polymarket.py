"""Resumable public Polymarket market census; run with --help for options."""
import argparse
import csv
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests

BASE = "https://gamma-api.polymarket.com"
DATA_BASE = "https://data-api.polymarket.com/v2"
FOLDER = Path(__file__).resolve().parent
# Separate open/closed scans because the events endpoint defaults to open only.
PHASES = [(kind, closed) for kind in ("events", "markets") for closed in (True, False)]
FIELDS = ["id", "condition_id", "title", "slug", "event_id", "event_title",
          "category", "category_source", "tags", "series_id", "series_title",
          "start_time", "end_time", "closed_time", "closed", "active",
          "archived", "accepting_orders", "volume_usd", "liquidity_usd",
          "resolution_status", "uma_resolution_status", "inferred_result",
          "outcomes", "outcome_prices", "collected_at", "data_resolution_status",
          "confirmed_result", "payouts", "resolved_at"]


def array(value):
    if isinstance(value, str):
        value = json.loads(value or "[]")
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Expected an array or JSON-encoded array")
    return value


def number(value):
    if value in (None, ""):
        return None
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"Invalid nonnegative numeric metric: {value!r}")
    return result


def get(session, path, params, base=BASE):
    last = None
    for attempt in range(8):
        try:
            response = session.get(base + path, params=params, timeout=(15, 60))
            if response.status_code == 429 or 500 <= response.status_code < 600:
                last = RuntimeError(f"HTTP {response.status_code} from {path}")
                delay = min(60, 2 ** (attempt + 1))
                try:
                    delay = min(300, max(delay, float(response.headers.get("Retry-After", 0))))
                except ValueError:
                    pass
            else:
                response.raise_for_status()
                return response.json()
        except (requests.ConnectionError, requests.Timeout) as error:
            last = error
            delay = min(60, 2 ** (attempt + 1))
        if attempt < 7:
            print(f"Retry {attempt + 1}/7 for {path} in {delay}s", flush=True)
            time.sleep(delay)
    raise RuntimeError(f"Request exhausted retries: {path}") from last


def connect(folder):
    folder.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(folder / "polymarket.sqlite3")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS records (
            kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
            collected_at TEXT NOT NULL, PRIMARY KEY (kind, id));
        CREATE TABLE IF NOT EXISTS progress (
            phase TEXT PRIMARY KEY, cursor TEXT, pages INTEGER NOT NULL,
            complete INTEGER NOT NULL);
    """)
    return db


def collect(db, session, page_size=100, max_pages=None, pause=0.2):
    fetched = 0
    for kind, closed in PHASES:
        phase = f"{kind}_{'closed' if closed else 'open'}"
        state = db.execute("SELECT cursor,pages,complete FROM progress WHERE phase=?", (phase,)).fetchone()
        cursor, pages, complete = state or (None, 0, 0)
        if complete:
            continue
        print(f"Collecting {phase}, resuming after {pages} pages", flush=True)
        seen = {cursor} if cursor else set()
        while True:
            if max_pages is not None and fetched >= max_pages:
                return False
            params = {"limit": page_size, "closed": str(closed).lower(),
                      "order": "id", "ascending": "true"}
            if kind == "markets":
                params["include_tag"] = "true"
            if cursor:
                params["after_cursor"] = cursor
            payload = get(session, f"/{kind}/keyset", params)
            if not isinstance(payload, dict) or not isinstance(payload.get(kind), list):
                raise ValueError(f"Unexpected {kind} response; progress was not advanced")
            items = payload[kind]
            next_cursor = payload.get("next_cursor")
            if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor):
                raise ValueError("Invalid next_cursor; progress was not advanced")
            if next_cursor and (next_cursor in seen or not items):
                raise ValueError("Pagination did not advance; progress was not advanced")
            rows = []
            collected_at = datetime.now(timezone.utc).isoformat()
            for item in items:
                if not isinstance(item, dict) or item.get("id") in (None, ""):
                    raise ValueError("Record is missing id; progress was not advanced")
                rows.append((kind, str(item["id"]), json.dumps(item), collected_at))
            # Data and cursor commit together. A crash can replay a request, never
            # append duplicate CSV rows or advance past data that was not saved.
            with db:
                db.executemany("INSERT OR REPLACE INTO records VALUES (?,?,?,?)", rows)
                db.execute("INSERT OR REPLACE INTO progress VALUES (?,?,?,?)",
                           (phase, next_cursor, pages + 1, int(next_cursor is None)))
            pages += 1
            fetched += 1
            print(f"  {phase}: page {pages}, {len(items)} records committed", flush=True)
            if next_cursor is None:
                break
            seen.add(next_cursor)
            cursor = next_cursor
            time.sleep(pause)
    return True


def enrich_resolutions(db, session, max_batches=None):
    """Fetch API resolution rows in batches of 20 conditions; cache misses too."""
    # Use a temporary SQLite work table rather than retaining the entire census
    # in Python memory. IDs are parameters, never SQL text.
    db.execute("CREATE TEMP TABLE IF NOT EXISTS pending_conditions (id TEXT PRIMARY KEY)")
    db.execute("DELETE FROM pending_conditions")
    for (payload,) in db.execute("SELECT payload FROM records WHERE kind='markets'"):
        condition = json.loads(payload).get("conditionId")
        if condition:
            db.execute("INSERT OR IGNORE INTO pending_conditions VALUES (?)", (condition.lower(),))
    db.commit()
    batches = 0
    while True:
        conditions = [row[0] for row in db.execute("""
            SELECT id FROM pending_conditions WHERE id NOT IN
            (SELECT id FROM records WHERE kind='resolutions') ORDER BY id LIMIT 20
        """)]
        if not conditions:
            return True
        if max_batches is not None and batches >= max_batches:
            return False
        payload = get(session, "/resolutions", {"condition": ",".join(conditions)}, base=DATA_BASE)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("Unexpected Data API resolution response")
        lookup = {}
        for row in payload["data"]:
            if not isinstance(row, dict) or not isinstance(row.get("condition_id"), str) or not isinstance(row.get("status"), str):
                raise ValueError("Resolution row is missing condition_id or status")
            identifier = row["condition_id"].lower()
            if identifier not in conditions or identifier in lookup:
                raise ValueError("Unexpected or duplicate condition in resolution response")
            lookup[identifier] = row
        stamp = datetime.now(timezone.utc).isoformat()
        with db:
            db.executemany("INSERT OR REPLACE INTO records VALUES (?,?,?,?)",
                           [("resolutions", condition, json.dumps(lookup.get(condition, {})), stamp)
                            for condition in conditions])
        batches += 1
        print(f"  Data API resolutions: batch {batches}, {len(lookup)}/{len(conditions)} found", flush=True)
        time.sleep(0.2)


def category(market, event):
    for record in (market, event):
        if record.get("category"):
            return record["category"], "api_category"
    tags = array(market.get("tags")) + array(event.get("tags"))
    labels = {str(t.get("label", "")).lower() for t in tags}
    # One exclusive category per market; preserve every tag separately.
    for name in ("Sports", "Politics", "Crypto", "Economics", "Finance",
                 "Science", "Technology", "Culture", "Weather"):
        if name.lower() in labels:
            return name, "broad_tag_priority"
    return "Unknown", "missing"


def normalize(market, event, collected_at, data_resolution=None):
    cat, cat_source = category(market, event)
    series = array(event.get("series"))
    first_series = series[0] if series else {}
    outcomes = array(market.get("outcomes"))
    prices = array(market.get("outcomePrices"))
    resolution = market.get("resolutionStatus", "")
    uma = market.get("umaResolutionStatus", "")
    inferred = ""
    if resolution == "resolved" or uma in ("resolved", "settled"):
        if len(outcomes) == len(prices) and len(outcomes) >= 2:
            numeric = [number(p) for p in prices]
            if numeric.count(1.0) == 1 and all(p in (0.0, 1.0) for p in numeric):
                inferred = outcomes[numeric.index(1.0)]
    volume = market.get("volumeNum")
    if volume in (None, ""):
        volume = market.get("volume")
    row = dict(zip(FIELDS, [None] * len(FIELDS)))
    row.update(id=str(market["id"]), condition_id=market.get("conditionId"),
               title=market.get("question") or market.get("title"), slug=market.get("slug"),
               event_id=str(event.get("id", "")), event_title=event.get("title"),
               category=cat, category_source=cat_source,
               tags=json.dumps(array(market.get("tags")) + array(event.get("tags"))),
               series_id=str(first_series.get("id", "")), series_title=first_series.get("title"),
               start_time=market.get("startDate"), end_time=market.get("endDate"),
               closed_time=market.get("closedTime"), closed=market.get("closed"),
               active=market.get("active"), archived=market.get("archived"),
               accepting_orders=market.get("acceptingOrders"), volume_usd=number(volume),
               liquidity_usd=number(market.get("liquidityNum", market.get("liquidity"))),
               resolution_status=resolution, uma_resolution_status=uma,
               inferred_result=inferred, outcomes=json.dumps(outcomes),
               outcome_prices=json.dumps(prices), collected_at=collected_at)
    resolution_data = data_resolution or {}
    payouts = resolution_data.get("payouts")
    confirmed = ""
    if resolution_data.get("status") == "resolved" and isinstance(payouts, list):
        if len(payouts) == len(outcomes) and len(outcomes) >= 2:
            if payouts.count(1_000_000) == 1 and all(p in (0, 1_000_000) for p in payouts):
                confirmed = outcomes[payouts.index(1_000_000)]
    row.update(data_resolution_status=resolution_data.get("status"), confirmed_result=confirmed,
               payouts=json.dumps(payouts), resolved_at=resolution_data.get("resolved_at"))
    return row


def atomic_csv(path, fields, rows):
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def export(db, folder):
    def markets():
        for payload, stamp in db.execute("SELECT payload,collected_at FROM records WHERE kind='markets' ORDER BY id"):
            market = json.loads(payload)
            embedded = array(market.get("events"))
            # Preserve additional relationships in raw JSON; primary event only
            # in this mutually exclusive market-level analysis to avoid duplication.
            event = embedded[0] if embedded else {}
            full = db.execute("SELECT payload FROM records WHERE kind='events' AND id=?",
                              (str(event.get("id", "")),)).fetchone()
            if full:
                event = json.loads(full[0])
            resolution = db.execute("SELECT payload FROM records WHERE kind='resolutions' AND id=?",
                                    (str(market.get("conditionId", "")).lower(),)).fetchone()
            row = normalize(market, event, stamp, json.loads(resolution[0]) if resolution else None)
            if row["volume_usd"] is not None and row["volume_usd"] > 0:
                yield row
    atomic_csv(folder / "polymarket_markets.csv", FIELDS, markets())
    def events():
        for (payload,) in db.execute("SELECT payload FROM records WHERE kind='events' ORDER BY id"):
            event = json.loads(payload)
            yield {"id": str(event["id"]), "title": event.get("title"),
                   "slug": event.get("slug"), "tags": json.dumps(event.get("tags", [])),
                   "series": json.dumps(event.get("series", []))}
    atomic_csv(folder / "polymarket_events.csv", ["id", "title", "slug", "tags", "series"], events())
    states = [dict(zip(("phase", "pages", "complete"), row)) for row in
              db.execute("SELECT phase,pages,complete FROM progress ORDER BY phase")]
    manifest = {"source": BASE, "exported_at": datetime.now(timezone.utc).isoformat(),
                "complete": len(states) == len(PHASES) and all(s["complete"] for s in states),
                "phases": states, "note": "Live API census, not an atomic historical snapshot."}
    manifest["data_api"] = DATA_BASE
    manifest["resolution_conditions_checked"] = db.execute("SELECT count(*) FROM records WHERE kind='resolutions'").fetchone()[0]
    temp = folder / "polymarket_manifest.json.tmp"
    temp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    temp.replace(folder / "polymarket_manifest.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=FOLDER / "data")
    parser.add_argument("--page-size", type=int, default=100, choices=range(1, 501), metavar="1..500")
    parser.add_argument("--max-pages", type=int, help="Pause after this many new pages; rerun to resume")
    parser.add_argument("--refresh", action="store_true", help="Rescan from the beginning, retaining and updating saved IDs")
    parser.add_argument("--export-only", action="store_true", help="Regenerate CSVs without network access")
    parser.add_argument("--with-resolutions", action="store_true", help="Enrich saved markets using Data API v2 payouts (additional requests)")
    parser.add_argument("--max-resolution-batches", type=int, help="Pause resolution enrichment after N batches of up to 20 conditions")
    args = parser.parse_args()
    if args.max_pages is not None and args.max_pages < 1:
        parser.error("--max-pages must be positive")
    if args.max_resolution_batches is not None and args.max_resolution_batches < 1:
        parser.error("--max-resolution-batches must be positive")
    if args.refresh and args.export_only:
        parser.error("--refresh and --export-only cannot be combined")
    with connect(args.data_dir) as db, requests.Session() as session:
        if args.refresh:
            with db:
                db.execute("DELETE FROM progress")
                db.execute("DELETE FROM records WHERE kind='resolutions'")
        try:
            complete = None if args.export_only else collect(db, session, args.page_size, args.max_pages)
            if args.with_resolutions and not args.export_only:
                enriched = enrich_resolutions(db, session, args.max_resolution_batches)
                print(f"Resolution lookups {'complete for saved markets' if enriched else 'paused; rerun to resume'}.")
        except (requests.RequestException, ValueError, RuntimeError, KeyboardInterrupt) as error:
            print(f"Stopped: {error}. Saved pages are safe; rerun to resume.", file=sys.stderr)
            export(db, args.data_dir)
            return 1
        export(db, args.data_dir)
        print(f"{'Complete' if complete else 'Exported (check manifest for completeness)'}. Files: {args.data_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
