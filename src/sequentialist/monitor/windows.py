# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Readings into windows on the data's clock: the same code for a running unit and for its calibration.

A reading is ``{"tag": <name in the source>, "value": <number or null>,
"quality": "Good" | "Uncertain" | "Bad"}`` (OPC UA StatusCode severities)
with its time.  Windows are aligned to multiples of their length since the
epoch; window k holds the times [kW, (k+1)W) and closes once the clock has
reached (k+1)W + lateness, whether or not readings came.  A reading for a
closed window is late and dropped.  A reading is Good if its quality is
Good (or Uncertain, when the plant allows it), its value a finite number
and inside the tag's physical range.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

from .pipeline import Window

GOOD, UNCERTAIN, BAD = "Good", "Uncertain", "Bad"


class Windows:
    def __init__(self, spec: Mapping[str, Any]):
        """`spec`: window, lateness, running, tags (source_tag, range) and alarms.uncertain_is_bad of a unit."""
        self.window = float(spec["window"])
        self.lateness = float(spec["lateness"])
        self.rule = spec["running"]
        self.uncertain_is_bad = spec["alarms"]["uncertain_is_bad"]
        self.ranges = {tid: tag["range"] for tid, tag in spec["tags"].items()}
        self.by_source = {tag["source_tag"]: tid for tid, tag in spec["tags"].items()}
        self.next_window: int | None = None
        self.open: dict[str, dict[int, Window]] = {tid: {} for tid in spec["tags"]}

    def add(self, data: Mapping[str, Any], time: float | None) -> str | None:
        """Put a reading in its window; the reason it was dropped ("unknown_tag", "untimed", "late") or None."""
        tid = self.by_source.get(data.get("tag"))
        if tid is None:
            return "unknown_tag"
        if time is None:
            return "untimed"
        index = math.floor(time / self.window)
        if self.next_window is None:
            self.next_window = index
        elif index < self.next_window:
            return "late"
        window = self.open[tid].setdefault(index, Window())
        value = data.get("value")
        if self.good(tid, value, data.get("quality", GOOD)):
            window.add(float(value))  # type: ignore[arg-type]
        else:
            window.bad += 1
        return None

    def good(self, tid: str, value: Any, quality: Any) -> bool:
        if quality != GOOD and not (quality == UNCERTAIN and not self.uncertain_is_bad):
            return False
        if type(value) not in (int, float) or not math.isfinite(value):
            return False
        limits = self.ranges[tid]
        return limits is None or limits[0] <= value <= limits[1]

    def close(self, time: float) -> list[tuple[int, dict[str, Window]]]:
        """The windows the clock `time` closes, oldest first, every tag's window in each (empty if no reading)."""
        out = []
        if self.next_window is not None:
            while (self.next_window + 1) * self.window + self.lateness <= time:
                out.append((self.next_window, {tid: ws.pop(self.next_window, Window())
                                               for tid, ws in self.open.items()}))
                self.next_window += 1
        return out

    def end(self, index: int) -> float:
        return (index + 1) * self.window

    def running(self, windows: Mapping[str, Window], previous: bool | None) -> bool | None:
        """Whether the unit ran in this window; with no reading of the running tag, what was known before."""
        if self.rule is None:
            return True
        w = windows[self.rule["tag"]]
        if w.n == 0:
            return previous
        return w.feature("mean") > self.rule["above"]

    def state(self) -> dict[str, Any]:
        return {"next_window": self.next_window,
                "open": {tid: {str(i): w.to_json() for i, w in sorted(ws.items())} for tid, ws in self.open.items()}}

    def restore(self, state: Mapping[str, Any]) -> None:
        self.next_window = state["next_window"]
        self.open = {tid: {int(i): Window.from_json(w) for i, w in ws.items()} for tid, ws in state["open"].items()}
