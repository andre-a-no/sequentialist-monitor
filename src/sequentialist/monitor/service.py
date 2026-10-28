# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""The service: polls the sources, feeds the units' tasks, takes commands, checkpoints, publishes status.

One process per shard.  Every cycle it reads each source from the units'
cursors, gives every unit its batch in one `ingest`, and moves every unit's
watermark to the time of the poll minus `lateness`, so windows close even
when no data come.  Commands from the HTTP API wait in a queue and run
between cycles, in the same thread as everything that touches a task.

A checkpoint writes a unit's whole state (sequentialist-core 0.1), about a
millisecond per run of sequential-analysis; left to the engine (every N
records of a unit) every unit would checkpoint in the same cycle.  So the
engine checkpoints only after decisions, and the service checkpoints the
units in turn, each once per `checkpoint_interval`.

A source that cannot be read raises the alarm "@<source>" (high) in every
unit that reads it, until a poll succeeds.  A task that fails is reopened
once (its journal replayed); failing again, its unit stays down and the
status says so.
"""
from __future__ import annotations

import functools
import logging
import math
import os
import queue
import threading
import time as _time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, cast

from sequentialist.core import Engine, Record, SequentialistError, Task, open_storage, pseudonymize
from sequentialist.core.storage.base import Storage

from .budget import allocate
from .config import BaselineSpec, PlantSpec, UnitSpec
from .notify import plant_sinks
from .sources import SourceError, SqlSource, open_source
from .unit import KIND, UnitHandler
from .unitspec import build_unit_spec

logger = logging.getLogger("sequentialist.monitor")
SERVICE_ACTOR = "sequentialist-monitor"


@dataclass
class Pending:
    """A command from the API waiting for the service's thread; `done` is set with its outcome."""

    unit: str
    name: str
    args: dict[str, Any]
    actor: str
    reason: str
    done: threading.Event = field(default_factory=threading.Event)
    status: int = 0
    result: dict[str, Any] = field(default_factory=dict)


