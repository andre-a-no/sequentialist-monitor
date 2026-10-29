# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Validation on open data: the Skoltech Anomaly Benchmark (SKAB), a water circulation testbed with a pump.

    python examples/skab.py [--files 34] [--cache DIR]

SKAB (https://github.com/waico/SKAB, GPL-3.0; Katser and Kozitsin, DOI 10.34740/KAGGLE/DSV/1693952) holds 34
experiments, each about 20 minutes of eight sensors read every second, with one fault: valves closed, the
pump's rotor unbalanced, the fluid's temperature or the motor's supply changed.  The data are downloaded,
not distributed with this package.

Protocol (SKAB's own): the first 400 seconds of every experiment are normal and are its reference period.
Each experiment is calibrated on them (window 1 s, an AR model of each sensor chosen by AIC, a two-sided
CUSUM with an ARL of one hour per stream, its threshold by simulation) and the rest is replayed through the
code the service runs.  Printed per experiment: alarms before the fault (false), and the delay from the
fault's start to the first alarm after it.  What it shows is what the pipeline does on real data whose
experiments are short and not stationary; it is not a guarantee (approximate, none).
"""
import argparse
import csv
import io
import statistics
import sys
import urllib.request
from pathlib import Path

from sequentialist.core import Record, SpecError, replay
from sequentialist.core.schema import parse_timestamp
from sequentialist.monitor.calibrate import calibrate
from sequentialist.monitor.config import load_plant
from sequentialist.monitor.unit import KIND, UnitHandler
from sequentialist.monitor.unitspec import build_unit_spec

BASE = "https://raw.githubusercontent.com/waico/SKAB/master/data/"
FILES = [f"valve1/{i}" for i in range(16)] + [f"valve2/{i}" for i in range(4)] + [f"other/{i}" for i in range(1, 15)]
REFERENCE = 400  # seconds: SKAB's training part
ARL = 3600  # windows of 1 s per detector stream


def fetch(name: str, cache: Path | None) -> str:
    if cache is not None and (cache / name.replace("/", "_")).exists():
        return (cache / name.replace("/", "_")).read_text(encoding="utf-8")
    with urllib.request.urlopen(BASE + name + ".csv", timeout=60) as response:
        text = response.read().decode("utf-8")
    if cache is not None:
        cache.mkdir(parents=True, exist_ok=True)
        (cache / name.replace("/", "_")).write_text(text, encoding="utf-8")
    return text


def tag_id(column: str) -> str:
    return "".join(c for c in column if c.isalnum())


def experiment(text: str, cache: dict):
    rows = list(csv.DictReader(io.StringIO(text), delimiter=";"))
    sensors = [c for c in rows[0] if c not in ("datetime", "anomaly", "changepoint")]
    times = [parse_timestamp(r["datetime"].replace(" ", "T") + "Z") for r in rows]
    start = times[0]
    fault = next((t for t, r in zip(times, rows) if float(r["anomaly"]) == 1.0), None)
    if fault is not None and fault < start + REFERENCE:
        return {"skipped": "the fault begins inside the first 400 s"}
    records = [Record("skab", i * len(sensors) + j, {"tag": s, "value": float(r[s]), "quality": "Good"}, t)
               for i, (t, r) in enumerate(zip(times, rows)) for j, s in enumerate(sensors)]
    plant = load_plant({
        "kind": "monitor.plant/1", "site": "skab", "storage": "memory:", "window": 1, "lateness": 0,
        "alarms": {"warmup": 10},
        "sources": {"skab": {"type": "csv", "directory": "."}},
        "units": {"pump": {"area": "testbed", "source": "skab",
                           "reference": {"from": start, "to": start + REFERENCE},
                           "tags": {tag_id(s): {"source_tag": s, "features": {"mean": {
                               "model": {"type": "ar"}, "detector": {"type": "cusum", "shift": 1.0}, "arl": ARL}}}
                               for s in sensors}}},
    })
    try:
        baseline = calibrate(plant, {"pump": records}, created=start, runs=200, min_windows=300, cache=cache)
    except SpecError as error:
        return {"skipped": error.errors[0][1]}
    spec = build_unit_spec(plant, baseline, "pump")
    test = [r for r in records if r.time >= start + REFERENCE]
    task = replay(KIND, spec.data, UnitHandler(spec.data), [*test, times[-1] + 1], task_id="skab")
    alarms = [e.basis["time"] for e in task.audit() if e.decision == "activated" and e.basis["condition"] == "mean"]
    task.close()
    false = [t for t in alarms if fault is None or t < fault]
    after = [t for t in alarms if fault is not None and t >= fault]
    normal = ((fault if fault is not None else times[-1]) - start - REFERENCE) / 3600
    warnings = sum(len(f["warnings"]) > 0 for f in baseline.data["units"]["pump"]["features"].values())
    return {"false": len(false), "normal_hours": normal, "delay": (after[0] - fault) if after else None,
            "streams": 2 * len(sensors), "warned": warnings, "features": len(sensors)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--files", type=int, default=len(FILES))
    parser.add_argument("--cache", type=Path, help="keep the downloaded files here")
    args = parser.parse_args()
    cache: dict = {}
    results = {}
    for name in FILES[: args.files]:
        r = experiment(fetch(name, args.cache), cache)
        if "skipped" in r:
            print(f"{name:<10} skipped: {r['skipped']}")
            continue
        results[name] = r
        delay = f"{r['delay']:.0f} s" if r["delay"] is not None else "not detected"
        print(f"{name:<10} false alarms {r['false']:>2} in {r['normal_hours'] * 60:4.1f} min of normal operation; "
              f"fault: {delay}; residual warnings on {r['warned']} of {r['features']} sensors")
    hours = sum(r["normal_hours"] for r in results.values())
    false = sum(r["false"] for r in results.values())
    streams = statistics.mean(r["streams"] for r in results.values())
    delays = [r["delay"] for r in results.values() if r["delay"] is not None]
    print()
    print(f"{len(results)} experiments: false alarms {false} in {hours:.2f} h of normal operation, "
          f"{false / hours:.1f} per hour; at ARL {ARL} windows of 1 s per stream the design rate is "
          f"{streams * 3600 / ARL:.0f} per hour ({streams:.0f} streams)")
    if delays:
        print(f"faults detected: {len(delays)} of {len(results)}; delay median {statistics.median(delays):.0f} s, "
              f"from {min(delays):.0f} to {max(delays):.0f} s")
    print("guarantee: none (an evaluation on data whose model is estimated on 400 s; not a test)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
