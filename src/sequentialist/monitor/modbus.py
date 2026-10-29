# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
"""A Modbus TCP client that only reads, on the standard library.

After the MODBUS Application Protocol Specification V1.1b3 and the MODBUS
Messaging on TCP/IP Implementation Guide V1.0b (modbus.org): an MBAP header
(transaction id, protocol id 0, length, unit id) before each PDU; functions
01 Read Coils, 02 Read Discrete Inputs, 03 Read Holding Registers and 04
Read Input Registers, at most 2000 bits or 125 registers a request.  No
function that writes is implemented: sequentialist-monitor is an advisory
system and never changes a controller.

Controllers that speak Modbus TCP publish a table of their registers (DEIF
controllers among them, a table per product and software version): the
plant names each value by its table (coil, discrete, input, holding), its
address as the PDU carries it (from 0), its type, word order, scale and
offset.  Registers are big-endian (the protocol's rule); the order of the
two words of a 32-bit value is not fixed by the protocol, so the table of
the device says it.
"""
from __future__ import annotations

import math
import socket
import struct
from typing import Any, Callable, Iterable, Mapping, Sequence

TABLES = {"coil": 1, "discrete": 2, "holding": 3, "input": 4}
BIT_TABLES = ("coil", "discrete")
TYPES = ("bool", "uint16", "int16", "uint32", "int32", "float32")
WIDTH = {"bool": 1, "uint16": 1, "int16": 1, "uint32": 2, "int32": 2, "float32": 2}
MAX_COUNT = {"coil": 2000, "discrete": 2000, "holding": 125, "input": 125}
EXCEPTIONS = {
    1: "illegal function", 2: "illegal data address", 3: "illegal data value", 4: "server device failure",
    5: "acknowledge", 6: "server device busy", 8: "memory parity error", 10: "gateway path unavailable",
    11: "gateway target device failed to respond",
}
DEVICE_DOWN = (10, 11)  # a gateway that cannot reach the device: the device is down, not the request wrong


class ModbusError(Exception):
    """A response that breaks the protocol, or a connection that fails."""


class ModbusException(ModbusError):
    """An exception response of the server: the request was understood and refused."""

    def __init__(self, function: int, code: int):
        self.function, self.code = function, code
        super().__init__(f"function {function:02d}: exception {code:02d} ({EXCEPTIONS.get(code, 'unknown')})")


def read_request(function: int, address: int, count: int) -> bytes:
    """The PDU of a read: function, start address, quantity."""
    if function not in (1, 2, 3, 4):
        raise ValueError(f"function {function} does not read")
    limit = 2000 if function in (1, 2) else 125
    if not 0 <= address <= 0xFFFF or not 1 <= count <= limit or address + count > 0x10000:
        raise ValueError(f"cannot read {count} from address {address} with function {function}")
    return struct.pack(">BHH", function, address, count)


