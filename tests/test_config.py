# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from sequentialist.core import SpecError, json_schema
from sequentialist.core.cli import main
from sequentialist.monitor.budget import allocate
from sequentialist.monitor.config import BaselineSpec, PlantSpec, UnitSpec, load_baseline, load_plant
from sequentialist.monitor.unitspec import build_unit_spec

from .plants import baseline, plant


def errors_of(data):
    try:
        load_plant(data)
    except SpecError as error:
        return dict(error.errors)
    raise AssertionError("the plant was accepted")


class PlantTest(unittest.TestCase):
    def test_normal_form(self):
        spec = load_plant(plant())
        d = spec.data
        self.assertEqual(d["window"], 60)
        self.assertEqual(d["lateness"], 30)
        self.assertEqual(d["budget"], {"alarms_per_hour": 6, "detector_share": 0.5})
        self.assertEqual(d["severity"], {"urgent": 900, "high": 700, "medium": 500, "low": 300})
        self.assertEqual(d["consoles"], {"main": {"name": None}})
        self.assertEqual(d["units"]["P-101"]["console"], "main")
        self.assertEqual(d["units"]["P-101"]["reference"]["from"], 1788220800.0)
        feature = d["units"]["P-101"]["tags"]["TI-101"]["features"]["mean"]
        self.assertEqual(feature["detector"], {"type": "cusum", "shift": 1.0, "direction": "both"})
        self.assertEqual(feature["model"], {"type": "ar", "order": 1})
        # the same plant spelled otherwise has the same id
        self.assertEqual(spec.id, load_plant(plant(window=60, lateness=30)).id)

    def test_every_error_at_once_with_its_pointer(self):
        data = plant()
        unit = data["units"]["P-101"]
        unit["source"] = "nowhere"
        unit["console"] = "night"
        unit["running"]["tag"] = "XX-1"
        unit["tags"]["TI-101"]["range"] = [150, -20]
        unit["tags"]["VI-101"]["features"]["max"]["model"] = {"type": "none"}
        errors = errors_of(data)
        self.assertEqual(set(errors), {
            "/units/P-101/source", "/units/P-101/console", "/units/P-101/running/tag",
            "/units/P-101/tags/TI-101/range", "/units/P-101/tags/VI-101/features/max/guarantee"})

    def test_ids_are_ascii(self):
        data = plant()
        data["units"]["Н-101"] = data["units"].pop("P-101")
        self.assertIn("/units/Н-101", errors_of(data))

    def test_schema_errors(self):
        data = plant()
        data["units"]["P-101"]["tags"]["TI-101"]["features"]["mean"]["detector"] = {"type": "ewma"}
        data["units"]["P-101"]["tags"]["TI-101"]["features"]["median"] = {}
        data["units"]["P-101"]["tags"]["TI-101"]["unit_of_measure"] = "°C"
        data["sources"]["historian"]["dsn"] = "Server=x;Password=secret"
        errors = errors_of(data)
        for where in ("/units/P-101/tags/TI-101/features/mean/detector/type",
                      "/units/P-101/tags/TI-101/features/median",
                      "/units/P-101/tags/TI-101/unit_of_measure",
                      "/sources/historian/dsn"):
            self.assertIn(where, errors)

    def test_secrets_are_named_not_given(self):
        # connection strings, tokens and passwords are environment variables: the spec holds their names
        schema = json.dumps(json_schema(PlantSpec))
        for word in ("dsn_env", "url_env", "password_env", "token_env"):
            self.assertIn(word, schema)
        for word in ('"dsn"', '"url"', '"password"', '"token"'):
            self.assertNotIn(word, schema)

    def test_too_many_bocpd(self):
        data = plant()
        tags = data["units"]["P-101"]["tags"]
        for i in range(21):
            tags[f"T{i}"] = {"features": {"mean": {"model": {"type": "ar"}, "detector": {"type": "bocpd"}}}}
        self.assertIn("/units", errors_of(data))


