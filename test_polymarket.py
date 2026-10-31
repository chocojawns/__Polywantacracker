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
import export_category_top10 as top10


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

    def test_data_api_identical_duplicate_is_saved_once_and_resume_skips_it(self):
        condition = "0x" + "1" * 64
        resolution = {"condition_id": condition, "status": "resolved", "payouts": [0, 1_000_000]}
        with self.db:
            self.db.execute("INSERT INTO records VALUES (?,?,?,?)",
                            ("markets", "1", json.dumps(market(conditionId=condition)), "now"))
        # Independently decoded objects, with a different key order, still
        # represent the same JSON row returned twice by the live API.
        duplicate = dict(reversed(list(resolution.items())))
        with patch.object(c, "get", return_value={"data": [resolution, duplicate]}), patch.object(c.time, "sleep"):
            self.assertTrue(c.enrich_resolutions(self.db, None))
        saved = self.db.execute("SELECT payload FROM records WHERE kind='resolutions'").fetchall()
        self.assertEqual(len(saved), 1)
        self.assertEqual(json.loads(saved[0][0]), resolution)
        c.export(self.db, self.folder)
        df = a.load(self.folder / "polymarket_markets.csv")
        self.assertEqual(df["confirmed_result"].iloc[0], "No")
        with patch.object(c, "get") as get:
            self.assertTrue(c.enrich_resolutions(self.db, None))
            get.assert_not_called()

    def test_data_api_conflicting_or_unrequested_rows_do_not_commit_batch(self):
        condition = "0x" + "1" * 64
        resolution = {"condition_id": condition, "status": "resolved", "payouts": [0, 1_000_000]}
        with self.db:
            self.db.execute("INSERT INTO records VALUES (?,?,?,?)",
                            ("markets", "1", json.dumps(market(conditionId=condition)), "now"))
        for bad, message in [
            ({**resolution, "payouts": [1_000_000, 0]}, "Conflicting resolution rows"),
            ({**resolution, "condition_id": "0x" + "2" * 64}, "Unexpected condition"),
        ]:
            with self.subTest(message=message):
                with patch.object(c, "get", return_value={"data": [resolution, bad]}):
                    with self.assertRaisesRegex(ValueError, message):
                        c.enrich_resolutions(self.db, None)
                self.assertEqual(self.db.execute("SELECT count(*) FROM records WHERE kind='resolutions'").fetchone()[0], 0)

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
    def test_currency_percent_and_duration_formatting_preserves_numeric_values(self):
        frame = pd.DataFrame({"dollar_volume": [1000.125], "share": [.25], "mean": [.5]})
        before = frame.copy()
        output = io.StringIO()
        with redirect_stdout(output):
            a.show(frame)
        text = output.getvalue()
        self.assertIn("$1,000.12", text)
        self.assertIn("25.00%", text)
        self.assertIn("mean_days", text)
        pd.testing.assert_frame_equal(frame, before)
        output = io.StringIO()
        with redirect_stdout(output):
            a.show(pd.DataFrame({"2020": [0, .000001, float('nan')]}), percent=True, zero_dash=True)
        self.assertIn("<0.01%", output.getvalue())
        self.assertIn("N/A", output.getvalue())

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
        self.assertEqual(tables["busiest_days"]["biggest_market_question"].iloc[0], "Question, with\na newline")

    def test_duplicates_keep_latest_and_yes_no_case(self):
        tables = self.run_analysis([market(), market(volumeNum=300, outcomes='["YES","NO"]')])
        self.assertEqual(tables["yearly"]["markets"].sum(), 1)
        self.assertEqual(tables["yearly"]["dollar_volume"].sum(), 300)
        self.assertEqual(tables["inferred_resolution"]["inferred_yes_rate"].iloc[0], 1)


class CategoryExportTests(unittest.TestCase):
    def test_top10_reads_full_source_preserves_titles_and_handles_small_categories(self):
        import csv
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            source = folder / "markets.csv"
            with source.open("w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["category", "title", "volume_usd"])
                for i in range(15):
                    writer.writerow(["Sports", f"Question {i}", str(i + 1)])
                writer.writerow(["Science", 'Question, with "quotes"\nand a newline', "1234.567"])
                writer.writerow(["Science", "No trading", "0"])
            with redirect_stdout(io.StringIO()):
                groups = top10.export(source, folder / "reports" / "top10_by_category")
            self.assertEqual(len(groups["Sports"]), 10)
            self.assertEqual(min(row[0] for row in groups["Sports"]), 6)
            self.assertEqual(len(groups["Science"]), 1)
            with (folder / "reports/top10_by_category/sports.csv").open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0], {"title": "Question 14", "volume_usd": "15"})
            report = (folder / "reports/top10_by_category.txt").read_text()
            self.assertIn("$1,234.57", report)
            self.assertIn('Question, with "quotes"\nand a newline', report)


if __name__ == "__main__":
    unittest.main()
