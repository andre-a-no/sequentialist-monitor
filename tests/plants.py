# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Plants and baselines the tests share."""
import copy


def plant(**changes):
    """A small plant: one pump with a temperature (AR + CUSUM) and a vibration (level + e-detector)."""
    data = {
        "kind": "monitor.plant/1",
        "site": "plant-1",
        "storage": "memory:",
        "window": "60s",
        "lateness": "30s",
        "sources": {"historian": {"type": "sql", "module": "sqlite3", "dsn_env": "HISTORIAN_DSN",
                                  "query": "SELECT tag, ts, value FROM samples WHERE ts >= ? AND ts < ?"}},
        "units": {
            "P-101": {
                "area": "pumping",
                "name": "Насос Н-101",
                "source": "historian",
                "running": {"tag": "II-101", "above": 5.0},
                "reference": {"from": "2026-09-01T00:00:00Z", "to": "2026-09-29T00:00:00Z"},
                "tags": {
                    "II-101": {"name": "Ток двигателя", "unit_of_measure": "AMP"},
                    "TI-101": {
                        "name": "Температура подшипника",
                        "unit_of_measure": "CEL",
                        "range": [-20, 150],
                        "features": {"mean": {"model": {"type": "ar", "order": 1},
                                              "detector": {"type": "cusum", "shift": 1.0},
                                              "priority": "high"}},
                    },
                    "VI-101": {
                        "unit_of_measure": "MMS",
                        "source_tag": "P101.VIB",
                        "features": {"max": {"model": {"type": "level"},
                                             "detector": {"type": "mean_e", "clip": 4}}},
                    },
                },
            },
        },
    }
    data.update(copy.deepcopy(changes))
    return data


def baseline(plant_spec, thresholds=None):
    """A baseline for `plant()`: fitted models by hand, CUSUM thresholds for the ARLs of the budget."""
    from sequentialist.monitor.budget import allocate

    arls = {(a.tag, a.feature, a.stream): a.arl for a in allocate(plant_spec) if a.unit == "P-101"}
    thresholds = thresholds or {}
    residuals = {"n": 1000, "mean": 0.0, "sd": 1.0, "median": 0.0, "acf": [0.0] * 10}
    return {
        "kind": "monitor.baseline/1",
        "site": "plant-1",
        "plant_spec": plant_spec.id,
        "created": "2026-10-01T00:00:00Z",
        "units": {"P-101": {
            "reference": {"from": "2026-09-01T00:00:00Z", "to": "2026-09-29T00:00:00Z"},
            "windows": 40000,
            "features": {
                "TI-101.mean": {
                    "model": {"type": "ar", "intercept": 20.0, "coefficients": [0.6], "sigma": 0.5},
                    "residuals": residuals,
                    "thresholds": {s: {"arl": arls[("TI-101", "mean", s)], "threshold": thresholds.get(s, 8.0),
                                       "runs": 100, "seed": 0, "arl_interval": [1.0, 2.0]}
                                   for s in ("up", "down")},
                },
                "VI-101.max": {"model": {"type": "level", "alpha": 0.3, "sigma": 0.2}, "residuals": residuals},
            },
        }},
    }