class BudgetTest(unittest.TestCase):
    def test_closed_form(self):
        # B = 6 alarms per hour, s = 0.5, three detector streams (CUSUM up and down, one e-detector), W = 60 s:
        # ARL = 3600 D / (B s W) = 3600 * 3 / (3 * 60) = 60 windows each
        allocations = allocate(load_plant(plant()))
        self.assertEqual([(a.tag, a.feature, a.stream) for a in allocations],
                         [("TI-101", "mean", "up"), ("TI-101", "mean", "down"), ("VI-101", "max", "detector")])
        for a in allocations:
            self.assertAlmostEqual(a.arl, 60.0)
        self.assertAlmostEqual(sum(a.per_hour for a in allocations), 3.0)

    def test_given_arl_comes_first(self):
        data = plant()
        data["units"]["P-101"]["tags"]["VI-101"]["features"]["max"]["arl"] = 120  # 0.5 alarms per hour
        allocations = {a.stream: a for a in allocate(load_plant(data))}
        self.assertTrue(allocations["detector"].given)
        self.assertAlmostEqual(allocations["detector"].arl, 120)
        # the other two share 3 - 0.5 = 2.5 per hour: ARL = 3600 * 2 / (2.5 * 60) = 48
        self.assertAlmostEqual(allocations["up"].arl, 48)
        self.assertAlmostEqual(sum(a.per_hour for a in allocations.values()), 3.0)

    def test_budget_exceeded(self):
        data = plant()
        data["units"]["P-101"]["tags"]["VI-101"]["features"]["max"]["arl"] = 10  # 6 per hour, over 3
        with self.assertRaises(ValueError):
            allocate(load_plant(data))

    def test_window_of_the_unit(self):
        data = plant()
        data["units"]["P-101"]["window"] = "10s"
        for a in allocate(load_plant(data)):
            self.assertAlmostEqual(a.arl, 360.0)


class UnitSpecTest(unittest.TestCase):
    def setUp(self):
        self.plant = load_plant(plant())
        self.baseline = load_baseline(baseline(self.plant, {"up": 7.5, "down": 7.5}))

    def test_build(self):
        spec = build_unit_spec(self.plant, self.baseline, "P-101")
        d = spec.data
        self.assertEqual(spec.task_id(), f"plant-1.pumping.P-101.{spec.id[:12]}")
        self.assertEqual(d["baseline"], self.baseline.id)
        self.assertEqual(d["window"], 60)
        ti = d["tags"]["TI-101"]["features"]["mean"]
        self.assertEqual(ti["guarantee"], "model")
        self.assertEqual(ti["detector"], {"type": "cusum", "shift": 1.0})
        self.assertEqual(ti["streams"]["up"], {"arl": 60.0, "arl_from": "budget", "threshold": 7.5,
                                               "threshold_from": "simulation"})
        vi = d["tags"]["VI-101"]["features"]["max"]
        self.assertEqual(vi["streams"], {"detector": {"arl": 60.0, "arl_from": "budget", "threshold": 60.0,
                                                      "threshold_from": "bound"}})
        self.assertEqual(d["tags"]["VI-101"]["source_tag"], "P101.VIB")
        self.assertEqual(d["tags"]["II-101"]["features"], {})

    def test_baseline_for_another_budget_is_refused(self):
        data = plant(budget={"alarms_per_hour": 3})
        with self.assertRaises(SpecError) as caught:
            build_unit_spec(load_plant(data), self.baseline, "P-101")
        self.assertIn("run calibrate again", str(caught.exception))

    def test_missing_baseline(self):
        with self.assertRaises(SpecError) as caught:
            build_unit_spec(self.plant, None, "P-101")
        self.assertEqual(len(caught.exception.errors), 2)

    def test_kalman_with_a_bound_needs_no_baseline(self):
        data = plant()
        tags = data["units"]["P-101"]["tags"]
        del tags["TI-101"]
        tags["VI-101"]["features"]["max"]["model"] = {"type": "kalman_level", "q": 0.01, "r": 1.0}
        spec = build_unit_spec(load_plant(data), None, "P-101")
        self.assertEqual(spec.data["tags"]["VI-101"]["features"]["max"]["model"],
                         {"type": "kalman_level", "q": 0.01, "r": 1.0})
        self.assertIsNone(spec.data["baseline"])


class CommandTest(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_check_and_schema(self):
        p = load_plant(plant())
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "plant.json").write_text(json.dumps(plant()), encoding="utf-8")
            (Path(d) / "baseline.json").write_text(json.dumps(baseline(p)), encoding="utf-8")
            code, out, _ = self.run_cli("monitor", "check", str(Path(d) / "plant.json"),
                                        "--baseline", str(Path(d) / "baseline.json"))
            self.assertEqual(code, 0, out)
            self.assertIn("3 detector streams", out)
            self.assertIn("plant-1.pumping.P-101.", out)
            bad = plant()
            bad["units"]["P-101"]["source"] = "nowhere"
            (Path(d) / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
            code, _, err = self.run_cli("monitor", "check", str(Path(d) / "bad.json"))
            self.assertEqual(code, 2)
            self.assertIn("/units/P-101/source", err)
            code, out, _ = self.run_cli("monitor", "budget", str(Path(d) / "plant.json"),
                                        "--baseline", str(Path(d) / "baseline.json"))
            self.assertEqual(code, 0)
            self.assertIn("TI-101.mean", out)
            self.assertIn("8 (simulation)", out)
            self.assertIn("60 (bound)", out)
        for name, cls in (("plant", PlantSpec), ("baseline", BaselineSpec), ("unit", UnitSpec)):
            code, out, _ = self.run_cli("monitor", "schema", name)
            self.assertEqual(json.loads(out)["title"], cls.kind)


if __name__ == "__main__":
    unittest.main()
