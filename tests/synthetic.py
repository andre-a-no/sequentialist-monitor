# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""A synthetic unit: window-level AR(1) processes read every second, with faults at known times."""
import random

from sequential_analysis.forecasting import fit_ar, fit_ets

from sequentialist.core import Record
from sequentialist.monitor.config import load_baseline, load_plant
from sequentialist.monitor.unitspec import build_unit_spec

T0 = 1793491200.0  # 2026-11-01T00:00:00Z, a multiple of every window used here
WINDOW = 10
PER_WINDOW = 5  # readings per window (one every two seconds)
TAGS = {  # tag: (mean, phi, sigma of the window-level innovation, reading noise)
    "TI-1": (60.0, 0.7, 0.5, 0.2),
    "VI-1": (2.0, 0.5, 0.1, 0.05),
}


def levels(tag, n, rng, shift_at=None, shift=0.0):
    """Window-level values: AR(1) around the tag's mean, with a step `shift` (in innovation sigmas) from a window."""
    mean, phi, sigma, _ = TAGS[tag]
    x, out = mean, []
    for k in range(n):
        x = mean + phi * (x - mean) + rng.gauss(0.0, sigma)
        out.append(x + (shift * sigma if shift_at is not None and k >= shift_at else 0.0))
    return out


def records(n_windows, seed=1, faults=None, start=T0, source="hist", running=None, quality=None):
    """Readings of every tag as records, in time order.

    faults: {tag: (window, shift)}; running: a list of window flags for the tag II-1 (current above 5 when
    True); quality: {tag: set of windows whose readings are Bad}.
    """
    rng = random.Random(seed)
    faults = faults or {}
    paths = {}
    for tag in sorted(TAGS):
        shift_at, shift = faults.get(tag, (None, 0.0))
        paths[tag] = levels(tag, n_windows, rng, shift_at, shift)
    out, seq = [], 0
    for k in range(n_windows):
        for j in range(PER_WINDOW):
            t = start + k * WINDOW + j * WINDOW / PER_WINDOW
            for tag in sorted(TAGS):
                value = paths[tag][k] + rng.gauss(0.0, TAGS[tag][3])
                bad = quality is not None and k in quality.get(tag, ())
                out.append(Record(source, seq, {"tag": tag, "value": value, "quality": "Bad" if bad else "Good"},
                                  time=t))
                seq += 1
            if running is not None:
                current = 20.0 if running[k] else 0.0
                out.append(Record(source, seq, {"tag": "II-1", "value": current, "quality": "Good"}, time=t))
                seq += 1
    return out


def window_means(tag, n, seed=7):
    rng = random.Random(seed)
    noise = TAGS[tag][3] / PER_WINDOW ** 0.5
    return [x + rng.gauss(0.0, noise) for x in levels(tag, n, rng)]


def plant(detector=None, running=False, **alarms):
    detector = detector or {"type": "cusum", "shift": 1.0}
    tags = {
        "TI-1": {"name": "Bearing temperature", "unit_of_measure": "CEL", "range": [-20, 150],
                 "features": {"mean": {"model": {"type": "ar", "order": 1}, "detector": detector,
                                       "priority": "high"}}},
        "VI-1": {"unit_of_measure": "MMS",
                 "features": {"mean": {"model": {"type": "level"}, "detector": {"type": "mean_e"}}}},
    }
    unit = {"area": "pumping", "name": "Pump P-1", "source": "hist", "tags": tags}
    if running:
        tags["II-1"] = {"unit_of_measure": "AMP"}
        unit["running"] = {"tag": "II-1", "above": 5.0}
    settings = {"warmup": 5, "bad_windows": 3}
    settings.update(alarms)
    return load_plant({
        "kind": "monitor.plant/1", "site": "test", "storage": "memory:", "window": WINDOW, "lateness": 0,
        "budget": {"alarms_per_hour": 6, "detector_share": 0.5}, "alarms": settings,
        "sources": {"hist": {"type": "sql", "module": "sqlite3", "dsn_env": "DSN", "query": "SELECT 1"}},
        "units": {"P-1": unit},
    })


def baseline(plant_spec, threshold=5.0):
    """Models fitted on 2000 windows of normal operation by sequential-analysis; CUSUM threshold given."""
    from sequentialist.monitor.budget import allocate

    ar = fit_ar(window_means("TI-1", 2000), 1)
    ets = fit_ets(window_means("VI-1", 2000, seed=8))
    residuals = {"n": 2000, "mean": 0.0, "sd": 1.0, "median": 0.0, "acf": [0.0] * 10}
    features = {
        "TI-1.mean": {"model": {"type": "ar", "intercept": ar.parameters["intercept"],
                                "coefficients": ar.parameters["coefficients"], "sigma": ar.parameters["sigma"]},
                      "residuals": residuals,
                      "thresholds": {a.stream: {"arl": a.arl, "threshold": threshold, "runs": 1, "seed": 0,
                                                "arl_interval": [0.0, 0.0]}
                                     for a in allocate(plant_spec) if a.tag == "TI-1"}},
        "VI-1.mean": {"model": {"type": "level", "alpha": ets.parameters["alpha"], "sigma": ets.parameters["sigma"]},
                      "residuals": residuals},
    }
    return load_baseline({"kind": "monitor.baseline/1", "site": "test", "plant_spec": plant_spec.id,
                          "created": T0, "units": {"P-1": {"reference": {"from": T0 - 86400, "to": T0},
                                                           "windows": 2000, "features": features}}})


def unit_spec(threshold=5.0, **kwargs):
    p = plant(**kwargs)
    return build_unit_spec(p, baseline(p, threshold), "P-1")
