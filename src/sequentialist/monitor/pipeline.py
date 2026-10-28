# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""The pipeline of one feature: window → model → residual → detector, made of sequential-analysis procedures.

Change detectors guarantee their average run length to a false alarm on
independent data (or a martingale structure); raw readings of a process,
taken every few seconds, are strongly autocorrelated and break that.  So a
window summarises the readings, a model of normal operation forecasts the
next window, and the detector watches the standardized one-step error

    z_t = (x_t - forecast of x_t) / (its standard deviation),

which is independent N(0, 1) while the model holds.  The guarantee of the
pipeline is therefore `model`: the weakest of its stages as
sequential-analysis declares them.  Nothing here computes a statistic;
every number comes from a procedure of sequential-analysis.
"""
from __future__ import annotations

import math
from typing import Any

from sequential_analysis import Procedure
from sequential_analysis.procedures import (
    CUSUM,
    AutoRegressive,
    BayesianChangePoint,
    ExponentialSmoothing,
    Gaussian,
    LocalLevelKalman,
    MeanEDetector,
    NormalGammaModel,
    QuantileEDetector,
    ShiryaevRoberts,
)

GUARANTEE_ORDER = ("exact", "asymptotic", "model", "approximate", "none")
E_DIRECTIONS = {"up": "increase", "down": "decrease", "both": "both"}
DIFFUSE = 1e6  # prior variance of an unknown level: the first windows set it


def weakest(*guarantees: str) -> str:
    """The weakest of the guarantees of a pipeline's stages."""
    return max(guarantees, key=GUARANTEE_ORDER.index)


def model_procedure(model: dict[str, Any]) -> Procedure | None:
    """The forecaster of a fitted model (None for a feature watched without a model)."""
    kind = model["type"]
    if kind == "ar":
        return AutoRegressive(len(model["coefficients"]), coefficients=model["coefficients"],
                              intercept=model["intercept"], sigma=model["sigma"])
    if kind == "level":
        return ExponentialSmoothing(model["alpha"], sigma=model["sigma"])
    if kind == "trend":
        damped = model["phi"] < 1.0
        return ExponentialSmoothing(model["alpha"], beta=model["beta"], phi=model["phi"],
                                    trend="damped" if damped else "additive", sigma=model["sigma"])
    if kind == "kalman_level":
        return LocalLevelKalman(model["q"], model["r"], p0=DIFFUSE)
    if kind == "none":
        return None
    raise ValueError(f"unknown model {kind!r}")


def detector_procedure(detector: dict[str, Any], stream: str, threshold: float) -> Procedure:
    """The detector of one stream ("up", "down" or "detector"), fed {"z": residual}."""
    kind = detector["type"]
    if kind in ("cusum", "sr"):
        shift = detector["shift"] if stream == "up" else -detector["shift"]
        cls = CUSUM if kind == "cusum" else ShiryaevRoberts
        return cls(Gaussian(1.0), 0.0, shift, threshold, input="z")
    if kind == "mean_e":
        c = detector["clip"]
        return MeanEDetector(0.0, -c, c, arl=threshold, direction=E_DIRECTIONS[detector["direction"]],
                             statistic=detector["statistic"], input="z")
    if kind == "quantile_e":
        return QuantileEDetector(0.0, 0.5, arl=threshold, direction=E_DIRECTIONS[detector["direction"]],
                                 statistic=detector["statistic"], input="z")
    if kind == "bocpd":
        return BayesianChangePoint(NormalGammaModel(0.0, 1.0, 1.0, 1.0), detector["hazard"],
                                   alarm_probability=threshold, max_hypotheses=detector["max_hypotheses"],
                                   input="z")
    raise ValueError(f"unknown detector {kind!r}")


def detector_input(detector: dict[str, Any], z: float) -> dict[str, float]:
    if detector["type"] == "mean_e":
        c = detector["clip"]
        z = min(max(z, -c), c)
    return {"z": z}


def residual(model: dict[str, Any], output: Any, previous_scale: float | None, value: float) -> float | None:
    """The standardized one-step error of a window; None while the model cannot forecast yet."""
    kind = model["type"]
    if kind == "none":
        return (value - model["mean"]) / model["sd"]
    if kind == "kalman_level":
        return output.innovation / math.sqrt(output.innovation_var) if output.n > 1 else None
    if output.error is None or previous_scale is None:
        return None
    return output.error / previous_scale


def next_scale(model: dict[str, Any], output: Any) -> float | None:
    """The standard deviation of the forecast of the next window (forecasters only)."""
    if model["type"] in ("none", "kalman_level"):
        return None
    return output.scale[0] if output.scale else None


class Window:
    """The readings of one tag in one window: count, sum, extremes and the readings that were not Good."""

    __slots__ = ("bad", "high", "low", "n", "total")

    def __init__(self, n: int = 0, total: float = 0.0, low: float = math.inf, high: float = -math.inf,
                 bad: int = 0):
        self.n, self.total, self.low, self.high, self.bad = n, total, low, high, bad

    def add(self, value: float) -> None:
        self.n += 1
        self.total += value
        if value < self.low:
            self.low = value
        if value > self.high:
            self.high = value

    def feature(self, name: str) -> float:
        if name == "mean":
            return self.total / self.n
        if name == "max":
            return self.high
        if name == "min":
            return self.low
        if name == "range":
            return self.high - self.low
        raise ValueError(f"unknown feature {name!r}")

    def good_share(self) -> float:
        seen = self.n + self.bad
        return self.n / seen if seen else 0.0

    def to_json(self) -> list[Any]:
        return [self.n, self.total, self.low if self.n else None, self.high if self.n else None, self.bad]

    @classmethod
    def from_json(cls, data: list[Any]) -> Window:
        n, total, low, high, bad = data
        return cls(n, total, math.inf if low is None else low, -math.inf if high is None else high, bad)
