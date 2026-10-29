# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""The spec of one unit, built from the plant and its baseline.

Everything a unit's task needs is resolved here: the fitted model of every
feature, the ARL of every detector stream from the plant's budget, and the
threshold that gives it.  Shiryaev-Roberts and the e-detectors take the ARL
itself as their threshold (sequential-analysis states ARL >= threshold);
CUSUM and BOCPD take the threshold that calibration found by simulation
for that ARL.  A baseline calibrated for another budget is refused.
"""
from __future__ import annotations

import math
from typing import Any

from sequentialist.core import SpecError, parse_spec

from .budget import Allocation, allocate, detector_streams
from .config import BaselineSpec, PlantSpec, UnitSpec

BOUND = ("sr", "mean_e", "quantile_e")  # threshold = ARL, as the procedure states
SIMULATED = ("cusum", "bocpd")  # threshold from a simulation at calibration


def needs_baseline(feature: dict[str, Any]) -> bool:
    """Whether calibration must fit this feature's model or find its thresholds."""
    return feature["model"]["type"] != "kalman_level" or feature["detector"]["type"] in SIMULATED


def build_unit_spec(plant: PlantSpec, baseline: BaselineSpec | None, uid: str,
                    allocations: list[Allocation] | None = None) -> UnitSpec:
    """The spec of unit `uid`; SpecError lists every feature the baseline does not cover."""
    if uid not in plant.data["units"]:
        raise SpecError([("/units", f"no unit {uid!r}")])
    unit = plant.data["units"][uid]
    if allocations is None:
        allocations = allocate(plant)
    arls = {(a.tag, a.feature, a.stream): a for a in allocations if a.unit == uid}
    fitted = baseline.data["units"].get(uid) if baseline is not None else None
    errors: list[tuple[str, str]] = []
    tags = {}
    for tid, tag in unit["tags"].items():
        features = {}
        for fname, feature in tag["features"].items():
            where = f"/units/{uid}/tags/{tid}/features/{fname}"
            key = f"{tid}.{fname}"
            got = fitted["features"].get(key) if fitted is not None else None
            if needs_baseline(feature) and got is None:
                errors.append((where, f"the baseline has no {key!r}; run sequentialist monitor calibrate"))
                continue
            model = _model(feature["model"], got)
            if model is None:
                assert got is not None
                errors.append((where, f"the baseline fitted {key!r} as {got['model']['type']!r} "
                                      f"(not {feature['model']['type']!r}); run calibrate again"))
                continue
            detector = {k: v for k, v in feature["detector"].items() if k not in ("direction", "alarm_probability")}
            if feature["detector"]["type"] in ("mean_e", "quantile_e"):
                detector["direction"] = feature["detector"]["direction"]
            streams = {}
            for stream in detector_streams(feature["detector"]):
                allocation = arls[(tid, fname, stream)]
                entry = {"arl": allocation.arl, "arl_from": "given" if allocation.given else "budget"}
                if feature["detector"]["type"] in BOUND:
                    entry.update(threshold=allocation.arl, threshold_from="bound")
                else:
                    assert got is not None
                    found = got["thresholds"].get(stream)
                    if found is None or not math.isclose(found["arl"], allocation.arl, rel_tol=1e-9):
                        errors.append((f"{where}/detector", f"the baseline has no threshold of stream {stream!r} "
                                                            f"for ARL {allocation.arl:.6g}; the budget changed "
                                                            f"since calibration: run calibrate again"))
                        continue
                    entry.update(threshold=found["threshold"], threshold_from="simulation")
                streams[stream] = entry
            features[fname] = {"priority": feature["priority"],
                               "guarantee": "none" if feature["model"]["type"] == "none" else "model",
                               "model": model, "detector": detector, "streams": streams}
        tags[tid] = {"name": tag["name"], "source_tag": tag["source_tag"] or tid,
                     "unit_of_measure": tag["unit_of_measure"], "range": tag["range"], "features": features}
    if errors:
        raise SpecError(errors)
    data = {
        "kind": UnitSpec.kind,
        "site": plant.data["site"], "area": unit["area"], "unit": uid, "name": unit["name"],
        "console": unit["console"], "window": plant.unit_window(uid), "lateness": plant.data["lateness"],
        "running": unit["running"],
        "reference": fitted["reference"] if fitted is not None else unit["reference"],
        "baseline": baseline.id if fitted is not None and baseline is not None else None,
        "alarms": plant.data["alarms"], "severity": plant.data["severity"], "tags": tags,
    }
    return parse_spec(data, UnitSpec)


def _model(given: dict[str, Any], fitted: dict[str, Any] | None) -> dict[str, Any] | None:
    if given["type"] == "kalman_level":
        return dict(given)
    assert fitted is not None
    model = fitted["model"]
    return model if model["type"] == given["type"] else None
