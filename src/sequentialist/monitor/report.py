# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Reports of a plant over a period, in JSON and Markdown, every conclusion with its guarantee.

Every report begins with the same statements (`STATEMENTS`): an advisory
system, not a safety instrumented system; the guarantee of a detector on a
model's residuals is `model`; parameters estimated on a reference period are
treated as known; the time to detect is not guaranteed.  A report cannot be
made without them.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from sequentialist.core import AuditEntry, Finding, Report, Section, Table
from sequentialist.core.schema import format_timestamp

from .config import BaselineSpec, PlantSpec
from .kpi import kpi

STATEMENTS = (
    "An advisory system: sequentialist-monitor is not a safety instrumented system and makes no claim under "
    "IEC 61511 (no SIL). Alarms advise people; they do not act on the process.",
    "The guarantee of a detector watching the residuals of a model is `model`: its average run length to a false "
    "alarm holds while the model of normal operation holds. Thresholds set by simulation are `approximate`. "
    "Statements about false alarms are about their long-run mean, not about every hour.",
    "Parameters were estimated on a reference period of normal operation and are then treated as known; the "
    "error of that estimate is not part of any guarantee.",
    "The time to detect a change is not guaranteed; where shown, it is estimated by simulation.",
)


def plant_report(plant: PlantSpec, baseline: BaselineSpec | None, entries: Iterable[AuditEntry], start: float,
                 end: float, *, title: str | None = None) -> Report:
    entries = list(entries)
    site = plant.data["site"]
    report = Report(title or f"Monitoring of {plant.data['name'] or site}: {format_timestamp(start)} to "
                             f"{format_timestamp(end)}",
                    site, plant.id, end, summary=" ".join(STATEMENTS))
    report.sections.append(Section("Scope and guarantees", "\n\n".join(f"{i}. {s}" for i, s in
                                                                      enumerate(STATEMENTS, 1))))
    report.sections.append(_alarms(entries, start, end))
    report.sections.append(_kpi(plant, entries, start, end))
    if baseline is not None:
        report.sections.append(_calibration(baseline))
    return report


def _alarms(entries: list[AuditEntry], start: float, end: float) -> Section:
    rows = []
    for e in sorted(entries, key=lambda e: (e.basis.get("time", 0), e.task_id, e.n)):
        b = e.basis
        if e.decision != "activated" or not start <= b.get("time", -1) < end:
            continue
        guarantee = b.get("guarantee", "none")  # a quality or source alarm is a rule on counts
        rows.append([format_timestamp(b["time"]), b["source_name"], b["condition"], b["priority"],
                     "yes" if b["notify"] else "no (" + b["state"]["after"] + ")", guarantee])
    section = Section("Alarms", f"{len(rows)} alarms became active in the period; refires of active alarms are "
                                f"not listed.")
    section.tables.append(Table(["time", "source", "condition", "priority", "announced", "guarantee"], rows))
    return section


def _kpi(plant: PlantSpec, entries: list[AuditEntry], start: float, end: float) -> Section:
    a, budget = plant.data["alarms"], plant.data["budget"]
    per_hour = budget["alarms_per_hour"] * budget["detector_share"]
    found = kpi(entries, start, end, flood=a["flood"], stale_after=a["stale_after"], budget_per_hour=per_hour)
    section = Section("Alarm management (ISA-18.2)", "Counts from the audit log; guarantee none.")
    rows = []
    for console, k in found.items():
        rows.append([console, k["annunciated"], k["per_10_minutes"]["mean"], k["per_10_minutes"]["max"],
                     k["per_10_minutes"]["flood_share"], len(k["stale"]), len(k["chattering"]),
                     k["detector_fires_per_hour"], per_hour, "none"])
        section.findings.append(Finding(
            f"Console {console}: detectors raised {k['detector_fires_per_hour']:.3g} alarms per hour against a "
            f"budget of {per_hour:.3g} for false alarms in normal operation.", "none",
            {"fires_per_hour": k["detector_fires_per_hour"], "budget_per_hour": per_hour},
            notes=("A count over the period, not a test: real changes count too.",)))
        if k["top_sources"]:
            section.tables.append(Table(["source", "alarms", "share"],
                                        [[s["source"], s["alarms"], s["share"]] for s in k["top_sources"]],
                                        f"Console {console}: the ten most frequent sources"))
        if k["actions"]:
            section.tables.append(Table(["time", "source", "action", "actor", "reason"],
                                        [[format_timestamp(x["time"]), x["source"], x["action"], x["actor"],
                                          x["reason"]] for x in k["actions"]],
                                        f"Console {console}: shelving and removal from service"))
    section.tables.insert(0, Table(["console", "annunciated", "per 10 min (mean)", "per 10 min (max)",
                                    "flood share", "stale", "chattering", "detector alarms per hour",
                                    "budget per hour", "guarantee"], rows))
    return section


def _calibration(baseline: BaselineSpec) -> Section:
    rows = []
    for uid, unit in sorted(baseline.data["units"].items()):
        ref = unit["reference"]
        for key, f in sorted(unit["features"].items()):
            m = f["model"]
            params = ", ".join(f"{k} {_short(v)}" for k, v in m.items() if k != "type")
            rows.append([uid, key, m["type"], params, f"{format_timestamp(ref['from'])} to "
                         f"{format_timestamp(ref['to'])}", f["residuals"]["n"], f["residuals"]["sd"],
                         "; ".join(f["warnings"]) or "—", "none"])
    section = Section("Calibration", "Models fitted by sequential-analysis on the reference periods, then treated "
                                     "as known; residual statistics are descriptive (guarantee none).")
    section.tables.append(Table(["unit", "feature", "model", "parameters", "reference period", "windows",
                                 "residual sd", "warnings", "guarantee"], rows))
    return section


def _short(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_short(v) for v in value) + "]"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def entries_of(storage: Any, plant: PlantSpec, units: Mapping[str, Any] | None = None) -> list[AuditEntry]:
    """The audit entries of every task of the plant's units in a storage (old specs' tasks included)."""
    prefixes = [f"{plant.data['site']}.{u['area']}.{uid}." for uid, u in plant.data["units"].items()
                if units is None or uid in units]
    out = []
    for task_id in storage.tasks():
        if any(task_id.startswith(p) for p in prefixes):
            out += [AuditEntry.from_json(e) for e in storage.audit_read(task_id)]
    return out
