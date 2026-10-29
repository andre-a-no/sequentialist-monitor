# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""`sequentialist monitor ...`: the subcommands of sequentialist-monitor.

Registered with the `sequentialist` command of sequentialist-core through the
entry point group ``sequentialist.commands``.

    sequentialist monitor check plant.yaml [--baseline baseline.json]
    sequentialist monitor schema {plant,baseline,unit}
    sequentialist monitor budget plant.yaml [--baseline baseline.json]
    sequentialist monitor calibrate plant.yaml -o baseline.json [--units U ...] [--previous baseline.json]
    sequentialist monitor replay plant.yaml --baseline baseline.json --from T --to T [-o report.md]
    sequentialist monitor serve plant.yaml [--baseline baseline.json] [--shard i/n]
    sequentialist monitor report plant.yaml --from T --to T [--baseline baseline.json] [--format md|json]
    sequentialist monitor command plant.yaml UNIT COMMAND --source S --actor A --reason R [--duration D]
    sequentialist monitor record plant.yaml SOURCE -o DIR [--for SECONDS]

Times are RFC 3339 with a time zone (2026-11-01T00:00:00Z) or seconds since the epoch.
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


def _moment(text: str) -> float:
    from sequentialist.core.schema import parse_timestamp

    try:
        return float(text)
    except ValueError:
        return parse_timestamp(text)


def _history(plant: Any, uid: str, start: float, end: float) -> list[Any]:
    from .sources import open_source

    name = plant.data["units"][uid]["source"]
    source = open_source(name, plant.data["sources"][name], plant.data)
    try:
        return source.history(start, end)
    finally:
        close = getattr(source, "close", None)
        if close is not None:
            close()


def _calibrate(sub: Any) -> None:
    parser = sub.add_parser("calibrate", help="fit the units' models on their reference periods, set thresholds")
    parser.add_argument("plant")
    parser.add_argument("-o", "--output", required=True, help="the baseline file to write (JSON)")
    parser.add_argument("--units", nargs="*", help="only these units (the others are kept from --previous)")
    parser.add_argument("--previous", help="a baseline whose other units are kept")
    parser.add_argument("--runs", type=int, default=400, help="simulated runs per CUSUM threshold")
    parser.add_argument("--bocpd-runs", type=int, default=50, help="simulated runs per BOCPD threshold")
    parser.add_argument("--seed", type=int, default=0)

    def handler(args: argparse.Namespace) -> int:
        import time

        from .calibrate import calibrate

        plant = load_plant(args.plant)
        units = args.units or [u for u, d in sorted(plant.data["units"].items()) if d["reference"] is not None]
        histories = {}
        for uid in units:
            ref = plant.data["units"][uid]["reference"]
            if ref is None:
                raise ValueError(f"unit {uid} has no reference period")
            print(f"{uid}: reading {ref['from']:.0f} to {ref['to']:.0f}", file=sys.stderr)
            histories[uid] = _history(plant, uid, ref["from"], ref["to"])
        previous = load_baseline(args.previous) if args.previous else None
        baseline = calibrate(plant, histories, created=time.time(), runs=args.runs, bocpd_runs=args.bocpd_runs,
                             seed=args.seed, previous=previous, progress=lambda m: print(m, file=sys.stderr))
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(baseline.to_json(), f, indent=2, ensure_ascii=False)
            f.write("\n")
        for uid in units:
            for key, feature in baseline.data["units"][uid]["features"].items():
                for warning in feature["warnings"]:
                    print(f"warning: {uid} {key}: {warning}", file=sys.stderr)
        print(f"{args.output}: baseline {baseline.id[:16]} for {len(units)} units")
        return 0

    parser.set_defaults(handler=handler)


def _write_report(report: Any, fmt: str, output: str | None) -> None:
    text = report.to_markdown() if fmt == "md" else json.dumps(report.to_json(), indent=2, ensure_ascii=False)
    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)


def _replay(sub: Any) -> None:
    parser = sub.add_parser("replay", help="run recorded history through the live code, in memory, and report")
    parser.add_argument("plant")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--from", dest="start", required=True, type=_moment)
    parser.add_argument("--to", dest="end", required=True, type=_moment)
    parser.add_argument("--units", nargs="*")
    parser.add_argument("--format", choices=["md", "json"], default="md")
    parser.add_argument("-o", "--output")

    def handler(args: argparse.Namespace) -> int:
        from sequentialist.core import replay

        from .report import plant_report
        from .unit import KIND, UnitHandler
        from .unitspec import build_unit_spec

        plant, baseline = load_plant(args.plant), load_baseline(args.baseline)
        allocations = allocate(plant)
        entries = []
        for uid in args.units or sorted(plant.data["units"]):
            spec = build_unit_spec(plant, baseline, uid, allocations)
            history = _history(plant, uid, args.start, args.end)
            task = replay(KIND, spec.data, UnitHandler(spec.data), [*history, args.end],
                          task_id=spec.task_id())
            entries += task.audit()
            task.close()
        _write_report(plant_report(plant, baseline, entries, args.start, args.end), args.format, args.output)
        return 0

    parser.set_defaults(handler=handler)


