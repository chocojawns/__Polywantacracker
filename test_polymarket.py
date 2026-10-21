"""Offline regression tests: python -m unittest -v."""
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import pandas as pd
import requests

import collect_polymarket as c
import analyze_polymarket as a


def market(identifier="1", **extra):
    return {"id": identifier, "question": "Question, with\na newline", "volumeNum": 100,
            "closed": True, "active": False, "acceptingOrders": False,
            "startDate": "2024-01-01T00:00:00Z", "closedTime": "2024-02-01T00:00:00Z",
            "endDate": "2024-02-02T00:00:00Z", "outcomes": '["Yes","No"]',
            "outcomePrices": '["1","0"]', "umaResolutionStatus": "resolved", **extra}


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.db = c.connect(self.folder)
        self.addCleanup(self.db.close)

    @patch.object(c, "PHASES", [("markets", True)])
    def test_resume_deduplicates_and_exports_multiline_csv(self):
        with patch.object(c, "get", return_value={"markets": [market()], "next_cursor": "next"}):
            self.assertFalse(c.collect(self.db, None, max_pages=1, pause=0))
        with patch.object(c, "get", return_value={"markets": [market(volumeNum=150), market("2")], "next_cursor": None}) as get:
            self.assertTrue(c.collect(self.db, None, pause=0))
            self.assertEqual(get.call_args.args[2]["after_cursor"], "next")
        c.export(self.db, self.folder)
        df = a.load(self.folder / "polymarket_markets.csv")
        self.assertEqual(len(df), 2)
        self.assertEqual(df.loc[df["id"].eq("1"), "volume_usd"].iloc[0], 150)
        self.assertIn("\n", df["title"].iloc[0])
        self.assertTrue(json.loads((self.folder / "polymarket_manifest.json").read_text())["complete"])
        with patch.object(c, "get") as get:
            c.collect(self.db, None)
            get.assert_not_called()

    @patch.object(c, "PHASES", [("markets", True)])
    def test_invalid_page_does_not_advance_state(self):
        with patch.object(c, "get", return_value={"markets": [market(), {}], "next_cursor": "next"}):
            with self.assertRaises(ValueError):
                c.collect(self.db, None)
        self.assertEqual(self.db.execute("SELECT count(*) FROM records").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM progress").fetchone()[0], 0)

    @patch.object(c, "PHASES", [("markets", True)])
    def test_failed_sql_write_rolls_back_data_and_cursor(self):
        self.db.execute("CREATE TRIGGER fail_progress BEFORE INSERT ON progress BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
        with patch.object(c, "get", return_value={"markets": [market()], "next_cursor": None}):
            with self.assertRaises(sqlite3.IntegrityError):
                c.collect(self.db, None)
        self.assertEqual(self.db.execute("SELECT count(*) FROM records").fetchone()[0], 0)

    @patch.object(c, "PHASES", [("markets", True)])
    def test_repeated_cursor_fails_without_saving_bad_page(self):
        with patch.object(c, "get", side_effect=[{"markets": [market()], "next_cursor": "same"},
                                                {"markets": [market("2")], "next_cursor": "same"}]):
            with self.assertRaises(ValueError):
                c.collect(self.db, None, pause=0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM records").fetchone()[0], 1)

    def test_resolution_and_category_semantics(self):
        event = {"id": "10", "tags": [{"label": "Sports"}], "series": [{"id": "20", "title": "League"}]}
        row = c.normalize(market(), event, "now")
        self.assertEqual(row["inferred_result"], "Yes")
        self.assertEqual(row["category"], "Sports")
        self.assertEqual(row["series_id"], "20")
        self.assertEqual(c.normalize(market(umaResolutionStatus="disputed"), event, "now")["inferred_result"], "")
        self.assertEqual(c.normalize(market(outcomePrices='["0.5","0.5"]'), event, "now")["inferred_result"], "")
        self.assertEqual(c.normalize(market(volumeNum=0, volume="123"), {}, "now")["volume_usd"], 0)

    @patch.object(c.time, "sleep")
    def test_retries_rate_limit_and_connection_errors(self, sleep):
        session = Mock()
        limited = Mock(status_code=429, headers={"Retry-After": "3"})
        good = Mock(status_code=200)
        good.json.return_value = {"markets": []}
        session.get.side_effect = [requests.ConnectionError("offline"), limited, good]
        self.assertEqual(c.get(session, "/markets/keyset", {}), {"markets": []})
        self.assertEqual(sleep.call_count, 2)

    def test_permanent_http_errors_fail_immediately(self):
        session = Mock()
        response = Mock(status_code=403)
        response.raise_for_status.side_effect = requests.HTTPError("forbidden")
        session.get.return_value = response
        with self.assertRaises(requests.HTTPError):
            c.get(session, "/markets/keyset", {})
        self.assertEqual(session.get.call_count, 1)

    def test_data_api_enrichment_resumes_and_caches_misses(self):
        records = [("markets", str(i), json.dumps(market(str(i), conditionId="0x" + format(i, "064x"))), "now") for i in range(21)]
        with self.db:
            self.db.executemany("INSERT INTO records VALUES (?,?,?,?)", records)
        condition = "0x" + "0" * 64
        resolution = {"condition_id": condition, "status": "resolved", "payouts": [0, 1_000_000]}
        with patch.object(c, "get", return_value={"data": [resolution]}) as get, patch.object(c.time, "sleep"):
            self.assertFalse(c.enrich_resolutions(self.db, None, max_batches=1))
            self.assertEqual(get.call_args.kwargs["base"], c.DATA_BASE)
        with patch.object(c, "get", return_value={"data": []}) as get, patch.object(c.time, "sleep"):
            self.assertTrue(c.enrich_resolutions(self.db, None))
            self.assertEqual(get.call_count, 1)
        c.export(self.db, self.folder)
        df = a.load(self.folder / "polymarket_markets.csv")
        self.assertEqual(df.loc[df["id"].eq("0"), "confirmed_result"].iloc[0], "No")
        with patch.object(c, "get") as get:
            self.assertTrue(c.enrich_resolutions(self.db, None))
            get.assert_not_called()
        split = c.normalize(market(), {}, "now", {"status": "resolved", "payouts": [500_000, 500_000]})
        self.assertEqual(split["confirmed_result"], "")


class AnalysisTests(unittest.TestCase):
    def run_analysis(self, records):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            c.atomic_csv(folder / "polymarket_markets.csv", c.FIELDS,
                         [c.normalize(m, {}, "now") for m in records])
            df = a.load(folder / "polymarket_markets.csv")
            output = io.StringIO()
            with redirect_stdout(output):
                a.analyze(df, 5, pd.Timestamp("2025-06-01", tz="UTC"), folder)
            self.assertIn("12. WITHIN-CATEGORY", output.getvalue())
            return {name.stem: pd.read_csv(name) for name in folder.glob("*.csv")}

    def test_empty_and_no_closed_window_data(self):
        self.run_analysis([])
        self.run_analysis([market(closed=False, active=True, acceptingOrders=True)])

    def test_actual_closed_flag_not_end_date_and_missing_months(self):
        tables = self.run_analysis([market(), market("2", closed=False),
                                   market("3", closedTime=None),
                                   market("4", closedTime="2024-04-01T00:00:00Z", volumeNum=200)])
        self.assertEqual(tables["yearly"]["markets"].sum(), 2)
        march = tables["monthly"].loc[tables["monthly"]["month"].eq("2024-03")]
        self.assertEqual(march["dollar_volume"].iloc[0], 0)
        self.assertEqual(tables["events"]["markets"].sum(), 4)
        self.assertEqual(len(tables["events"]), 4)

    def test_duplicates_keep_latest_and_yes_no_case(self):
        tables = self.run_analysis([market(), market(volumeNum=300, outcomes='["YES","NO"]')])
        self.assertEqual(tables["yearly"]["markets"].sum(), 1)
        self.assertEqual(tables["yearly"]["dollar_volume"].sum(), 300)
        self.assertEqual(tables["inferred_resolution"]["inferred_yes_rate"].iloc[0], 1)


if __name__ == "__main__":
    unittest.main()
