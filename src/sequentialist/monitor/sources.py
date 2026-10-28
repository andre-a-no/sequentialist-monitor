# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""Sources of readings: a directory of CSV files and a SQL historian through DB-API 2.0.

Records and their identity.  A unit's task drops a record it has seen by
(source, seq), and keeps every number it has not seen yet below the
highest it has (sequentialist-core 0.1): numbers must be dense in each
source.  The readings of one source go to several units, so each unit
numbers its own: the n-th reading a unit takes from a source has seq n.
The batch a unit gets from a poll ends with a *cursor* record (same source,
next seq, data ``{"cursor": position}``): where the source has been read up
to for that unit, written to the unit's journal with the readings it
covers.  After a restart the service reads every unit's cursor and reads
each source again from the earliest; a unit skips what lies before its own
cursor, so it sees every reading once, under the same number.

Positions: for a CSV file (source ``<name>:<file>``), the byte offset after
the last complete line read; for a SQL source, the end of the last closed
slice of time.  Readings carry an OPC UA quality: Good, Uncertain or Bad.
"""
from __future__ import annotations

import csv
import datetime as _dt
import importlib
import io
import math
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from sequentialist.core import Record, SequentialistError
from sequentialist.core.schema import parse_timestamp

GOOD, UNCERTAIN, BAD = "Good", "Uncertain", "Bad"

Cursors = Mapping[str, Mapping[str, Mapping[str, Any]]]  # unit -> source -> {"position", "next"}


class SourceError(SequentialistError):
    """A source cannot be read: the service raises a system alarm and tries again at the next poll."""


def quality(code: Any, mode: str) -> str:
    """An OPC UA quality from a source's code.

    opc-ua: StatusCode, severity in bits 30-31 (00 Good, 01 Uncertain, 1x Bad); opc-da: quality byte, bits 6-7
    (11 Good, 01 Uncertain, otherwise Bad); text: Good, Uncertain or Bad in any case; none: always Good.  A
    missing code is Good.
    """
    if mode == "none" or code is None or code == "":
        return GOOD
    if mode == "text":
        text = str(code).strip().lower()
        return {"good": GOOD, "uncertain": UNCERTAIN, "bad": BAD}.get(text, BAD)
    try:
        value = int(code)
    except (TypeError, ValueError):
        return BAD
    if mode == "opc-ua":
        severity = (value >> 30) & 0b11
        return GOOD if severity == 0 else UNCERTAIN if severity == 1 else BAD
    if mode == "opc-da":
        bits = (value >> 6) & 0b11
        return GOOD if bits == 0b11 else UNCERTAIN if bits == 0b01 else BAD
    raise ValueError(f"unknown quality mode {mode!r}")


def timestamp(value: Any) -> float | None:
    """Seconds since the epoch from a number, an RFC 3339 text with a time zone or a datetime (naive is UTC)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.timezone.utc)
        return value.timestamp()
    if isinstance(value, str):
        text = value.strip()
        try:
            return parse_timestamp(text)
        except ValueError:
            try:
                return float(text)
            except ValueError:
                return None
    return None


def number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def routes(plant: Mapping[str, Any], source: str) -> dict[str, list[str]]:
    """The units that read each tag of a source: {name in the source: [unit, ...]}."""
    out: dict[str, list[str]] = {}
    for uid, unit in sorted(plant["units"].items()):
        if unit["source"] == source:
            for tid, tag in sorted(unit["tags"].items()):
                out.setdefault(tag["source_tag"] or tid, []).append(uid)
    return out


class _Batches:
    """Records for each unit, numbered per unit and source, each source closed by a cursor."""

    def __init__(self, units: Iterable[str], cursors: Cursors):
        self.cursors = cursors
        self.out: dict[str, list[Record]] = {uid: [] for uid in units}
        self.next: dict[tuple[str, str], int] = {}

    def at(self, uid: str, source: str, default: float) -> float:
        found = self.cursors.get(uid, {}).get(source)
        return found["position"] if found else default

    def _seq(self, uid: str, source: str) -> int:
        key = (uid, source)
        if key not in self.next:
            found = self.cursors.get(uid, {}).get(source)
            self.next[key] = found["next"] if found else 0
        n = self.next[key]
        self.next[key] = n + 1
        return n

    def reading(self, uid: str, source: str, tag: str, time: float, value: float | None, q: str) -> None:
        self.out[uid].append(Record(source, self._seq(uid, source), {"tag": tag, "value": value, "quality": q},
                                    time))

    def cursor(self, uid: str, source: str, position: float) -> None:
        self.out[uid].append(Record(source, self._seq(uid, source), {"cursor": position}))