def read_response(function: int, count: int, pdu: bytes) -> list[int]:
    """The bits (0 or 1) or registers (0..65535) in the PDU of a response to a read of `count`."""
    if not pdu:
        raise ModbusError("an empty response")
    if pdu[0] == function | 0x80 and len(pdu) == 2:
        raise ModbusException(function, pdu[1])
    if pdu[0] != function:
        raise ModbusError(f"a response with function {pdu[0]} to function {function}")
    size = (count + 7) // 8 if function in (1, 2) else 2 * count
    if len(pdu) < 2 or pdu[1] != size or len(pdu) != 2 + size:
        raise ModbusError(f"a response of {len(pdu) - 1} bytes to a read of {count} with function {function}")
    data = pdu[2:]
    if function in (1, 2):
        return [(data[i // 8] >> (i % 8)) & 1 for i in range(count)]
    return list(struct.unpack(f">{count}H", data))


def decode(words: Sequence[int], kind: str, word_order: str = "big", bit: int | None = None) -> float:
    """A value from its registers (or its bit), before scale and offset; NaN for a float that is not a number."""
    if kind == "bool":
        if bit is not None:
            return float((words[0] >> bit) & 1)
        return 1.0 if words[0] else 0.0  # a bit of a bit table, or a whole register as a flag
    if WIDTH[kind] == 2:
        hi, lo = (words[0], words[1]) if word_order == "big" else (words[1], words[0])
        raw = struct.pack(">HH", hi, lo)
        return float(struct.unpack({"uint32": ">I", "int32": ">i", "float32": ">f"}[kind], raw)[0])
    return float(struct.unpack(">h" if kind == "int16" else ">H", struct.pack(">H", words[0]))[0])


class Point:
    """A value of the device: where it is and how it reads."""

    def __init__(self, name: str, table: str, address: int, kind: str, *, word_order: str = "big",
                 bit: int | None = None, scale: float = 1.0, offset: float = 0.0):
        if table not in TABLES:
            raise ValueError(f"{name}: unknown table {table!r}")
        if table in BIT_TABLES and kind != "bool":
            raise ValueError(f"{name}: a {table} is a bit, its type is bool")
        if bit is not None and (table in BIT_TABLES or kind != "bool" or not 0 <= bit <= 15):
            raise ValueError(f"{name}: a bit 0..15 is read from a register as a bool")
        self.name, self.table, self.address, self.kind = name, table, address, kind
        self.word_order, self.bit, self.scale, self.offset = word_order, bit, scale, offset
        self.width = WIDTH[kind]
        if address + self.width > 0x10000:
            raise ValueError(f"{name}: address {address} out of range")

    @classmethod
    def from_config(cls, name: str, config: Mapping[str, Any], word_order: str) -> Point:
        kind = config["type"] or ("bool" if config["table"] in BIT_TABLES or config["bit"] is not None
                                  else "uint16")
        return cls(name, config["table"], config["address"], kind, word_order=config["word_order"] or word_order,
                   bit=config["bit"], scale=config["scale"], offset=config["offset"])

    def value(self, words: Sequence[int]) -> float | None:
        raw = decode(words, self.kind, self.word_order, self.bit)
        if not math.isfinite(raw):
            return None
        return raw * self.scale + self.offset


class Block:
    """One read: a run of addresses in one table and the points in it."""

    def __init__(self, table: str, start: int, count: int, points: list[Point]):
        self.table, self.start, self.count, self.points = table, start, count, points

    def __repr__(self) -> str:
        return f"{self.table} {self.start}..{self.start + self.count - 1}"


def plan(points: Iterable[Point], max_gap: int = 0) -> list[Block]:
    """Points grouped into reads: neighbours in a table share one if at most `max_gap` unused addresses lie
    between them and the read stays within the protocol's limit.  With max_gap 0 nothing that is not asked
    for is read: some devices refuse an address they do not define (exception 02)."""
    blocks: list[Block] = []
    for p in sorted(points, key=lambda p: (p.table, p.address, p.name)):
        last = blocks[-1] if blocks else None
        end = p.address + p.width
        if (last is not None and last.table == p.table and p.address <= last.start + last.count + max_gap
                and max(end, last.start + last.count) - last.start <= MAX_COUNT[p.table]):
            last.count = max(end, last.start + last.count) - last.start
            last.points.append(p)
        else:
            blocks.append(Block(p.table, p.address, p.width, [p]))
    return blocks


class ModbusClient:
    """One TCP connection to a Modbus server (or gateway), opened when needed and dropped after an error."""

    def __init__(self, host: str, port: int = 502, unit: int = 1, timeout: float = 2.0,
                 connect: Callable[[], Any] | None = None):
        self.host, self.port, self.unit, self.timeout = host, port, unit, timeout
        self._connect = connect
        self._socket: Any = None
        self._transaction = 0

    def _open(self) -> Any:
        if self._socket is None:
            if self._connect is not None:
                self._socket = self._connect()
            else:
                self._socket = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._socket.settimeout(self.timeout)
        return self._socket

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def _receive(self, size: int) -> bytes:
        out = b""
        while len(out) < size:
            chunk = self._socket.recv(size - len(out))
            if not chunk:
                raise ModbusError("the server closed the connection")
            out += chunk
        return out

    def request(self, pdu: bytes) -> bytes:
        """Send a PDU, return the PDU of the response; any failure drops the connection."""
        self._transaction = (self._transaction + 1) & 0xFFFF
        tid = self._transaction
        try:
            sock = self._open()
            sock.sendall(struct.pack(">HHHB", tid, 0, len(pdu) + 1, self.unit) + pdu)
            while True:
                rid, protocol, length, unit = struct.unpack(">HHHB", self._receive(7))
                if protocol != 0 or not 2 <= length <= 254:
                    raise ModbusError(f"a header with protocol {protocol} and length {length}")
                body = self._receive(length - 1)
                if rid == tid:
                    break  # an older transaction's late answer is read and dropped
            if unit != self.unit:
                raise ModbusError(f"a response from unit {unit} to unit {self.unit}")
            return body
        except ModbusError:
            self.close()
            raise
        except OSError as error:
            self.close()
            raise ModbusError(f"{self.host}:{self.port}: {type(error).__name__}: {error}") from error

    def read(self, table: str, address: int, count: int) -> list[int]:
        function = TABLES[table]
        return read_response(function, count, self.request(read_request(function, address, count)))
