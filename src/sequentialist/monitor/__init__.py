# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""sequentialist.monitor: monitoring of equipment, processes and energy with sequential change detection.

An advisory system: it is not a safety instrumented system and makes no
claim under IEC 61511.  Statistics live in sequential-analysis; the
checkpointed engine, storage and audit log in sequentialist-core.
"""
from .version import __version__

__all__ = ["__version__"]
