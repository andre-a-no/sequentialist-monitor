# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""The plant's budget of false alarms, divided into the average run length of every detector.

A console may receive B alarms per hour in normal operation (ISA-18.2: about
6 per operator), a share s of them from statistical detectors.  A detector
restarts after each alarm, so in the long run it raises 1/ARL false alarms
per step (renewal theorem), and the expected number of false alarms of a
console is the sum over its detectors, whatever the correlation between
tags.  With D detectors stepping every W_i seconds and the budget divided
equally,

    ARL_i >= 3600 D / (B s W_i)  steps

keeps the expected false alarms of the console at most B s per hour when
every ARL bound holds.  This is a statement about the long-run mean, not
about every hour.  A detector with its own `arl` takes its rate out of the
budget first; the rest is shared equally by the others.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import PlantSpec

# directions watched by separate streams; e-detectors watch both sides in one stream
TWO_STREAMS = ("cusum", "sr")


def detector_streams(detector: dict[str, Any]) -> list[str]:
    """The detector streams of one feature, named ``<tag>.<feature>.<suffix>``: "up" and "down", or "detector"."""
    if detector["type"] in TWO_STREAMS and detector["direction"] == "both":
        return ["up", "down"]
    if detector["type"] in TWO_STREAMS:
        return [detector["direction"]]
    return ["detector"]


@dataclass(frozen=True)
class Allocation:
    unit: str
    tag: str
    feature: str
    stream: str  # "detector", "up" or "down"
    console: str
    window: float  # seconds per step
    arl: float  # steps
    given: bool  # the feature set its own arl

    @property
    def per_hour(self) -> float:
        """Expected false alarms per hour when ARL holds with equality."""
        return 3600.0 / (self.window * self.arl)


def allocate(plant: PlantSpec) -> list[Allocation]:
    """The ARL of every detector stream of the plant; ValueError if fixed ARLs exceed a console's budget."""
    budget = plant.data["budget"]
    per_console = budget["alarms_per_hour"] * budget["detector_share"]
    slots: dict[str, list[tuple[str, str, str, str, float, Any]]] = {}
    for uid, unit in sorted(plant.data["units"].items()):
        window = plant.unit_window(uid)
        for tid, tag in sorted(unit["tags"].items()):
            for fname, feature in sorted(tag["features"].items()):
                for stream in detector_streams(feature["detector"]):
                    slots.setdefault(unit["console"], []).append((uid, tid, fname, stream, window, feature["arl"]))
    out: list[Allocation] = []
    for console, items in sorted(slots.items()):
        fixed = sum(3600.0 / (w * arl) for *_, w, arl in items if arl is not None)
        free = [item for item in items if item[5] is None]
        left = per_console - fixed
        if free and left <= 0:
            raise ValueError(f"console {console!r}: detectors with their own arl expect {fixed:.3g} false alarms "
                             f"per hour, the budget is {per_console:.3g}")
        for uid, tid, fname, stream, w, own in items:
            arl = own if own is not None else 3600.0 * len(free) / (left * w)
            out.append(Allocation(uid, tid, fname, stream, console, w, float(arl), own is not None))
    return out
