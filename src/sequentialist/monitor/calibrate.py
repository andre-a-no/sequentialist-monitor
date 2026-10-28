# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Calibration on a reference period of normal operation: fitted models and simulated thresholds.

The windows of the reference period are made exactly as a running unit
makes them (`windows.Windows`); only windows of a running unit with enough
Good readings are used.  Models are fitted by sequential-analysis
(`fit_ar`, the order chosen by AIC, and `fit_ets`) and are then treated as
known: the baseline says so, and so does every report.  The standardized
residuals on the reference period are described (mean, deviation, median,
autocorrelations: descriptive, guarantee none), with a warning where they
do not look like independent N(0, 1) values, on which the detectors'
guarantees rest.

Thresholds.  Shiryaev-Roberts and the e-detectors need none: sequential-
analysis states ARL >= threshold, so the threshold is the ARL.  CUSUM and
BOCPD get theirs by simulation on independent N(0, 1) residuals (the model
of normal operation), with the procedures of sequential-analysis run
without stopping: the path of a detector's statistic does not depend on
its threshold, so one path gives the run length for every threshold (the
first time its running maximum reaches it).  The ARL at a threshold is
estimated as total time over number of alarms (paths that never reach it
are censored at their end), with an interval from the exponential
approximation of run lengths; the threshold chosen is the smallest whose
lower bound reaches the target.  Such thresholds are `approximate`.
"""
from __future__ import annotations

import math
import random
import statistics
from typing import Any, Callable, Iterable, Mapping

from sequential_analysis import Run
from sequential_analysis.forecasting import fit_ar, fit_ets

from sequentialist.core import Record, SpecError, parse_spec, run_simulations

from .budget import Allocation, allocate
from .config import BaselineSpec, PlantSpec
from .pipeline import (
    Window,
    bocpd_procedure,
    detector_input,
    detector_procedure,
    model_procedure,
    next_scale,
    residual,
)
from .windows import Windows

MIN_WINDOWS = 500
LAGS = 10
Z = 1.959964  # two-sided 95%


# ---------------------------------------------------------------------------------------------- windows

def unit_windows(plant: PlantSpec, uid: str, records: Iterable[Record]) -> list[tuple[bool, dict[str, Window]]]:
    """The windows of a unit's reference period: (running, {tag: window}) in time order."""
    unit = plant.data["units"][uid]
    spec = {"window": plant.unit_window(uid), "lateness": 0, "running": unit["running"],
            "alarms": plant.data["alarms"],
            "tags": {tid: {"source_tag": t["source_tag"] or tid, "range": t["range"]}
                     for tid, t in unit["tags"].items()}}
    windows = Windows(spec)
    out: list[tuple[bool, dict[str, Window]]] = []
    running: bool | None = None if unit["running"] else True
    reference = unit["reference"]
    timed = sorted((r for r in records if r.time is not None and reference["from"] <= r.time < reference["to"]),
                   key=lambda r: r.time)  # type: ignore[arg-type, return-value]
    for record in [*timed, None]:
        for _, ws in windows.close(record.time if record is not None else reference["to"]):  # type: ignore[arg-type]
            running = windows.running(ws, running)
            out.append((bool(running), ws))
        if record is not None:
            windows.add(record.data, record.time)
    return out


def feature_series(windows: list[tuple[bool, dict[str, Window]]], tid: str, fname: str, min_good: float) -> list[float]:
    return [ws[tid].feature(fname) for running, ws in windows
            if running and ws[tid].n and ws[tid].good_share() >= min_good]


# ---------------------------------------------------------------------------------------------- models

def fit_model(model: Mapping[str, Any], series: list[float]) -> dict[str, Any]:
    """The model of a feature fitted by sequential-analysis (or as given, for a Kalman filter)."""
    kind = model["type"]
    if kind == "ar":
        orders = [model["order"]] if model["order"] else range(1, 6)
        best = min((fit_ar(series, p) for p in orders), key=lambda f: f.aic)
        p = best.parameters
        return {"type": "ar", "intercept": p["intercept"], "coefficients": list(p["coefficients"]),
                "sigma": p["sigma"]}
    if kind == "level":
        p = fit_ets(series).parameters
        return {"type": "level", "alpha": p["alpha"], "sigma": p["sigma"]}
    if kind == "trend":
        p = fit_ets(series, trend="damped" if model["damped"] else "additive").parameters
        return {"type": "trend", "alpha": p["alpha"], "beta": p["beta"], "phi": p["phi"], "sigma": p["sigma"]}
    if kind == "kalman_level":
        return {"type": "kalman_level", "q": model["q"], "r": model["r"]}
    if kind == "none":
        return {"type": "none", "mean": statistics.fmean(series), "sd": statistics.stdev(series)}
    raise ValueError(f"unknown model {kind!r}")


