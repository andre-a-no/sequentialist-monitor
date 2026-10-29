# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
import datetime as dt
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sequentialist.monitor.sources import (
    BAD,
    GOOD,
    UNCERTAIN,
    CsvSource,
    SourceError,
    SqlSource,
    quality,
    timestamp,
)

T = 1793491200.0
ROUTES = {"TI-1": ["A"], "TI-2": ["B"], "TI-3": ["A", "B"]}
CSV = {"pattern": "*.csv", "delimiter": ",", "quality_codes": "text",
       "columns": {"tag": "tag", "time": "time", "value": "value", "quality": "q"}}
SQL = {"module": "sqlite3", "dsn_env": "X", "query": "SELECT tag, ts, value, q FROM s WHERE ts >= ? AND ts < ?",
       "paramstyle": "qmark", "time_param": "epoch", "quality_codes": "opc-da", "period": 5}


def cursors_of(batches, before=None):
    """The cursors the units would hold after taking `batches` (as their handlers record them)."""
    out = {uid: dict(c) for uid, c in (before or {}).items()}
    for uid, records in batches.items():
        for r in records:
            if "cursor" in r.data:
                out.setdefault(uid, {})[r.source] = {"position": r.data["cursor"], "next": r.seq + 1}
    return out


class QualityTest(unittest.TestCase):
    def test_codes(self):
        self.assertEqual([quality(c, "opc-da") for c in (192, 216, 64, 88, 0, 24, 128)],
                         [GOOD, GOOD, UNCERTAIN, UNCERTAIN, BAD, BAD, BAD])
        self.assertEqual([quality(c, "opc-ua") for c in (0, 0x40000000, 0x80000000, 0x80340000)],
                         [GOOD, UNCERTAIN, BAD, BAD])
        self.assertEqual([quality(c, "text") for c in ("good", "Uncertain", "BAD", "?")], [GOOD, UNCERTAIN, BAD, BAD])
        self.assertEqual([quality(None, "opc-da"), quality("x", "none")], [GOOD, GOOD])

    def test_times(self):
        self.assertEqual(timestamp("2026-11-01T00:00:00Z"), T)
        self.assertEqual(timestamp("2026-11-01T03:00:00+03:00"), T)
        self.assertEqual(timestamp(str(T)), T)
        self.assertEqual(timestamp(dt.datetime(2026, 11, 1)), T)  # naive is UTC
        self.assertIsNone(timestamp("2026-11-01 00:00"))  # no time zone: refused, not guessed


class CsvTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "a.csv"
        self.source = CsvSource("files", dict(CSV, directory=self.dir.name), ROUTES)

    def tearDown(self):
        self.dir.cleanup()

    def write(self, text, mode="a"):
        with open(self.path, mode, encoding="utf-8", newline="") as f:
            f.write(text)

    def test_each_unit_numbers_its_readings_and_ends_with_a_cursor(self):
        self.write("tag,time,value,q\nTI-1,2026-11-01T00:00:00Z,1.5,good\nTI-2,2026-11-01T00:00:00Z,2,bad\n"
                   "TI-3,1793491201,3,good\nXX,1793491201,9,good\nTI-1,1793491202,", mode="w")
        first = self.source.poll({})
        a, b = first["A"], first["B"]
        self.assertEqual([(r.seq, r.data) for r in a], [
            (0, {"tag": "TI-1", "value": 1.5, "quality": GOOD}),
            (1, {"tag": "TI-3", "value": 3.0, "quality": GOOD}),
            (2, {"cursor": a[-1].data["cursor"]})])
        self.assertEqual([r.data.get("tag") for r in b], ["TI-2", "TI-3", None])
        self.assertEqual(b[1].seq, 1)
        self.assertEqual(a[0].source, "files:a.csv")
        # the incomplete last line is not read; once complete, only it is
        self.write("5,good\n")
        batches = self.source.poll(cursors_of(first))
        self.assertEqual([(r.seq, r.data.get("tag"), r.time) for r in batches["A"]],
                         [(3, "TI-1", T + 2), (4, None, None)])
        self.assertEqual([r.data.get("tag") for r in batches["B"]], [None])  # B only moves its cursor

    def test_a_restart_reads_again_from_the_earliest_cursor(self):
        self.write("tag,time,value,q\nTI-1,1793491200,1,good\nTI-3,1793491200,3,good\n", mode="w")
        first = self.source.poll({})
        self.write("TI-1,1793491201,1,good\nTI-3,1793491201,3,good\n")
        # A took the first batch, B crashed before it did: B reads it all, A only what is new
        cursors = {"A": cursors_of(first)["A"]}
        again = self.source.poll(cursors)
        self.assertEqual([(r.seq, r.time) for r in again["A"] if "tag" in r.data], [(3, T + 1), (4, T + 1)])
        self.assertEqual([(r.seq, r.time) for r in again["B"] if "tag" in r.data], [(0, T), (1, T + 1)])
        self.assertEqual(first["B"][0], again["B"][0])  # the same record under the same number

    def test_history(self):
        self.write("tag,time,value,q\nTI-1,1793491200,1,good\nTI-3,1793491200,3,good\nTI-1,1793491300,1,good\n",
                   mode="w")
        history = self.source.history(T, T + 100)
        self.assertEqual([(r.seq, r.data["tag"]) for r in history], [(0, "TI-1"), (1, "TI-3")])

    def test_a_missing_column(self):
        self.write("tag,when,value\nTI-1,1793491200,1\n", mode="w")
        with self.assertRaises(SourceError):
            self.source.poll({})


class SqlTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE s (tag TEXT, ts REAL, value REAL, q INTEGER)")
        self.db.executemany("INSERT INTO s VALUES (?, ?, ?, ?)", [
            ("TI-1", T, 1.0, 192), ("TI-2", T, 2.0, 64), ("TI-3", T + 1, 3.0, 0),
            ("TI-1", T + 6, 1.1, 192), ("TI-3", T + 7, 3.3, 192)])
        self.source = SqlSource("hist", SQL, ROUTES, connect=lambda: self.db)

    def tearDown(self):
        self.db.close()

    def test_closed_slices(self):
        first = self.source.poll({}, until=T + 5, first=T)
        self.assertEqual([(r.seq, r.data) for r in first["A"]], [
            (0, {"tag": "TI-1", "value": 1.0, "quality": GOOD}), (1, {"tag": "TI-3", "value": 3.0, "quality": BAD}),
            (2, {"cursor": T + 5})])
        self.assertEqual(first["B"][0].data["quality"], UNCERTAIN)
        cursors = cursors_of(first)
        second = self.source.poll(cursors, until=T + 10, first=T)
        self.assertEqual([(r.seq, r.time) for r in second["A"]], [(3, T + 6), (4, T + 7), (5, None)])
        self.assertEqual([(r.seq, r.time) for r in second["B"]], [(3, T + 7), (4, None)])
        # a unit that is behind (it crashed) reads again from its own cursor, the other skips
        behind = {"A": cursors["A"]}
        again = self.source.poll(behind, until=T + 10, first=T)
        self.assertEqual(again["B"][:2], first["B"][:2])
        self.assertEqual(again["A"], second["A"])
        self.assertEqual(self.source.poll(cursors_of(second, cursors), until=T + 10, first=T),
                         {"A": [], "B": []})

    def test_driver_errors_are_source_errors(self):
        broken = SqlSource("hist", dict(SQL, query="SELECT nothing FROM nowhere"), ROUTES, connect=lambda: self.db)
        with self.assertRaises(SourceError):
            broken.poll({}, until=T + 5, first=T)

    def test_history(self):
        self.assertEqual([r.data["tag"] for r in self.source.history(T, T + 10)], ["TI-1", "TI-2", "TI-3", "TI-1",
                                                                                   "TI-3"])


if __name__ == "__main__":
    unittest.main()
