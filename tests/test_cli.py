# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""The commands end to end, on a historian in an SQLite file read through DB-API."""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sequentialist.core.cli import main
from sequentialist.monitor.config import load_baseline, load_plant
from sequentialist.monitor.service import Service

from .synthetic import T0, WINDOW, plant, records


class CommandLineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        d = Path(cls.dir.name)
        db = sqlite3.connect(d / "historian.db")
        db.execute("CREATE TABLE s (tag TEXT, ts REAL, value REAL, q INTEGER)")
        db.executemany("INSERT INTO s VALUES (?, ?, ?, ?)",
                       [(r.data["tag"], r.time, r.data["value"], 192)
                        for r in records(3300, seed=5, faults={"TI-1": (3150, 5.0)})])
        db.commit()
        db.close()
        data = plant().to_json()
        data["storage"] = f"sqlite:{d / 'monitor.db'}"
        data["sources"]["hist"] = {"type": "sql", "module": "sqlite3", "dsn_env": "HIST_DSN",
                                   "query": "SELECT tag, ts, value, q FROM s WHERE ts >= ? AND ts < ?",
                                   "time_param": "epoch"}
        data["units"]["P-1"]["reference"] = {"from": T0, "to": T0 + 3000 * WINDOW}
        (d / "plant.json").write_text(json.dumps(data), encoding="utf-8")
        cls.paths = {name: str(d / name) for name in ("plant.json", "baseline.json", "replay.md", "report.json")}
        cls.env = mock.patch.dict(os.environ, {"HIST_DSN": str(d / "historian.db")})
        cls.env.start()

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.dir.cleanup()

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["monitor", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_1_calibrate(self):
        code, _out, err = self.cli("calibrate", self.paths["plant.json"], "-o", self.paths["baseline.json"],
                                  "--runs", "200")
        self.assertEqual(code, 0, err)
        baseline = load_baseline(self.paths["baseline.json"])
        self.assertEqual(baseline.data["units"]["P-1"]["windows"], 3000)
        self.assertIn("threshold of cusum for ARL 360 by simulation", err)

    def test_2_replay_reports_the_shift_with_its_guarantee(self):
        code, _out, err = self.cli("replay", self.paths["plant.json"], "--baseline", self.paths["baseline.json"],
                                  "--from", str(T0 + 3000 * WINDOW), "--to", str(T0 + 3300 * WINDOW),
                                  "-o", self.paths["replay.md"])
        self.assertEqual(code, 0, err)
        text = Path(self.paths["replay.md"]).read_text(encoding="utf-8")
        self.assertIn("not a safety instrumented system", text)
        self.assertIn("treated as known", text)
        rows = [line for line in text.splitlines() if "test.pumping.P-1.TI-1 | mean" in line]
        self.assertTrue(rows)
        self.assertIn("approximate", rows[-1])  # model, with a threshold set by simulation
        self.assertIn("## Guarantees", text)

    def test_3_serve_command_report(self):
        plant_spec = load_plant(self.paths["plant.json"])
        now = [T0 + 3100 * WINDOW]
        service = Service(plant_spec, load_baseline(self.paths["baseline.json"]), clock=lambda: now[0], environ={})
        service.cycle()  # the first poll fixes where the unit starts reading
        while now[0] < T0 + 3300 * WINDOW:
            now[0] += 5
            service.cycle()
        self.assertTrue(service.handler("P-1").alarms["TI-1.mean"].active)
        service.close()
        code, out, err = self.cli("command", self.paths["plant.json"], "P-1", "ack", "--source", "*",
                                  "--actor", "op-17", "--reason", "seen", "--baseline", self.paths["baseline.json"])
        self.assertEqual(code, 0, err)
        self.assertIn("acknowledged", out)
        code, out, err = self.cli("command", self.paths["plant.json"], "P-1", "ack", "--source", "TI-1.mean",
                                  "--actor", "op-17", "--reason", "again", "--baseline", self.paths["baseline.json"])
        self.assertEqual((code, err.strip()), (1, "refused: already acknowledged"))
        code, out, err = self.cli("report", self.paths["plant.json"], "--from", str(T0 + 3100 * WINDOW),
                                  "--to", str(T0 + 3300 * WINDOW), "--format", "json", "-o", self.paths["report.json"])
        self.assertEqual(code, 0, err)
        report = json.loads(Path(self.paths["report.json"]).read_text(encoding="utf-8"))
        kpi = next(s for s in report["sections"] if s["title"].startswith("Alarm management"))
        actions = [t for t in kpi["tables"] if "shelving" in t["caption"]]
        self.assertEqual(actions, [])  # an acknowledgement is not a shelving
        self.assertEqual(kpi["tables"][0]["rows"][0][0], "main")


if __name__ == "__main__":
    unittest.main()
