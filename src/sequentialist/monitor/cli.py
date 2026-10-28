# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""`sequentialist monitor ...`: the subcommands of sequentialist-monitor.

Registered with the `sequentialist` command of sequentialist-core through the
entry point group ``sequentialist.commands``.
"""
from __future__ import annotations

import argparse
from typing import Any, Callable

from .version import __version__

# each adds its parser to the subparsers of `sequentialist monitor`
_COMMANDS: list[Callable[[Any], None]] = []


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