def residual_series(model: Mapping[str, Any], series: list[float], warmup: int) -> list[float]:
    procedure = model_procedure(dict(model))
    run = None
    if procedure is not None:
        run = Run(procedure)
        run.start()
    out, scale, seen = [], None, 0
    for value in series:
        output = run.step({"y": value}).output if run is not None else None  # type: ignore[union-attr]
        z = residual(model, output, scale, value)
        scale = next_scale(model, output) if output is not None else None
        seen += 1
        if z is not None and seen > warmup:
            out.append(z)
    if run is not None:
        run.close()
    return out


def describe(z: list[float]) -> tuple[dict[str, Any], list[str]]:
    """Mean, deviation, median and autocorrelations of the residuals (descriptive), and warnings."""
    n = len(z)
    mean = statistics.fmean(z)
    sd = statistics.stdev(z)
    var = sum((v - mean) ** 2 for v in z)
    acf = [sum((z[i] - mean) * (z[i + k] - mean) for i in range(n - k)) / var for k in range(1, LAGS + 1)]
    warnings = []
    limit = 3 / math.sqrt(n)
    if abs(mean) > limit:
        warnings.append(f"residual mean {mean:.3g} is not near 0: the model is biased on the reference period")
    if not 0.8 <= sd <= 1.25:
        warnings.append(f"residual deviation {sd:.3g} is not near 1: the model's sigma does not fit")
    lags = [k + 1 for k, r in enumerate(acf) if abs(r) > limit]
    if lags:
        warnings.append(f"residuals are autocorrelated at lags {lags}: the detector's guarantee may not hold "
                        f"(choose another model or a longer window)")
    return {"n": n, "mean": mean, "sd": sd, "median": statistics.median(z), "acf": acf}, warnings


# ---------------------------------------------------------------------------------------------- thresholds

def _statistic(detector: Mapping[str, Any]) -> tuple[Callable[[], Any], Callable[[Any], float]]:
    """A procedure that never stops and the statistic its threshold compares with."""
    if detector["type"] == "cusum":
        return (lambda: detector_procedure(detector, "up", 1e300), lambda out: out.statistic)
    if detector["type"] == "bocpd":
        return (lambda: bocpd_procedure(detector, None), lambda out: out.recent_change_probability)
    raise ValueError(f"{detector['type']} needs no simulated threshold")


def _paths(detector: Mapping[str, Any], cap: float, limit: int, runs: int, seed: int) -> list[tuple[list, int]]:
    """For each run, the times its running maximum grew and to what, until it reaches `cap` or `limit` steps."""
    make, statistic = _statistic(detector)

    def trial(rng: random.Random, i: int) -> tuple[list, int]:
        procedure = make()
        state = procedure.initial_state()
        best, grew = -math.inf, []
        for t in range(1, limit + 1):
            state, out = procedure.step(state, detector_input(dict(detector), rng.gauss(0.0, 1.0)))
            s = statistic(out)
            if s > best:
                best = s
                grew.append((t, s))
                if s >= cap:
                    return grew, t
        return grew, limit

    return run_simulations(trial, runs, seed=seed)


def _arl(paths: list[tuple[list, int]], h: float) -> tuple[float, float, float, int]:
    """ARL at threshold h: total time over alarms (censored paths count their time), with a 95% interval."""
    total, alarms = 0, 0
    for grew, end in paths:
        hit = next((t for t, s in grew if s >= h), None)
        if hit is None:
            total += end
        else:
            total += hit
            alarms += 1
    if alarms == 0:
        return math.inf, math.inf, math.inf, 0
    arl = total / alarms
    spread = Z / math.sqrt(alarms)
    return arl, arl * math.exp(-spread), arl * math.exp(spread), alarms


