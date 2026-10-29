# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""A synthetic pump, end to end: a historian in SQLite, calibration, a replay of history with two faults.

    python examples/synthetic_plant.py [--report report.md]

The pump has a bearing temperature (window-level AR(1)) and a vibration (a slowly wandering level).  Ten
days are normal; then, on the eleventh, the temperature steps up by 2 sigma of its innovations and later the
vibration starts to drift.  The script calibrates on the normal days, replays the eleventh through the code
the service runs, and prints when each fault was detected and how many alarms came before any fault.

With three detectors the plant's budget (3 statistical alarms per hour) would give each an ARL of one
hour; a plant with a few hundred detectors gives each tens of hours.  Here every detector gets 6000
windows (100 hours) as its own `arl`, as it would in such a plant.
"""
import argparse
import json
import math
import os
import random
import sqlite3
import sys
import tempfile
from pathlib import Path

from sequentialist.core.cli import main as sequentialist

T0 = 1793491200.0  # 2026-11-01T00:00:00Z
DAY = 86400
WINDOW = 60
STEP_AT = 10 * DAY + 6 * 3600  # the temperature steps up
DRIFT_AT = 10 * DAY + 14 * 3600  # the vibration starts to drift


def history(db: sqlite3.Connection, seed: int = 1) -> None:
    rng = random.Random(seed)
    temp, level, rows = 60.0, 2.0, []
    for k in range(11 * DAY // WINDOW):
        t = k * WINDOW
        temp = 60.0 + 0.7 * (temp - 60.0) + rng.gauss(0, 0.5)
        level += rng.gauss(0, 0.005)
        step = 1.0 if t >= STEP_AT else 0.0  # 2 innovation sigmas
        drift = 0.0005 * (t - DRIFT_AT) / WINDOW if t >= DRIFT_AT else 0.0
        for j in range(6):  # a reading every 10 s
            when = T0 + t + 10 * j
            rows.append(("TI-101", when, temp + step + rng.gauss(0, 0.2), 192))
            rows.append(("VI-101", when, level + drift + rng.gauss(0, 0.05), 192))
            rows.append(("II-101", when, 20.0 + rng.gauss(0, 0.1), 192))
    db.executemany("INSERT INTO samples VALUES (?, ?, ?, ?)", rows)
    db.commit()


def plant(directory: Path) -> dict:
    return {
        "kind": "monitor.plant/1", "site": "demo", "storage": f"sqlite:{directory / 'monitor.db'}",
        "window": WINDOW, "lateness": 30,
        "sources": {"historian": {"type": "sql", "module": "sqlite3", "dsn_env": "DEMO_HISTORIAN",
                                  "query": "SELECT tag, ts, value, q FROM samples WHERE ts >= ? AND ts < ?",
                                  "time_param": "epoch"}},
        "units": {"P-101": {
            "area": "pumping", "name": "Pump P-101", "source": "historian",
            "running": {"tag": "II-101", "above": 5.0},
            "reference": {"from": T0, "to": T0 + 10 * DAY},
            "tags": {
                "II-101": {"unit_of_measure": "AMP"},
                "TI-101": {"name": "Bearing temperature", "unit_of_measure": "CEL", "features": {
                    "mean": {"model": {"type": "ar"}, "detector": {"type": "cusum", "shift": 1.0},
                             "priority": "high", "arl": 6000}}},
                "VI-101": {"name": "Vibration", "unit_of_measure": "MMS", "features": {
                    "mean": {"model": {"type": "level"}, "detector": {"type": "mean_e", "direction": "up"},
                             "arl": 6000}}},
            }}},
    }


def run(*argv: str) -> None:
    code = sequentialist(["monitor", *argv])
    if code:
        sys.exit(code)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--report", help="write the replay's report (Markdown) here")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as d:
        directory = Path(d)
        db = sqlite3.connect(directory / "historian.db")
        db.execute("CREATE TABLE samples (tag TEXT, ts REAL, value REAL, q INTEGER)")
        history(db)
        db.close()
        os.environ["DEMO_HISTORIAN"] = str(directory / "historian.db")
        (directory / "plant.json").write_text(json.dumps(plant(directory)), encoding="utf-8")
        p, b = str(directory / "plant.json"), str(directory / "baseline.json")
        run("calibrate", p, "-o", b, "--runs", "300")
        run("budget", p, "--baseline", b)
        report = args.report or str(directory / "report.json")
        start, end = T0 + 10 * DAY, T0 + 11 * DAY
        run("replay", p, "--baseline", b, "--from", str(start), "--to", str(end), "--format", "json",
            "-o", str(directory / "report.json"))
        found = json.loads((directory / "report.json").read_text(encoding="utf-8"))
        if args.report:
            run("replay", p, "--baseline", b, "--from", str(start), "--to", str(end), "-o", report)
    rows = next(s for s in found["sections"] if s["title"] == "Alarms")["tables"][0]["rows"]
    print()
    print(f"{len(rows)} alarms on the eleventh day:")
    for time, source, condition, priority, announced, guarantee in rows:
        print(f"  {time}  {source} {condition}  {priority}  announced: {announced}  guarantee: {guarantee}")

    def first(tag: str, after: float) -> str:
        from sequentialist.core.schema import parse_timestamp

        times = [parse_timestamp(r[0]) - T0 for r in rows if r[1].endswith(tag) and parse_timestamp(r[0]) - T0
                 >= after]
        return f"{(min(times) - after) / 60:.0f} min after it began" if times else "not detected"

    before = [r for r in rows if r[1].endswith(("TI-101", "VI-101")) and r[0] < "2026-11-11T06:00:00Z"]
    again = [r for r in rows if r[1].endswith("TI-101") and r[0] >= "2026-11-11T06:00:00Z"]
    print(f"temperature step: {first('TI-101', STEP_AT)}; vibration drift: {first('VI-101', DRIFT_AT)}; "
          f"{len(before)} alarms in the 6 normal hours before the step")
    print(f"the temperature alarm became active {len(again)} times while the step lasted: a step smaller than the "
          f"design shift leaves a residual that refires the CUSUM less often than the off-delay of 30 windows, "
          f"so the alarm returns and activates again (a limit of 0.1, see README)")
    return 0 if math.isfinite(len(rows)) else 1


if __name__ == "__main__":
    sys.exit(main())
