# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""sequentialist.monitor: monitoring of equipment, processes and energy with sequential change detection.

An advisory system: it is not a safety instrumented system and makes no
claim under IEC 61511.  Statistics live in sequential-analysis; the
checkpointed engine, storage and audit log in sequentialist-core.

config     the plant (monitor.plant/1), its baseline (monitor.baseline/1), a unit's spec (monitor.unit/1)
budget     the plant's budget of false alarms divided into the ARL of every detector
calibrate  models and thresholds from a reference period of normal operation
pipeline   window, model, residual and detector of one feature (procedures of sequential-analysis)
windows    readings into windows on the data's clock
alarms     ISA-18.2 alarms with the sub-states of OPC UA Alarms & Conditions
unit       the task of one unit (a Handler of sequentialist-core)
sources    CSV directories and SQL historians, with cursors in the units' journals
service    the polling service; api: its HTTP API; notify: log, webhook, e-mail
kpi        KPIs of alarm management; report: reports with the statements every report carries
"""
from .alarms import Alarm
from .budget import Allocation, allocate
from .calibrate import calibrate
from .config import BaselineSpec, PlantSpec, UnitSpec, load_baseline, load_plant
from .kpi import kpi
from .notify import Announce, EmailSink, plant_sinks
from .report import STATEMENTS, plant_report
from .service import Service
from .sources import CsvSource, SourceError, SqlSource
from .unit import KIND, UnitHandler
from .unitspec import build_unit_spec
from .version import __version__

__all__ = [
    "KIND",
    "STATEMENTS",
    "Alarm",
    "Allocation",
    "Announce",
    "BaselineSpec",
    "CsvSource",
    "EmailSink",
    "PlantSpec",
    "Service",
    "SourceError",
    "SqlSource",
    "UnitHandler",
    "UnitSpec",
    "__version__",
    "allocate",
    "build_unit_spec",
    "calibrate",
    "kpi",
    "load_baseline",
    "load_plant",
    "plant_report",
    "plant_sinks",
]