def simulated_threshold(detector: Mapping[str, Any], arl: float, runs: int, seed: int) -> dict[str, Any]:
    """The smallest threshold whose ARL, by simulation on N(0, 1) residuals, has a 95% lower bound >= arl."""
    pilot_runs = max(20, runs // 10)
    cap = math.log(arl) if detector["type"] == "cusum" else 1.0 - 1e-12  # CUSUM: ARL >= e^h, enough
    limit = max(100, int(5 * arl))  # longer runs are censored there
    pilot = _paths(detector, cap, limit, pilot_runs, seed + 10**6)
    h = _search(pilot, arl, cap, conservative=False)
    if detector["type"] == "cusum":
        cap = min(cap, h + 0.75)  # the main runs need only reach a little beyond
    paths = _paths(detector, cap, limit, runs, seed)
    h = _search(paths, arl, cap, conservative=True)
    estimate, low, high, _ = _arl(paths, h)
    return {"arl": arl, "threshold": h, "runs": runs, "seed": seed, "arl_interval": [low, high],
            "arl_estimate": estimate}


def _search(paths: list[tuple[list, int]], arl: float, cap: float, conservative: bool) -> float:
    levels = sorted({s for grew, _ in paths for _, s in grew if 0 < s <= cap})
    if not levels:
        raise ValueError("the simulation never moved the detector's statistic")
    lo, hi = 0, len(levels) - 1
    if _arl(paths, levels[hi])[1 if conservative else 0] < arl:
        return levels[hi]
    while lo < hi:  # ARL(h) does not decrease with h
        mid = (lo + hi) // 2
        if _arl(paths, levels[mid])[1 if conservative else 0] >= arl:
            hi = mid
        else:
            lo = mid + 1
    return levels[lo]


# ---------------------------------------------------------------------------------------------- calibrate

def calibrate(plant: PlantSpec, histories: Mapping[str, Iterable[Record]], *, created: float, runs: int = 400,
              bocpd_runs: int = 50, seed: int = 0, min_windows: int = MIN_WINDOWS,
              previous: BaselineSpec | None = None, cache: dict[tuple, dict[str, Any]] | None = None,
              progress: Callable[[str], None] = lambda message: None) -> BaselineSpec:
    """A baseline for the units in `histories` (records of their reference periods); the units of `previous`
    that are not recalibrated are kept.  `cache` keeps simulated thresholds between calls (they depend only on
    the detector, the ARL, the runs and the seed).  SpecError lists the features that cannot be calibrated."""
    allocations = allocate(plant)
    cache = {} if cache is None else cache
    units: dict[str, Any] = dict(previous.data["units"]) if previous is not None else {}
    errors: list[tuple[str, str]] = []
    for uid, records in sorted(histories.items()):
        unit = plant.data["units"][uid]
        where = f"/units/{uid}"
        if unit["reference"] is None:
            errors.append((f"{where}/reference", "a unit needs a reference period to be calibrated"))
            continue
        windows = unit_windows(plant, uid, records)
        features: dict[str, Any] = {}
        used = 0
        for tid, tag in sorted(unit["tags"].items()):
            for fname, feature in sorted(tag["features"].items()):
                series = feature_series(windows, tid, fname, plant.data["alarms"]["min_good"])
                used = max(used, len(series))
                at = f"{where}/tags/{tid}/features/{fname}"
                if len(series) < min_windows:
                    errors.append((at, f"{len(series)} usable windows in the reference period; at least "
                                       f"{min_windows} are needed"))
                    continue
                progress(f"{uid}: {tid}.{fname}: {len(series)} windows")
                model = fit_model(feature["model"], series)
                described, warnings = describe(residual_series(model, series, plant.data["alarms"]["warmup"]))
                thresholds = {}
                for a in _streams(allocations, uid, tid, fname):
                    if feature["detector"]["type"] in ("cusum", "bocpd"):
                        key = (_detector_key(feature["detector"]), a.arl, runs, bocpd_runs, seed)
                        if key not in cache:
                            progress(f"  threshold of {feature['detector']['type']} for ARL {a.arl:.6g} by simulation")
                            n = runs if feature["detector"]["type"] == "cusum" else bocpd_runs
                            cache[key] = simulated_threshold(feature["detector"], a.arl, n, seed)
                        found = cache[key]
                        thresholds[a.stream] = {k: found[k] for k in ("arl", "threshold", "runs", "seed",
                                                                      "arl_interval")}
                features[f"{tid}.{fname}"] = {"model": model, "residuals": described, "thresholds": thresholds,
                                              "warnings": warnings}
        units[uid] = {"reference": unit["reference"], "windows": used, "features": features}
    if errors:
        raise SpecError(errors)
    data = {"kind": BaselineSpec.kind, "site": plant.data["site"], "plant_spec": plant.id, "created": created,
            "units": units}
    return parse_spec(data, BaselineSpec)


def _streams(allocations: list[Allocation], uid: str, tid: str, fname: str) -> list[Allocation]:
    return [a for a in allocations if (a.unit, a.tag, a.feature) == (uid, tid, fname)]


def _detector_key(detector: Mapping[str, Any]) -> tuple:
    # the direction does not matter: N(0, 1) is symmetric, the threshold is the same both ways
    return tuple(sorted((k, v) for k, v in detector.items() if k != "direction"))