class Service:
    def __init__(self, plant: PlantSpec, baseline: BaselineSpec | None, *, storage: Storage | None = None,
                 shard: tuple[int, int] = (0, 1), clock: Callable[[], float] = _time.time,
                 sources: Mapping[str, Any] | None = None, sinks: list[Any] | None = None,
                 checkpoint_interval: float = 300.0, environ: Mapping[str, str] | None = None):
        self.plant, self.clock = plant, clock
        self.env = os.environ if environ is None else environ
        self.storage = storage if storage is not None else open_storage(plant.data["storage"])
        self.sinks = sinks if sinks is not None else plant_sinks(plant.data["notify"], self.env)
        self.engine = Engine(self.storage, shard=shard, sinks=self.sinks, checkpoint_every=10**9, clock=clock)
        self.checkpoint_interval = checkpoint_interval
        self.lateness = float(plant.data["lateness"])
        key_env = plant.data["actor_key_env"]
        self.actor_key = self.env.get(key_env) if key_env else None
        if key_env and not self.actor_key:
            raise ValueError(f"the environment variable {key_env} (the key of actors' pseudonyms) is not set")
        allocations = allocate(plant)
        self.specs: dict[str, UnitSpec] = {}
        for uid in sorted(plant.data["units"]):
            spec = build_unit_spec(plant, baseline, uid, allocations)
            if self.engine.shard[1] == 1 or _in_shard(spec.task_id(), self.engine.shard):
                self.specs[uid] = spec
        self.tasks: dict[str, Task] = {}
        self.down: dict[str, str] = {}
        for uid, spec in self.specs.items():
            self.tasks[uid] = self._open(uid, spec)
        used = {plant.data["units"][u]["source"] for u in self.specs}
        self.sources: dict[str, Any] = dict(sources) if sources is not None else {
            name: open_source(name, cfg, plant.data) for name, cfg in plant.data["sources"].items() if name in used}
        self.source_status: dict[str, dict[str, Any]] = {name: {"ok": None, "error": None} for name in self.sources}
        self.commands: queue.Queue[Pending] = queue.Queue()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.snapshot: dict[str, Any] = {}
        self._turn = 0
        self.cycles = 0
        self._publish(None)

    # ------------------------------------------------------------------ tasks

    def handler(self, uid: str) -> UnitHandler:
        return cast(UnitHandler, self.tasks[uid].handler)

    def _open(self, uid: str, spec: UnitSpec) -> Task:
        task_id = spec.task_id()
        if self.engine.exists(task_id):
            return self.engine.open(task_id, UnitHandler)
        task = self.engine.create(task_id, KIND, spec.data, UnitHandler(spec.data))
        self._carry_over(uid, task)
        return task

    def _carry_over(self, uid: str, task: Task) -> None:
        """A new spec makes a new task: people's shelving and removals from service carry over from the last one."""
        prefix = task.task_id[: task.task_id.rindex(".") + 1]
        previous = [t for t in self.storage.tasks() if t.startswith(prefix) and t != task.task_id]
        if not previous:
            return
        states = [(s[1]["created_at"], t, s[1]) for t in previous if (s := self.storage.load(t)) is not None]
        _, old_id, state = max(states)
        reason = f"carried over from task {old_id} (its spec changed)"
        for key, alarm in sorted(state["handler"]["alarms"].items()):
            if key not in cast(UnitHandler, task.handler).alarms:
                continue
            if alarm["out_of_service"]:
                task.command("remove_from_service", {"source": key}, actor=SERVICE_ACTOR, reason=reason)
            elif alarm["shelving"] != "Unshelved" and alarm["shelved_until"] is not None:
                left = alarm["shelved_until"] - (task.watermark or self.clock())
                if alarm["shelving"] == "OneShotShelved":
                    task.command("shelve", {"source": key, "duration": "oneshot"}, actor=SERVICE_ACTOR,
                                 reason=reason)
                elif left > 0:
                    task.command("shelve", {"source": key, "duration": left}, actor=SERVICE_ACTOR, reason=reason)
        logger.info("unit %s: new task %s, carried over from %s", uid, task.task_id, old_id)

    def _guard(self, uid: str, action: Callable[[Task], Any]) -> Any:
        """Run an action on a unit's task; a failed task is reopened once, then the unit is down."""
        if uid in self.down:
            return None
        task = self.tasks[uid]
        try:
            return action(task)
        except SequentialistError as error:
            if task.failed is None:
                raise
            logger.error("unit %s: task %s failed: %s; reopening it", uid, task.task_id, error)
            try:
                task.close()
                self.tasks[uid] = self.engine.open(task.task_id, UnitHandler)
                return action(self.tasks[uid])
            except Exception as again:
                self.down[uid] = f"{type(again).__name__}: {again}"
                logger.error("unit %s is down: %s", uid, self.down[uid])
                return None

    # ------------------------------------------------------------------ a cycle

    def cycle(self, now: float | None = None) -> None:
        """Poll every source, feed every unit, move the watermarks, run waiting commands, checkpoint in turn."""
        now = self.clock() if now is None else now
        until = now - self.lateness
        batches: dict[str, list[Record]] = {uid: [] for uid in self.tasks}
        for name, source in sorted(self.sources.items()):
            units = [u for u in self.tasks if self.plant.data["units"][u]["source"] == name and u not in self.down]
            cursors = {u: self.handler(u).cursors for u in units}
            try:
                if isinstance(source, SqlSource):
                    got = source.poll(cursors, until=until, first=until)
                else:
                    got = source.poll(cursors)
                failed = None
            except SourceError as error:
                got, failed = {}, str(error)
                logger.error("%s", error)
            was = self.source_status[name]["error"]
            self.source_status[name] = {"ok": now if failed is None else self.source_status[name]["ok"],
                                        "error": failed}
            for u in units:
                batches[u].extend(got.get(u, []))
                if failed is not None or was is not None:
                    batches[u].append(self._system(u, name, failed))
        for uid in sorted(self.tasks):
            batch = batches[uid]
            if batch:
                self._guard(uid, functools.partial(Task.ingest, records=batch))
            self._guard(uid, lambda t: t.advance(until))
        self._run_commands()
        self._checkpoint_in_turn()
        self.cycles += 1
        self._publish(now)

    def _system(self, uid: str, source: str, error: str | None) -> Record:
        stream = f"{source}#system"
        found = self.handler(uid).cursors.get(stream)
        seq = found["next"] if found else 0
        data = {"system": "source_down", "source": source, "error": error} if error is not None else \
            {"system": "source_up", "source": source}
        return Record(stream, seq, data)

    def _checkpoint_in_turn(self) -> None:
        live = sorted(u for u in self.tasks if u not in self.down)
        if not live:
            return
        period = self._period()
        per_cycle = max(1, math.ceil(len(live) * period / self.checkpoint_interval))
        for _ in range(min(per_cycle, len(live))):
            uid = live[self._turn % len(live)]
            self._turn += 1
            self._guard(uid, lambda t: t.checkpoint())

    def _period(self) -> float:
        periods = [cfg["period"] for name, cfg in self.plant.data["sources"].items()
                   if name in self.sources and cfg["type"] == "sql"]
        return float(min(periods)) if periods else 5.0

    # ------------------------------------------------------------------ commands of people

    def submit(self, unit: str, name: str, args: dict[str, Any], actor: str, reason: str,
               timeout: float = 30.0) -> Pending:
        """From any thread: queue a command and wait for the service's thread to run it."""
        if self.actor_key:
            actor = pseudonymize(actor, self.actor_key)
        pending = Pending(unit, name, dict(args), actor, reason)
        self.commands.put(pending)
        self.wake.set()
        if not pending.done.wait(timeout):
            pending.status, pending.result = 503, {"error": "the service did not take the command in time"}
        return pending

    def _run_commands(self) -> None:
        while True:
            try:
                p = self.commands.get_nowait()
            except queue.Empty:
                return
            try:
                p.status, p.result = self._command(p)
            except Exception as error:  # never leave the caller waiting
                p.status, p.result = 500, {"error": f"{type(error).__name__}: {error}"}
            p.done.set()

    def _command(self, p: Pending) -> tuple[int, dict[str, Any]]:
        if p.unit not in self.tasks:
            return 404, {"error": f"no unit {p.unit!r} in this service"}
        if p.unit in self.down:
            return 503, {"error": f"unit {p.unit!r} is down: {self.down[p.unit]}"}
        if not p.actor or not p.reason:
            return 400, {"error": "a command needs an actor and a reason"}
        task = self.tasks[p.unit]
        refusal = self.handler(p.unit).check(p.name, p.args)
        if refusal is not None:
            return 409, {"error": refusal}
        result = self._guard(p.unit, lambda t: t.command(p.name, p.args, actor=p.actor, reason=p.reason))
        entries = [{"n": e.n, "hash": e.hash, "transition": e.decision, "source": e.basis.get("source")}
                   for e in (result.entries if result is not None else [])]
        self._publish(self.clock())
        return 202, {"task": task.task_id, "entries": entries}

    # ------------------------------------------------------------------ status

    def storage_audit(self, task_id: str) -> list[Any]:
        """A task's audit log, read from the storage (from any thread)."""
        from sequentialist.core import AuditEntry

        return [AuditEntry.from_json(e) for e in self.storage.audit_read(task_id)]

    def _publish(self, now: float | None) -> None:
        units = {}
        for uid, task in sorted(self.tasks.items()):
            h = self.handler(uid)
            spec = self.specs[uid].data
            features = {}
            for tid, fname in h.features:
                key = f"{tid}.{fname}"
                f = spec["tags"][tid]["features"][fname]
                detectors = {}
                for stream, entry in f["streams"].items():
                    out = task.last_output(f"{key}.{stream}")
                    statistic = None
                    if isinstance(out, dict):
                        statistic = out.get("statistic", out.get("log_statistic", out.get("recent_change_probability")))
                    detectors[stream] = {"statistic": statistic, "threshold": entry["threshold"], "arl": entry["arl"]}
                features[key] = {"last": h.fed[key].get("last"), "detectors": detectors,
                                 "guarantee": f["guarantee"]}
            units[uid] = {
                "task": task.task_id, "spec_id": task.spec_id, "console": spec["console"],
                "running": h.running, "watermark": task.watermark, "down": self.down.get(uid),
                "alarms": {k: {"state": a.isa_state(), "opc_ua": a.opc_ua(), "priority": a.priority,
                               "severity": spec["severity"][a.priority], "active_since": a.active_since,
                               "shelved_until": a.shelved_until, "chattering": a.chattering}
                           for k, a in sorted(h.alarms.items())},
                "features": features, "counters": dict(h.counters),
            }
        self.snapshot = {"time": now, "cycles": self.cycles, "sources": dict(self.source_status), "units": units,
                         "down": dict(self.down)}

    # ------------------------------------------------------------------ running

    def run(self) -> None:
        """Cycle every poll period until `stop`."""
        period = self._period()
        while not self.stopping.is_set():
            began = self.clock()
            try:
                self.cycle(began)
            except Exception:
                logger.exception("the cycle failed; trying again at the next one")
            self._run_commands()
            while not self.stopping.is_set():
                left = began + period - self.clock()
                if left <= 0:
                    break
                self.wake.wait(min(left, 1.0))
                if self.wake.is_set():
                    self.wake.clear()
                    self._run_commands()

    def stop(self) -> None:
        self.stopping.set()
        self.wake.set()

    def close(self) -> None:
        self._run_commands()
        for task in self.tasks.values():
            try:
                task.close()
            except Exception:
                logger.exception("closing task %s", task.task_id)
        for source in self.sources.values():
            close = getattr(source, "close", None)
            if close is not None:
                close()
        for sink in self.sinks:
            close = getattr(sink, "close", None)
            if close is not None:
                close()
        self.storage.close()


def _in_shard(task_id: str, shard: tuple[int, int]) -> bool:
    from sequentialist.core import shard_of

    return shard_of(task_id, shard[1]) == shard[0]
