# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Key performance indicators of alarm management (ISA-18.2, EEMUA 191), counted from the audit log.

Counts, not statistics: their guarantee is `none`.  Per console and period:

* annunciated alarms (activations that notified) per 10 minutes: mean, maximum, and the share of 10-minute
  intervals in a flood (at least `flood` alarms);
* stale alarms: active longer than `stale_after` at the end of the period;
* chattering sources, the ten most frequent sources and their share, alarms by priority;
* shelving and removal from service: how many, by whom and why;
* statistical alarms (detector fires, refires included) per hour against the console's budget.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Any, Iterable

from sequentialist.core import AuditEntry

from .config import PRIORITIES


def kpi(entries: Iterable[AuditEntry], start: float, end: float, *, flood: int = 10, stale_after: float = 86400,
        budget_per_hour: float | None = None) -> dict[str, dict[str, Any]]:
    """KPIs per console over [start, end) from the audit entries of its units (any order, any tasks)."""
    if not end > start:
        raise ValueError("the period must end after it starts")
    events = sorted((e for e in entries if e.kind in ("decision", "action") and "console" in e.basis),
                    key=lambda e: (e.basis["time"], e.task_id, e.n))
    consoles: dict[str, list[AuditEntry]] = {}
    for e in events:
        consoles.setdefault(e.basis["console"], []).append(e)
    return {console: _console(es, start, end, flood, stale_after, budget_per_hour)
            for console, es in sorted(consoles.items())}


def _console(events: list[AuditEntry], start: float, end: float, flood: int, stale_after: float,
             budget_per_hour: float | None) -> dict[str, Any]:
    intervals = max(1, math.ceil((end - start) / 600))
    per_interval = [0] * intervals
    sources: Counter[str] = Counter()
    priorities: Counter[str] = Counter()
    chattering: set[str] = set()
    fires = 0
    active_since: dict[str, float] = {}
    actions: list[dict[str, Any]] = []
    for e in events:
        b = e.basis
        key = f"{b['source_name']}/{b['condition']}"
        t = b["time"]
        if t < end:  # the state at the end of the period
            if b["opc_ua"]["ActiveState"]:
                active_since.setdefault(key, t)
            else:
                active_since.pop(key, None)
        if not start <= t < end:
            continue
        if b.get("chattering"):
            chattering.add(key)
        if e.decision in ("activated", "refired") and b["condition"] != "quality":
            fires += 1
        if e.decision == "activated" and b["notify"]:
            per_interval[int((t - start) // 600)] += 1
            sources[key] += 1
            priorities[b["priority"]] += 1
        if e.kind == "action" and e.decision in ("shelved", "removed_from_service", "returned_to_service",
                                                 "unshelved"):
            actions.append({"time": t, "source": key, "action": e.decision, "actor": e.actor, "reason": e.reason})
    total = sum(per_interval)
    hours = (end - start) / 3600
    out: dict[str, Any] = {
        "period": [start, end],
        "annunciated": total,
        "per_10_minutes": {"mean": total / intervals, "max": max(per_interval),
                           "flood_share": sum(1 for c in per_interval if c >= flood) / intervals},
        "stale": sorted(k for k, since in active_since.items() if end - since > stale_after),
        "chattering": sorted(chattering),
        "top_sources": [{"source": k, "alarms": c, "share": c / total} for k, c in sources.most_common(10)],
        "priorities": {p: priorities[p] / total if total else 0.0 for p in PRIORITIES},
        "actions": actions,
        "detector_fires_per_hour": fires / hours,
        "guarantee": "none",
    }
    if budget_per_hour is not None:
        out["detector_budget_per_hour"] = budget_per_hour
    return out
