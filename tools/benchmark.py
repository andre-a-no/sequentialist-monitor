# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""How fast the service keeps up: tags read every second, units of tags, a poll cycle of a few seconds.

    python tools/benchmark.py [--tags 2000] [--unit-size 50] [--minutes 10] [--cycle 5] [--storage sqlite]

Every tag has one feature (the window mean) with an AR(1) model and a two-sided CUSUM: three runs of
sequential-analysis per tag.  Each cycle ingests the readings of every unit in one batch and moves the
watermark, as `sequentialist monitor serve` does.  Prints the time per cycle against the cycle itself.

A checkpoint writes a unit's whole state (sequentialist-core 0.1), about a millisecond per run.  With
--checkpoint-interval (as the service does) units are checkpointed in turn, a few per cycle, so that each
is checkpointed once per interval; --checkpoint-every N instead lets the engine checkpoint every N records
of a unit, which checkpoints all units in the same cycle.
"""
import argparse
import platform
import random
import statistics
import sys
import tempfile
import time

from sequentialist.core import Engine, FileStorage, MemoryStorage, Record, SQLiteStorage
from sequentialist.monitor.config import load_baseline, load_plant
from sequentialist.monitor.unit import KIND, UnitHandler
from sequentialist.monitor.unitspec import build_unit_spec

T0 = 1793491200.0


def specs(tags, unit_size, window):
    units = {}
    for u in range(tags // unit_size):
        units[f"U{u:03d}"] = {"area": "a", "source": "s", "tags": {
            f"T{u:03d}-{i:02d}": {"features": {"mean": {"model": {"type": "ar", "order": 1},
                                                        "detector": {"type": "cusum"}}}}
            for i in range(unit_size)}}
    plant = load_plant({"kind": "monitor.plant/1", "site": "bench", "storage": "memory:", "window": window,
                        "sources": {"s": {"type": "sql", "module": "x", "dsn_env": "X", "query": "x"}},
                        "units": units})
    from sequentialist.monitor.budget import allocate

    allocations = allocate(plant)
    features = {}
    for a in allocations:
        key = f"{a.tag}.{a.feature}"
        f = features.setdefault(key, {"model": {"type": "ar", "intercept": 0.0, "coefficients": [0.5], "sigma": 1.0},
                                      "residuals": {"n": 1, "mean": 0, "sd": 1, "median": 0, "acf": []},
                                      "thresholds": {}})
        f["thresholds"][a.stream] = {"arl": a.arl, "threshold": 8.0, "runs": 1, "seed": 0, "arl_interval": [0, 0]}
    baseline = load_baseline({"kind": "monitor.baseline/1", "site": "bench", "plant_spec": plant.id, "created": T0,
                              "units": {uid: {"reference": {"from": 0, "to": 1}, "windows": 1, "features": {
                                  k: v for k, v in features.items() if k.startswith(f"T{uid[1:]}-")}}
                                  for uid in units}})
    return [build_unit_spec(plant, baseline, uid, allocations) for uid in sorted(units)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--tags", type=int, default=2000)
    parser.add_argument("--unit-size", type=int, default=50)
    parser.add_argument("--minutes", type=float, default=10)
    parser.add_argument("--cycle", type=float, default=5.0)
    parser.add_argument("--window", type=float, default=60.0)
    parser.add_argument("--storage", choices=["sqlite", "files", "memory"], default="sqlite")
    parser.add_argument("--checkpoint-every", type=int, default=None)
    parser.add_argument("--checkpoint-interval", type=float, default=300.0, help="seconds of data")
    args = parser.parse_args()

    unit_specs = specs(args.tags, args.unit_size, args.window)
    rng = random.Random(1)
    with tempfile.TemporaryDirectory() as d:
        storage = {"sqlite": lambda: SQLiteStorage(f"{d}/bench.db"), "files": lambda: FileStorage(d),
                   "memory": MemoryStorage}[args.storage]()
        engine = Engine(storage, clock="event", checkpoint_every=args.checkpoint_every or 10**9)
        per_cycle = 0 if args.checkpoint_every else -(-len(unit_specs) * args.cycle // args.checkpoint_interval)
        tasks = [engine.create(s.task_id(), KIND, s.data, UnitHandler(s.data)) for s in unit_specs]
        names = [sorted(s.data["tags"]) for s in unit_specs]
        levels = [{t: 0.0 for t in n} for n in names]
        seq = 0
        cycles = int(args.minutes * 60 / args.cycle)
        times = []
        for c in range(cycles):
            start = T0 + c * args.cycle
            batches = []
            for u in range(len(tasks)):
                batch = []
                for s in range(int(args.cycle)):
                    t = start + s
                    for tag in names[u]:
                        levels[u][tag] = 0.5 * levels[u][tag] + rng.gauss(0, 1)
                        batch.append(Record("s", seq, {"tag": tag, "value": levels[u][tag], "quality": "Good"}, t))
                        seq += 1
                batches.append(batch)
            began = time.perf_counter()
            for task, batch in zip(tasks, batches):
                task.ingest(batch)
                task.advance(start + args.cycle - 30)
            for i in range(int(per_cycle)):
                tasks[(c * int(per_cycle) + i) % len(tasks)].checkpoint()
            times.append(time.perf_counter() - began)
        alarms = sum(1 for task in tasks for e in task.audit() if e.decision == "activated")
        for task in tasks:
            task.close()
        storage.close()
    records = args.tags * args.cycle
    p95 = sorted(times)[int(0.95 * len(times))]
    print(f"python {platform.python_version()} on {platform.machine()} ({platform.processor() or 'cpu'}), "
          f"{args.storage} storage, " + (f"checkpoint every {args.checkpoint_every} records of a unit"
                                          if args.checkpoint_every else
                                          f"each unit checkpointed every {args.checkpoint_interval:g} s in turn"))
    print(f"{args.tags} tags in {len(tasks)} units, {args.minutes:g} min of data, {cycles} cycles of "
          f"{args.cycle:g} s ({records:.0f} records each), window {args.window:g} s")
    print(f"time per cycle: median {statistics.median(times):.3f} s, 95% {p95:.3f} s, max {max(times):.3f} s "
          f"({records / statistics.median(times):.0f} records/s); {alarms} alarms")
    return 0 if p95 <= args.cycle else 1


if __name__ == "__main__":
    sys.exit(main())
