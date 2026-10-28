# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
import random
import unittest

from sequentialist.core import SpecError, replay
from sequentialist.monitor.budget import allocate
from sequentialist.monitor.calibrate import calibrate, describe, simulated_threshold
from sequentialist.monitor.config import load_plant
from sequentialist.monitor.unit import KIND, UnitHandler
from sequentialist.monitor.unitspec import build_unit_spec

from .reference import cusum_arl
from .synthetic import T0, WINDOW, plant, records


def reference_plant(**kwargs):
    data = plant(**kwargs).to_json()
    data["units"]["P-1"]["reference"] = {"from": T0, "to": T0 + 3000 * WINDOW}
    return load_plant(data)


class CalibrateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plant = reference_plant()
        cls.baseline = calibrate(cls.plant, {"P-1": records(3200, seed=5)}, created=T0, runs=300)

    def test_models_fitted_by_sequential_analysis(self):
        unit = self.baseline.data["units"]["P-1"]
        self.assertEqual(unit["windows"], 3000)  # only the reference period
        ti = unit["features"]["TI-1.mean"]
        self.assertEqual(ti["model"]["type"], "ar")
        self.assertEqual(len(ti["model"]["coefficients"]), 1)
        self.assertAlmostEqual(ti["model"]["coefficients"][0], 0.7, delta=0.05)  # the true phi
        self.assertAlmostEqual(ti["model"]["intercept"] / (1 - ti["model"]["coefficients"][0]), 60.0, delta=0.2)
        self.assertEqual(ti["warnings"], [])
        self.assertLess(abs(ti["residuals"]["mean"]), 0.1)
        self.assertEqual(unit["features"]["VI-1.mean"]["model"]["type"], "level")

    def test_cusum_threshold_against_brook_evans(self):
        # the budget gives every one of the three streams ARL = 3600 * 3 / (3 * 10) = 360 windows
        thresholds = self.baseline.data["units"]["P-1"]["features"]["TI-1.mean"]["thresholds"]
        self.assertEqual(sorted(thresholds), ["down", "up"])
        h = thresholds["up"]["threshold"]
        self.assertEqual(thresholds["down"]["threshold"], h)
        self.assertEqual(thresholds["up"]["arl"], 360.0)
        # chosen so that the simulated ARL's lower bound reaches 360: the exact ARL is at least that, and not
        # far above it
        self.assertGreaterEqual(cusum_arl(h), 360 * 0.97)
        self.assertLess(cusum_arl(h), 360 * 1.4)
        low, high = thresholds["up"]["arl_interval"]
        self.assertLess(low, cusum_arl(h))
        self.assertLess(cusum_arl(h), high)

    def test_the_baseline_builds_the_unit(self):
        spec = build_unit_spec(self.plant, self.baseline, "P-1")
        self.assertEqual(spec.data["tags"]["TI-1"]["features"]["mean"]["streams"]["up"]["threshold_from"],
                         "simulation")

    def test_too_short_a_reference(self):
        with self.assertRaises(SpecError) as caught:
            calibrate(self.plant, {"P-1": records(300, seed=5)}, created=T0)
        self.assertEqual({p for p, _ in caught.exception.errors},
                         {"/units/P-1/tags/TI-1/features/mean", "/units/P-1/tags/VI-1/features/mean"})

    def test_the_budget_holds_on_normal_operation(self):
        # 20000 windows (about 55 hours) of normal operation through the calibrated unit: the expected false
        # alarms of the console are at most B s = 3 per hour when every ARL holds
        spec = build_unit_spec(self.plant, self.baseline, "P-1")
        task = replay(KIND, spec.data, UnitHandler(spec.data), records(20000, seed=99), task_id="u")
        fires = [e for e in task.audit() if e.decision in ("activated", "refired")
                 and e.basis.get("condition") == "mean"]
        task.close()
        hours = 20000 * WINDOW / 3600
        rate = len(fires) / hours
        self.assertLess(rate, 3.0 + 3 * (3.0 * hours) ** 0.5 / hours, (len(fires), hours))


class ThresholdTest(unittest.TestCase):
    def test_brook_evans_value(self):
        # one-sided CUSUM with k = 0.5: Brook-Evans gives ARL 335.4 at h = 4
        found = simulated_threshold({"type": "cusum", "shift": 1.0}, cusum_arl(4.0), 600, 3)
        self.assertGreaterEqual(found["threshold"], 3.9)
        self.assertLess(found["threshold"], 4.35)


class DescribeTest(unittest.TestCase):
    def test_white_noise_passes_and_autocorrelation_is_reported(self):
        rng = random.Random(4)
        white = [rng.gauss(0, 1) for _ in range(3000)]
        self.assertEqual(describe(white)[1], [])
        ar = [0.0]
        for _ in range(3000):
            ar.append(0.5 * ar[-1] + rng.gauss(0, 0.87))
        warnings = describe(ar)[1]
        self.assertEqual(len(warnings), 1)
        self.assertIn("autocorrelated at lags [1, 2", warnings[0])

    def test_allocations_used(self):
        self.assertEqual({a.arl for a in allocate(reference_plant())}, {360.0})


if __name__ == "__main__":
    unittest.main()