def _report(sub: Any) -> None:
    parser = sub.add_parser("report", help="report a period from the service's storage")
    parser.add_argument("plant")
    parser.add_argument("--from", dest="start", required=True, type=_moment)
    parser.add_argument("--to", dest="end", required=True, type=_moment)
    parser.add_argument("--baseline")
    parser.add_argument("--format", choices=["md", "json"], default="md")
    parser.add_argument("-o", "--output")

    def handler(args: argparse.Namespace) -> int:
        from sequentialist.core import open_storage

        from .report import entries_of, plant_report

        plant = load_plant(args.plant)
        baseline = load_baseline(args.baseline) if args.baseline else None
        with open_storage(plant.data["storage"]) as storage:
            entries = entries_of(storage, plant)
        _write_report(plant_report(plant, baseline, entries, args.start, args.end), args.format, args.output)
        return 0

    parser.set_defaults(handler=handler)


def _command(sub: Any) -> None:
    parser = sub.add_parser("command", help="act on an alarm while the service is not running (it holds the lock)")
    parser.add_argument("plant")
    parser.add_argument("unit")
    parser.add_argument("name", choices=["ack", "shelve", "unshelve", "remove_from_service", "return_to_service"])
    parser.add_argument("--source", required=True, help="tag.feature, tag.quality, @source, a tag, or * (ack)")
    parser.add_argument("--duration", help="for shelve: seconds, or oneshot")
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--baseline")

    def handler(args: argparse.Namespace) -> int:
        import os

        from sequentialist.core import Engine, open_storage, pseudonymize

        from .unit import UnitHandler
        from .unitspec import build_unit_spec

        plant = load_plant(args.plant)
        baseline = load_baseline(args.baseline) if args.baseline else None
        spec = build_unit_spec(plant, baseline, args.unit)
        command_args: dict[str, Any] = {"source": args.source}
        if args.duration is not None:
            command_args["duration"] = args.duration if args.duration == "oneshot" else float(args.duration)
        actor = args.actor
        if plant.data["actor_key_env"]:
            actor = pseudonymize(actor, os.environ[plant.data["actor_key_env"]])
        with open_storage(plant.data["storage"]) as storage:
            task = Engine(storage).open(spec.task_id(), UnitHandler)
            try:
                refusal = task.handler.check(args.name, command_args)  # type: ignore[attr-defined]
                if refusal is not None:
                    print(f"refused: {refusal}", file=sys.stderr)
                    return 1
                result = task.command(args.name, command_args, actor=actor, reason=args.reason)
            finally:
                task.close()
        for e in result.entries:
            print(f"{e.n}  {e.decision}  {e.basis.get('source')}  {e.basis.get('state')}")
        return 0

    parser.set_defaults(handler=handler)


def _serve(sub: Any) -> None:
    parser = sub.add_parser("serve", help="run the service and its HTTP API")
    parser.add_argument("plant")
    parser.add_argument("--baseline")
    parser.add_argument("--shard", default="0/1", help="i/n: this process serves shard i of n")
    parser.add_argument("--checkpoint-interval", type=float, default=300.0)

    def handler(args: argparse.Namespace) -> int:
        import os
        import signal

        from .api import Api
        from .service import Service

        plant = load_plant(args.plant)
        baseline = load_baseline(args.baseline) if args.baseline else None
        index, count = (int(x) for x in args.shard.split("/"))
        service = Service(plant, baseline, shard=(index, count), checkpoint_interval=args.checkpoint_interval)
        http = plant.data["http"]
        token = os.environ.get(http["token_env"])
        if not token:
            print(f"warning: {http['token_env']} is not set: the API refuses every command", file=sys.stderr)
        api = Api(service, http["host"], http["port"] + index, token)
        api.start()

        def stop(*_: Any) -> None:
            service.stop()

        signal.signal(signal.SIGINT, stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, stop)
        host, port = api.address
        print(f"sequentialist-monitor: {len(service.tasks)} units, API on http://{host}:{port} "
              f"(an advisory system, not a safety instrumented system)", file=sys.stderr)
        try:
            service.run()
        finally:
            api.close()
            service.close()
        return 0

    parser.set_defaults(handler=handler)


def _record(sub: Any) -> None:
    parser = sub.add_parser("record", help="read a Modbus source every period into CSV files (a reference "
                                           "period for calibration: a device keeps no history)")
    parser.add_argument("plant")
    parser.add_argument("source")
    parser.add_argument("-o", "--output", required=True, help="directory of the CSV files, one per UTC day")
    parser.add_argument("--for", dest="duration", type=float, default=None,
                        help="seconds to record; without it, until interrupted")

    def handler(args: argparse.Namespace) -> int:
        import signal
        import threading

        from .sources import ModbusSource, Recorder, record

        plant = load_plant(args.plant)
        config = plant.data["sources"].get(args.source)
        if config is None or config["type"] != "modbus":
            print(f"error: {args.source!r} is not a Modbus source of the plant", file=sys.stderr)
            return 2
        source = ModbusSource(args.source, config, {})
        stopped = threading.Event()

        def stop(*_: Any) -> None:
            stopped.set()

        signal.signal(signal.SIGINT, stop)
        until = source.clock() + args.duration if args.duration is not None else None
        recorder = Recorder(args.output, args.source)
        try:
            polls = record(source, recorder, until=until, stop=stopped.is_set, sleep=stopped.wait)
        finally:
            source.close()
        print(f"{polls} polls, {recorder.rows} readings in {args.output}", file=sys.stderr)
        return 0

    parser.set_defaults(handler=handler)


# each adds its parser to the subparsers of `sequentialist monitor`
_COMMANDS: list[Callable[[Any], None]] = [_check, _schema, _budget, _calibrate, _replay, _serve, _report, _command,
                                          _record]


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
