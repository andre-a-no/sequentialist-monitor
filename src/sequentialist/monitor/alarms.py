# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Alarm management after ISA-18.2 / IEC 62682, with the state model of OPC UA Alarms & Conditions.

Every source of alarms (a feature of a tag, or the quality of a tag's
signal) holds the sub-states of an OPC UA AlarmCondition: ActiveState,
AckedState, ShelvingState (Unshelved, TimedShelved, OneShotShelved),
SuppressedState and OutOfServiceState.  The ISA-18.2 state is derived from
them (`isa_state`).  Nothing here does input or output: `Alarm` is a value
the unit's handler keeps in its state and changes by events (a detector
fired, a window passed without firing, time passed) and by commands of
people; each change returns a `Transition` the handler writes to the audit
log, and the sinks decide by its `notify` flag whether to tell anyone.

A change detector says "a change happened", not "the condition holds".  So
an alarm returns to normal when its restarted detector has stayed silent
for `clear_after` windows; while the shift persists the detector fires
again and the alarm stays active without a new notification.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

UNSHELVED, TIMED, ONESHOT = "Unshelved", "TimedShelved", "OneShotShelved"

# ISA-18.2 states, derived from the OPC UA sub-states
NORMAL, UNACK, ACKED, RTN_UNACK = "normal", "unacknowledged", "acknowledged", "returned_unacknowledged"
SHELVED, SUPPRESSED, OUT_OF_SERVICE = "shelved", "suppressed_by_design", "out_of_service"

COMMANDS = ("ack", "shelve", "unshelve", "remove_from_service", "return_to_service")


@dataclass
class Settings:
    clear_after: int
    rearm: float
    chatter_count: int
    chatter_window: float
    max_shelve: float

    @classmethod
    def of(cls, alarms: dict[str, Any]) -> Settings:
        return cls(alarms["clear_after"], alarms["rearm"], alarms["chatter_count"], alarms["chatter_window"],
                   alarms["max_shelve"])


@dataclass
class Transition:
    """One change of an alarm: what the audit log records and what sinks may announce."""

    source: str
    event: str  # activated, refired, returned, acknowledged, normal, shelved, unshelved, suppressed, ...
    before: str  # ISA-18.2 state
    after: str
    notify: bool
    time: float


