# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""The pipeline against references: closed forms, Brook-Evans, published tables and the bounds of the procedures.

Simulations run with fixed seeds and compare within Monte Carlo error (four standard errors), not to the digit.
"""
import math
import random
import unittest

from sequential_analysis import Run

from sequentialist.monitor.pipeline import detector_input, detector_procedure, model_procedure, next_scale, residual

from .reference import cusum_arl, mean_and_error, run_length


def residuals(model, values):
    run = Run(model_procedure(model))
    run.start()
    out, scale = [], None
    for v in values:
        output = run.step({"y": v}).output
        z = residual(model, output, scale, v)
        scale = next_scale(model, output)
        if z is not None:
            out.append(z)
    return out


def autocorrelation(z, lag):
    n, m = len(z), sum(z) / len(z)
    var = sum((v - m) ** 2 for v in z)
    return sum((z[i] - m) * (z[i + lag] - m) for i in range(n - lag)) / var


class ResidualTest(unittest.TestCase):
    """While the model holds, the standardized residuals are independent N(0, 1)."""

    def check_white(self, z):
        n = len(z)
        mean = sum(z) / n
        var = sum((v - mean) ** 2 for v in z) / (n - 1)
        self.assertLess(abs(mean), 4 / math.sqrt(n))
        self.assertLess(abs(var - 1), 4 * math.sqrt(2 / n))
        for lag in (1, 2, 5):
            self.assertLess(abs(autocorrelation(z, lag)), 4 / math.sqrt(n))

    def test_ar2(self):
        rng = random.Random(11)
        c, phi, sigma = 10.0, (0.5, 0.2), 2.0
        y = [c / (1 - sum(phi))] * 2
        for _ in range(5000):
            y.append(c + phi[0] * y[-1] + phi[1] * y[-2] + rng.gauss(0, sigma))
        z = residuals({"type": "ar", "intercept": c, "coefficients": list(phi), "sigma": sigma}, y[2:])
        self.check_white(z)

    def test_local_level(self):
        # ETS(A,N,N): y_t = l_(t-1) + e_t, l_t = l_(t-1) + alpha e_t
        rng = random.Random(12)
        alpha, sigma, level, y = 0.3, 0.5, 100.0, []
        for _ in range(5000):
            e = rng.gauss(0, sigma)
            y.append(level + e)
            level += alpha * e
        self.check_white(residuals({"type": "level", "alpha": alpha, "sigma": sigma}, y))

    def test_local_linear_trend(self):
        rng = random.Random(13)
        alpha, beta, sigma, level, slope, y = 0.4, 0.05, 1.0, 50.0, 0.2, []
        for _ in range(5000):
            e = rng.gauss(0, sigma)
            y.append(level + slope + e)
            level, slope = level + slope + alpha * e, slope + beta * e
        self.check_white(residuals({"type": "trend", "alpha": alpha, "beta": beta, "phi": 1.0, "sigma": sigma}, y))


class DetectorTest(unittest.TestCase):
    def arl(self, detector, stream, threshold, draw, runs, limit=10**6):
        procedure = detector_procedure(detector, stream, threshold)
        return mean_and_error([run_length(procedure, lambda: detector_input(detector, draw()), limit)
                               for _ in range(runs)])

    def test_brook_evans_against_the_published_table(self):
        # two-sided tabular CUSUM, k = 0.5 (Montgomery, Introduction to Statistical Quality Control, ARL table of
        # the CUSUM): h = 4 gives 168 in control and 8.38 at a shift of one sigma; h = 5 gives 465 and 10.4.
        # Two symmetric one-sided charts: 1/ARL = 2/ARL_one_sided in control.
        self.assertAlmostEqual(cusum_arl(4) / 2, 168, delta=1.7)
        self.assertAlmostEqual(cusum_arl(5) / 2, 465, delta=4.7)
        self.assertAlmostEqual(cusum_arl(4, mu=1.0), 8.38, delta=0.01)
        self.assertAlmostEqual(cusum_arl(5, mu=1.0), 10.4, delta=0.05)

    def test_cusum_against_brook_evans(self):
        # the pipeline's CUSUM (Gaussian(1), theta 0 -> 1) is the tabular CUSUM with k = 0.5 on z
        rng = random.Random(21)
        detector = {"type": "cusum", "shift": 1.0}
        for mu, runs in ((0.0, 1500), (1.0, 3000)):
            mean, se = self.arl(detector, "up", 4.0, lambda mu=mu: rng.gauss(mu, 1.0), runs)
            self.assertLess(abs(mean - cusum_arl(4.0, mu=mu)), 4 * se, (mu, mean, se))
        # the downward stream is the mirror image
        mean, se = self.arl(detector, "down", 4.0, lambda: rng.gauss(-1.0, 1.0), 3000)
        self.assertLess(abs(mean - cusum_arl(4.0, mu=1.0)), 4 * se)

    def test_shiryaev_roberts_arl_is_at_least_a(self):
        rng = random.Random(22)
        mean, se = self.arl({"type": "sr", "shift": 1.0}, "up", 100.0, lambda: rng.gauss(0, 1), 1500)
        self.assertGreater(mean + 4 * se, 100.0)

    def test_e_detectors_hold_their_arl_whatever_the_symmetric_distribution(self):
        rng = random.Random(23)
        t3 = lambda: rng.gauss(0, 1) / math.sqrt(sum(rng.gauss(0, 1) ** 2 for _ in range(3)) / 3)
        draws = {"normal": lambda: rng.gauss(0, 1), "t3": t3, "uniform": lambda: rng.uniform(-3, 3)}
        for name, draw in draws.items():
            for detector in ({"type": "mean_e", "clip": 4.0, "direction": "both", "statistic": "shiryaev-roberts"},
                             {"type": "quantile_e", "direction": "both", "statistic": "shiryaev-roberts"}):
                mean, se = self.arl(detector, "detector", 50.0, draw, 600)
                self.assertGreater(mean + 4 * se, 50.0, (name, detector["type"], mean, se))

    def test_without_a_model_the_arl_does_not_hold(self):
        # why the pipeline needs a model: raw AR(1) values with phi = 0.9, standardized by their stationary
        # deviation, are N(0, 1) one at a time but not independent, and SR with A = 100 alarms far sooner
        rng = random.Random(24)
        phi, state = 0.9, [0.0]

        def draw():
            state[0] = phi * state[0] + rng.gauss(0, math.sqrt(1 - phi ** 2))
            return state[0]

        mean, se = self.arl({"type": "sr", "shift": 1.0}, "up", 100.0, draw, 600)
        self.assertLess(mean + 4 * se, 50.0, (mean, se))
        self.assertGreater(cusum_arl(4.0), math.exp(4.0))  # and the bound ARL >= e^h of CUSUM holds


if __name__ == "__main__":
    unittest.main()
