# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""`sequentialist monitor ...`: the subcommands of sequentialist-monitor.

Registered with the `sequentialist` command of sequentialist-core through the
entry point group ``sequentialist.commands``.

    sequentialist monitor check plant.yaml [--baseline baseline.json]
    sequentialist monitor schema {plant,baseline,unit}
    sequentialist monitor budget plant.yaml [--baseline baseline.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable

from sequentialist.core import json_schema

from .budget import allocate
from .config import BaselineSpec, PlantSpec, UnitSpec, load_baseline, load_plant
from .version import __version__


def _check(sub: Any) -> None:
    parser = sub.add_parser("check", help="check a plant (and its baseline): every problem at once")
    parser.add_argument("plant")
    parser.add_argument("--baseline", help="also build the spec of every unit from this baseline")

    def handler(args: argparse.Namespace) -> int:
        from .unitspec import build_unit_spec

        plant = load_plant(args.plant)
        allocations = allocate(plant)
        units = plant.data["units"]
        print(f"{args.plant}: plant {plant.data['site']}, {len(units)} units, "
              f"{sum(len(u['tags']) for u in units.values())} tags, {len(allocations)} detector streams; "
              f"spec {plant.id[:16]}")
        if args.baseline:
            baseline = load_baseline(args.baseline)
            for uid in sorted(units):
                spec = build_unit_spec(plant, baseline, uid, allocations)
                print(f"  {spec.task_id()}")
        return 0

    parser.set_defaults(handler=handler)


def _schema(sub: Any) -> None:
    parser = sub.add_parser("schema", help="print the JSON Schema of a format")
    parser.add_argument("format", choices=["plant", "baseline", "unit"])

    def handler(args: argparse.Namespace) -> int:
        cls = {"plant": PlantSpec, "baseline": BaselineSpec, "unit": UnitSpec}[args.format]
        json.dump(json_schema(cls), sys.stdout, indent=2, ensure_ascii=False)
        print()
        return 0

    parser.set_defaults(handler=handler)


def _budget(sub: Any) -> None:
    parser = sub.add_parser("budget", help="the plant's budget of false alarms divided among the detectors")
    parser.add_argument("plant")
    parser.add_argument("--baseline", help="also show the thresholds of the calibrated units")

    def handler(args: argparse.Namespace) -> int:
        plant = load_plant(args.plant)
        budget = plant.data["budget"]
        allocations = allocate(plant)
        print(f"budget: {budget['alarms_per_hour']:g} alarms per hour per console, "
              f"{budget['detector_share']:g} of them for detectors")
        print("expected false alarms per hour, at most, when every ARL bound holds (a long-run mean, not a "
              "promise for every hour)")
        thresholds: dict[tuple[str, str, str, str], tuple[float, str]] = {}
        if args.baseline:
            from .unitspec import build_unit_spec

            baseline = load_baseline(args.baseline)
            for uid in sorted(baseline.data["units"]):
                tags = build_unit_spec(plant, baseline, uid, allocations).data["tags"]
                for tid, tag in tags.items():
                    for fname, feature in tag["features"].items():
                        for stream, entry in feature["streams"].items():
                            thresholds[(uid, tid, fname, stream)] = (entry["threshold"], entry["threshold_from"])
        print(f"{'console':<10} {'unit':<12} {'feature':<24} {'stream':<9} {'window':>7} {'ARL':>10} "
              f"{'hours':>8} {'per h':>7}  threshold")
        for a in allocations:
            found = thresholds.get((a.unit, a.tag, a.feature, a.stream))
            shown = f"{found[0]:.6g} ({found[1]})" if found else "—"
            print(f"{a.console:<10} {a.unit:<12} {a.tag + '.' + a.feature:<24} {a.stream:<9} {a.window:>6g}s "
                  f"{a.arl:>10.6g} {a.arl * a.window / 3600:>8.4g} {a.per_hour:>7.3g}  {shown}"
                  f"{'  (arl given)' if a.given else ''}")
        return 0

    parser.set_defaults(handler=handler)


# each adds its parser to the subparsers of `sequentialist monitor`
_COMMANDS: list[Callable[[Any], None]] = [_check, _schema, _budget]


def register(commands: Any) -> None:
    parser = commands.add_parser("monitor", help="monitoring of equipment, processes and energy",
                                 description=f"sequentialist-monitor {__version__}: an advisory system, "
                                             f"not a safety instrumented system (no claim under IEC 61511).")
    sub = parser.add_subparsers(dest="monitor_command")
    for add in _COMMANDS:
        add(sub)

    def handler(args: argparse.Namespace) -> int:
        parser.print_help()
        return 2

    parser.set_defaults(handler=handler)