@dataclass
class Alarm:
    source: str
    priority: str
    active: bool = False
    acked: bool = True
    shelving: str = UNSHELVED
    shelved_until: float | None = None
    suppressed: bool = False
    out_of_service: bool = False
    active_since: float | None = None
    quiet: int = 0  # windows since the detector last fired
    fires: list[float] = field(default_factory=list)  # recent fire times, for chattering
    chattering: bool = False
    last_fire: float | None = None

    # ------------------------------------------------------------------ state

    def isa_state(self) -> str:
        if self.out_of_service:
            return OUT_OF_SERVICE
        if self.suppressed:
            return SUPPRESSED
        if self.shelving != UNSHELVED:
            return SHELVED
        if self.active:
            return ACKED if self.acked else UNACK
        return NORMAL if self.acked else RTN_UNACK

    def opc_ua(self) -> dict[str, Any]:
        """The sub-states as OPC UA Alarms & Conditions names them."""
        return {"ActiveState": self.active, "AckedState": self.acked, "ShelvingState": self.shelving,
                "SuppressedState": self.suppressed, "OutOfServiceState": self.out_of_service}

    def silenced(self) -> bool:
        """Whether a change would be announced: not while shelved, suppressed, out of service or chattering."""
        return self.out_of_service or self.suppressed or self.shelving != UNSHELVED or self.chattering

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Alarm:
        return cls(**data)

    # ------------------------------------------------------------------ events

    def _change(self, event: str, before: str, time: float, notify: bool) -> Transition:
        return Transition(self.source, event, before, self.isa_state(), notify, time)

    def fire(self, time: float, s: Settings) -> Transition:
        """The detector raised an alarm (and restarted)."""
        before = self.isa_state()
        self.quiet = 0
        self.last_fire = time
        self.fires = [t for t in self.fires if t > time - s.chatter_window] + [time]
        if len(self.fires) >= s.chatter_count:
            self.chattering = True
        if self.active:
            return self._change("refired", before, time, False)
        self.active, self.acked, self.active_since = True, False, time
        return self._change("activated", before, time, not self.silenced())

    def quiet_window(self, time: float, s: Settings) -> Transition | None:
        """A window passed and the detector did not fire."""
        self.quiet += 1
        if not self.active or self.quiet < s.clear_after:
            return None
        before = self.isa_state()
        self.active, self.active_since = False, None
        if self.shelving == ONESHOT:  # one-shot shelving lasts until the alarm returns to normal
            self.shelving, self.shelved_until = UNSHELVED, None
        return self._change("returned", before, time, not self.silenced() and before != SHELVED)

    def tick(self, time: float, s: Settings) -> list[Transition]:
        """Time passed: shelving expires, a chattering source re-arms."""
        out = []
        if self.shelving != UNSHELVED and self.shelved_until is not None and time >= self.shelved_until:
            before = self.isa_state()
            self.shelving, self.shelved_until = UNSHELVED, None
            out.append(self._change("unshelved", before, time, self.active and not self.silenced()))
        if self.chattering and (self.last_fire is None or time - self.last_fire >= s.rearm):
            self.chattering, self.fires = False, []
        return out

    def set_suppressed(self, suppressed: bool, time: float) -> Transition | None:
        """Suppression by design: the unit stopped (or started again)."""
        if self.suppressed == suppressed:
            return None
        before = self.isa_state()
        self.suppressed = suppressed
        if suppressed:  # its detectors restart when the unit does; nothing stays active meanwhile
            self.active, self.acked, self.active_since, self.quiet = False, True, None, 0
        return self._change("suppressed" if suppressed else "unsuppressed", before, time, False)

    # ------------------------------------------------------------------ commands of people

    def refusal(self, name: str, args: dict[str, Any], s: Settings) -> str | None:
        """Why the command cannot apply now (None if it can): checked before it reaches the journal."""
        if name == "ack":
            return None if not self.acked else "already acknowledged"
        if name == "shelve":
            if self.out_of_service:
                return "out of service"
            duration = args.get("duration")
            if duration == "oneshot" or (isinstance(duration, (int, float)) and not isinstance(duration, bool)
                                         and 0 < duration <= s.max_shelve):
                return None
            return f"duration must be 'oneshot' or seconds in (0, {s.max_shelve:g}]"
        if name == "unshelve":
            return None if self.shelving != UNSHELVED else "not shelved"
        if name == "remove_from_service":
            return None if not self.out_of_service else "already out of service"
        if name == "return_to_service":
            return None if self.out_of_service else "not out of service"
        return f"unknown command {name!r}"

    def command(self, name: str, args: dict[str, Any], time: float, s: Settings) -> Transition:
        """Apply a command `refusal` accepted."""
        before = self.isa_state()
        if name == "ack":
            self.acked = True
            return self._change("acknowledged", before, time, True)
        if name == "shelve":
            if args["duration"] == "oneshot":
                self.shelving, self.shelved_until = ONESHOT, time + s.max_shelve
            else:
                self.shelving, self.shelved_until = TIMED, time + float(args["duration"])
            return self._change("shelved", before, time, True)
        if name == "unshelve":
            self.shelving, self.shelved_until = UNSHELVED, None
            return self._change("unshelved", before, time, True)
        if name == "remove_from_service":
            self.out_of_service = True
            self.active, self.acked, self.active_since, self.quiet = False, True, None, 0
            return self._change("removed_from_service", before, time, True)
        if name == "return_to_service":
            self.out_of_service = False
            return self._change("returned_to_service", before, time, True)
        raise ValueError(f"unknown command {name!r}")
