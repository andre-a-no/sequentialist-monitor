# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""The task of one unit of equipment: its tags' windows, models, detectors and alarms.

A record is one reading: ``{"tag": <name in the source>, "value": <number or
null>, "quality": "Good" | "Uncertain" | "Bad"}`` with its time in the
source.  Readings go into windows of the unit's length, on the data's
clock; a window closes when the watermark has passed its end by
`lateness`, whether or not readings arrived (`on_time`).  Then, while the
unit runs, every feature of every tag with enough Good readings goes
through its model and detector streams (`pipeline`), and the alarms
(`alarms`) change; every change is an audit entry whose basis carries the
fields of an OPC UA Alarms & Conditions event.

The handler is deterministic and keeps all its state as plain JSON: a
replay of the journal, or of recorded history, gives the same audit log.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

from sequentialist.core import Command, Context, Handler, Record, Stream

from .alarms import COMMANDS, Alarm, Settings, Transition
from .pipeline import (
    Window,
    detector_input,
    detector_procedure,
    model_procedure,
    next_scale,
    residual,
    weakest,
)
from .windows import Windows

KIND = "monitor.unit/1"
QUALITY = "quality"  # the condition of a tag's own signal: too few Good readings
QUALITY_PRIORITY = "medium"


class UnitHandler(Handler):
    commands = COMMANDS

    def __init__(self, spec: Mapping[str, Any]):
        self.spec = dict(spec)
        self.windows = Windows(spec)
        self.settings = Settings.of(spec["alarms"])
        self.quality_settings = replace(self.settings, clear_after=1)
        self.watched = sorted(tid for tid, tag in spec["tags"].items() if tag["features"])
        self.features = [(tid, fname) for tid in sorted(spec["tags"])
                         for fname in sorted(spec["tags"][tid]["features"])]
        # state (and that of the windows)
        self.running: bool | None = None if spec["running"] else True
        self.fed: dict[str, dict[str, Any]] = {f"{t}.{f}": {"scale": None, "seen": 0} for t, f in self.features}
        self.bad: dict[str, int] = {tid: 0 for tid in self.watched}
        self.alarms: dict[str, Alarm] = {}
        for tid, fname in self.features:
            self.alarms[f"{tid}.{fname}"] = Alarm(f"{tid}.{fname}", spec["tags"][tid]["features"][fname]["priority"])
        for tid in self.watched:
            self.alarms[f"{tid}.{QUALITY}"] = Alarm(f"{tid}.{QUALITY}", QUALITY_PRIORITY)
        self.cursors: dict[str, dict[str, Any]] = {}  # source -> {"position", "next"}: see sources
        self.counters = {"late": 0, "unknown_tag": 0, "untimed": 0}

    # ------------------------------------------------------------------ streams and state

    def feature(self, tid: str, fname: str) -> dict[str, Any]:
        return self.spec["tags"][tid]["features"][fname]

    def streams(self) -> dict[str, Stream]:
        out = {}
        for tid, fname in self.features:
            f = self.feature(tid, fname)
            model = model_procedure(f["model"])
            if model is not None:
                out[f"{tid}.{fname}.model"] = Stream(model)
            for stream, entry in f["streams"].items():
                out[f"{tid}.{fname}.{stream}"] = Stream(detector_procedure(f["detector"], stream, entry["threshold"]))
        return out

    def state(self) -> Any:
        return {
            "windows": self.windows.state(),
            "running": self.running,
            "fed": self.fed,
            "bad": self.bad,
            "alarms": {k: a.to_json() for k, a in sorted(self.alarms.items())},
            "cursors": self.cursors,
            "counters": self.counters,
        }

    def restore(self, state: Any) -> None:
        self.windows.restore(state["windows"])
        self.running = state["running"]
        self.fed, self.bad, self.counters = state["fed"], state["bad"], state["counters"]
        self.alarms = {k: Alarm.from_json(a) for k, a in state["alarms"].items()}
        self.cursors = state["cursors"]

    # ------------------------------------------------------------------ inputs

    def on_record(self, record: Record, ctx: Context) -> None:
        if "cursor" in record.data:  # where the source has been read up to for this unit
            self.cursors[record.source] = {"position": record.data["cursor"], "next": record.seq + 1}
            return
        dropped = self.windows.add(record.data, record.time)
        if dropped is not None:
            self.counters[dropped] += 1

    def on_time(self, time: float, ctx: Context) -> None:
        for index, windows in self.windows.close(time):
            self._close(self.windows.end(index), windows, ctx)
        for alarm in self.alarms.values():
            for transition in alarm.tick(time, self.settings):
                self._audit(ctx, alarm, transition)

    # ------------------------------------------------------------------ a window closes

    def _close(self, end: float, windows: dict[str, Window], ctx: Context) -> None:
        running = self.windows.running(windows, self.running)
        if running != self.running:
            self._set_running(running, end, ctx)
        if not self.running:
            return
        for tid in self.watched:
            w = windows[tid]
            quality = self.alarms[f"{tid}.{QUALITY}"]
            if quality.out_of_service:
                continue
            if w.n == 0 or w.good_share() < self.spec["alarms"]["min_good"]:
                self.bad[tid] += 1
                if self.bad[tid] == self.spec["alarms"]["bad_windows"]:
                    self._audit(ctx, quality, quality.fire(end, self.quality_settings),
                                {"bad_windows": self.bad[tid], "good_share": w.good_share()})
                continue
            self.bad[tid] = 0
            transition = quality.quiet_window(end, self.quality_settings)
            if transition is not None:
                self._audit(ctx, quality, transition)
            for fname in sorted(self.spec["tags"][tid]["features"]):
                if not self.alarms[f"{tid}.{fname}"].out_of_service:
                    self._feed(tid, fname, w.feature(fname), end, ctx)

    def _set_running(self, running: bool | None, time: float, ctx: Context) -> None:
        was = self.running
        self.running = running
        if was and not running:  # a stopped unit is not in normal operation: start the models afresh
            for tid, fname in self.features:
                self._restart_feature(tid, fname, ctx, "unit stopped")
        for alarm in self.alarms.values():
            transition = alarm.set_suppressed(not running, time)
            if transition is not None:
                self._audit(ctx, alarm, transition)

    def _restart_feature(self, tid: str, fname: str, ctx: Context, reason: str) -> None:
        key = f"{tid}.{fname}"
        f = self.feature(tid, fname)
        if f["model"]["type"] != "none" and ctx.run(f"{key}.model").t:
            ctx.restart(f"{key}.model", reason)
        for stream in f["streams"]:
            if ctx.run(f"{key}.{stream}").t:
                ctx.restart(f"{key}.{stream}", reason)
        self.fed[key] = {"scale": None, "seen": 0}

    def _feed(self, tid: str, fname: str, value: float, time: float, ctx: Context) -> None:
        key = f"{tid}.{fname}"
        f = self.feature(tid, fname)
        fed = self.fed[key]
        output = None
        if f["model"]["type"] != "none":
            event = ctx.step(f"{key}.model", {"y": value})
            if event is None:  # an input the model refused (logged by the run)
                return
            output = event.output
        z = residual(f["model"], output, fed["scale"], value)
        fed["scale"] = next_scale(f["model"], output) if output is not None else None
        fed["seen"] += 1
        if z is None or fed["seen"] <= self.spec["alarms"]["warmup"]:
            return
        x = detector_input(f["detector"], z)
        fired = []
        for stream in sorted(f["streams"]):
            event = ctx.step(f"{key}.{stream}", x)
            if event is not None and event.terminal:
                fired.append(stream)
        alarm = self.alarms[key]
        if not fired:
            transition = alarm.quiet_window(time, self.settings)
            if transition is not None:
                self._audit(ctx, alarm, transition)
            return
        evidence = ([f"{key}.model"] if f["model"]["type"] != "none" else []) + [f"{key}.{s}" for s in fired]
        extra = {"value": value, "residual": z, "streams": fired,
                 "thresholds": {s: f["streams"][s]["threshold"] for s in fired},
                 "arl": {s: f["streams"][s]["arl"] for s in fired},
                 "guarantee": weakest(f["guarantee"], *(("approximate",) if any(
                     f["streams"][s]["threshold_from"] == "simulation" for s in fired) else ()))}
        self._audit(ctx, alarm, alarm.fire(time, self.settings), extra, evidence)
        for stream in fired:
            ctx.restart(f"{key}.{stream}", "alarm")

    # ------------------------------------------------------------------ commands of people

    def _targets(self, args: Mapping[str, Any]) -> list[Alarm]:
        source = args.get("source")
        if source == "*":
            return [a for _, a in sorted(self.alarms.items())]
        if source in self.alarms:
            return [self.alarms[source]]
        if source in self.spec["tags"]:  # a whole tag
            return [a for k, a in sorted(self.alarms.items()) if k.split(".")[0] == source]
        return []

    def check(self, name: str, args: Mapping[str, Any]) -> str | None:
        """Why a command would be refused now, or None: the service asks before it journals a command."""
        if name not in COMMANDS:
            return f"unknown command {name!r}; this task accepts {list(COMMANDS)}"
        targets = self._targets(args)
        if not targets:
            return f"no alarm source {args.get('source')!r}"
        if args.get("source") == "*":
            if name != "ack":
                return "only ack applies to every source at once"
            return None if any(a.refusal(name, dict(args), self.settings) is None for a in targets) else \
                "nothing to acknowledge"
        refusals = [a.refusal(name, dict(args), self.settings) for a in targets]
        if all(r is not None for r in refusals):
            return refusals[0]
        return None

    def on_command(self, command: Command, ctx: Context) -> None:
        refused = self.check(command.name, command.args)
        time = ctx.now if ctx.now is not None else 0.0
        if refused is not None:  # never raise: a failing command would fail again at every replay
            ctx.record("action", "refused", basis={"command": command.name, "args": command.args,
                                                   "refusal": refused}, actor=command.actor, reason=command.reason)
            return
        for alarm in self._targets(command.args):
            if alarm.refusal(command.name, dict(command.args), self.settings) is not None:
                continue
            transition = alarm.command(command.name, dict(command.args), time, self.settings)
            if command.name == "return_to_service" and not alarm.source.endswith(f".{QUALITY}"):
                tid, fname = alarm.source.split(".")
                self._restart_feature(tid, fname, ctx, "returned to service")
            self._audit(ctx, alarm, transition, {"command": command.name, "args": command.args},
                        actor=command.actor, reason=command.reason)

    # ------------------------------------------------------------------ the audit log

    def event(self, alarm: Alarm, transition: Transition) -> dict[str, Any]:
        """The fields of an OPC UA Alarms & Conditions event for this transition."""
        s = self.spec
        tid, condition = alarm.source.split(".")
        tag = s["tags"][tid]
        return {
            "source": alarm.source,
            "source_name": f"{s['site']}.{s['area']}.{s['unit']}.{tid}",
            "condition": condition,
            "console": s["console"],
            "priority": alarm.priority,
            "severity": s["severity"][alarm.priority],
            "transition": transition.event,
            "state": {"before": transition.before, "after": transition.after},
            "opc_ua": alarm.opc_ua(),
            "notify": transition.notify,
            "chattering": alarm.chattering,
            "time": transition.time,
            "message": message(s, tag, tid, condition, transition),
        }

    def _audit(self, ctx: Context, alarm: Alarm, transition: Transition, extra: Mapping[str, Any] | None = None,
               evidence: list[str] | None = None, actor: str | None = None, reason: str | None = None) -> None:
        basis = self.event(alarm, transition)
        if extra:
            basis.update(extra)
        ctx.record("action" if actor else "decision", transition.event, basis=basis, evidence=evidence or [],
                   actor=actor, reason=reason)


def message(spec: Mapping[str, Any], tag: Mapping[str, Any], tid: str, condition: str, t: Transition) -> str:
    where = spec["name"] or spec["unit"]
    what = tag["name"] or tid
    if condition == QUALITY:
        return f"{where}: {what}: signal quality {t.event} ({t.after})"
    return f"{where}: {what}, window {condition}: {t.event} ({t.after})"