class CsvSource:
    """A directory of CSV files, each with a header line, only appended to; every file is a source."""

    def __init__(self, name: str, config: Mapping[str, Any], routes: Mapping[str, list[str]]):
        self.name, self.config, self.routes = name, config, routes
        self.units = sorted({u for us in routes.values() for u in us})
        self.directory = Path(config["directory"])
        self.skipped = 0  # lines that could not be read

    def files(self) -> list[Path]:
        return sorted(p for p in self.directory.glob(self.config["pattern"]) if p.is_file())

    def poll(self, cursors: Cursors) -> dict[str, list[Record]]:
        batches = _Batches(self.units, cursors)
        try:
            for path in self.files():
                self._read(path, batches)
        except OSError as error:
            raise SourceError(f"source {self.name!r}: {error}") from error
        return batches.out

    def _read(self, path: Path, batches: _Batches) -> None:
        source = f"{self.name}:{path.relative_to(self.directory).as_posix()}"
        positions = {uid: batches.at(uid, source, 0) for uid in self.units}
        with open(path, "rb") as f:
            header_line = f.readline()
            if not header_line.endswith(b"\n"):
                return  # the header is still being written
            header = next(csv.reader([header_line.decode("utf-8-sig")], delimiter=self.config["delimiter"]))
            columns = self.config["columns"]
            try:
                index = {k: header.index(columns[k]) for k in ("tag", "time", "value")}
            except ValueError as error:
                raise SourceError(f"{path}: {error}; the header is {header}") from None
            q_index = header.index(columns["quality"]) if columns["quality"] in header else None
            start = max(f.tell(), int(min(positions.values())))
            f.seek(start)
            end = start
            for raw in f:
                if not raw.endswith(b"\n"):
                    break  # a line still being written
                here, end = end, end + len(raw)
                try:
                    row = next(csv.reader(io.StringIO(raw.decode("utf-8")), delimiter=self.config["delimiter"]))
                    tag, time = row[index["tag"]], timestamp(row[index["time"]])
                except (StopIteration, IndexError, UnicodeDecodeError):
                    self.skipped += 1
                    continue
                if time is None:
                    self.skipped += 1
                    continue
                for uid in self.routes.get(tag, ()):
                    if here >= positions[uid]:
                        q = quality(row[q_index] if q_index is not None and q_index < len(row) else None,
                                    self.config["quality_codes"])
                        batches.reading(uid, source, tag, time, number(row[index["value"]]), q)
        for uid in self.units:
            if end > positions[uid]:
                batches.cursor(uid, source, end)

    def history(self, start: float, end: float) -> list[Record]:
        """Every reading in [start, end) of the tags of this source, numbered in reading order (for calibration)."""
        out: dict[str, list[Record]] = self.poll({})
        seen: dict[tuple[str, float], Record] = {}
        for records in out.values():
            for r in records:
                if r.time is not None and start <= r.time < end:
                    seen.setdefault((r.data["tag"], r.time), r)
        return _renumber(self.name, sorted(seen.values(), key=lambda r: (r.time, r.data["tag"])))


class SqlSource:
    """A historian polled through a DB-API 2.0 module in closed slices of time [start, end)."""

    def __init__(self, name: str, config: Mapping[str, Any], routes: Mapping[str, list[str]], connect: Any = None):
        self.name, self.config, self.routes = name, config, routes
        self.units = sorted({u for us in routes.values() for u in us})
        self._connect = connect
        self._connection: Any = None
        self.skipped = 0

    def connection(self) -> Any:
        if self._connection is None:
            if self._connect is not None:
                self._connection = self._connect()
            else:
                module = importlib.import_module(self.config["module"])
                dsn = os.environ.get(self.config["dsn_env"])
                if dsn is None:
                    raise SourceError(f"source {self.name!r}: the environment variable "
                                      f"{self.config['dsn_env']} is not set")
                self._connection = module.connect(dsn)
        return self._connection

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            finally:
                self._connection = None

    def _param(self, t: float) -> Any:
        kind = self.config["time_param"]
        if kind == "epoch":
            return t
        moment = _dt.datetime.fromtimestamp(t, _dt.timezone.utc)
        return moment.replace(tzinfo=None) if kind == "datetime" else moment.isoformat().replace("+00:00", "Z")

    def rows(self, start: float, end: float) -> list[tuple[str, float, float | None, str]]:
        """(tag, time, value, quality) in [start, end), by time then tag."""
        style = self.config["paramstyle"]
        if style is None:
            style = getattr(importlib.import_module(self.config["module"]), "paramstyle", "qmark") \
                if self._connect is None else "qmark"
        a, b = self._param(start), self._param(end)
        params: Any = {"start": a, "end": b} if style in ("named", "pyformat") else (a, b)
        try:
            cursor = self.connection().cursor()
            try:
                cursor.execute(self.config["query"], params)
                fetched = cursor.fetchall()
            finally:
                cursor.close()
        except SourceError:
            raise
        except Exception as error:  # the driver's own errors: the connection is tried afresh next time
            self.close()
            raise SourceError(f"source {self.name!r}: {type(error).__name__}: {error}") from error
        out = []
        for row in fetched:
            time = timestamp(row[1])
            if time is None or not start <= time < end:
                self.skipped += 1
                continue
            out.append((str(row[0]), time, number(row[2]),
                        quality(row[3] if len(row) > 3 else None, self.config["quality_codes"])))
        out.sort(key=lambda r: (r[1], r[0]))
        return out

    def poll(self, cursors: Cursors, until: float, first: float) -> dict[str, list[Record]]:
        """Readings in the closed slice up to `until`; a unit with no cursor starts at `first`."""
        batches = _Batches(self.units, cursors)
        positions = {uid: batches.at(uid, self.name, first) for uid in self.units}
        start = min(positions.values(), default=until)
        if until <= start:
            return batches.out
        for tag, time, value, q in self.rows(start, until):
            for uid in self.routes.get(tag, ()):
                if time >= positions[uid]:
                    batches.reading(uid, self.name, tag, time, value, q)
        for uid in self.units:
            if until > positions[uid]:
                batches.cursor(uid, self.name, until)
        return batches.out

    def history(self, start: float, end: float) -> list[Record]:
        rows = self.rows(start, end)
        return _renumber(self.name, [Record(self.name, 0, {"tag": t, "value": v, "quality": q}, time)
                                     for t, time, v, q in rows])


def _renumber(source: str, records: list[Record]) -> list[Record]:
    return [Record(source, i, r.data, r.time) for i, r in enumerate(records)]


def open_source(name: str, config: Mapping[str, Any], plant: Mapping[str, Any]) -> CsvSource | SqlSource:
    r = routes(plant, name)
    if config["type"] == "csv":
        return CsvSource(name, config, r)
    return SqlSource(name, config, r)
