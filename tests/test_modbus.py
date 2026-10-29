# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
import asyncio
import logging
import math
import socket
import socketserver
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

from sequentialist.core import SpecError
from sequentialist.monitor.config import load_plant
from sequentialist.monitor.modbus import (
    ModbusError,
    ModbusException,
    Point,
    decode,
    plan,
    read_request,
    read_response,
)
from sequentialist.monitor.sources import BAD, GOOD, CsvSource, ModbusSource, Recorder, SourceError, record

from .plants import plant as plant_dict


class SpecificationTest(unittest.TestCase):
    """The examples of the MODBUS Application Protocol Specification V1.1b3, section 6."""

    def test_read_coils(self):  # 6.1: coils 20-38
        self.assertEqual(read_request(1, 19, 19), bytes.fromhex("01 0013 0013"))
        bits = read_response(1, 19, bytes.fromhex("01 03 CD 6B 05"))
        self.assertEqual(bits, [1, 0, 1, 1, 0, 0, 1, 1, 1, 1, 0, 1, 0, 1, 1, 0, 1, 0, 1])

    def test_read_discrete_inputs(self):  # 6.2: inputs 197-218
        self.assertEqual(read_request(2, 196, 22), bytes.fromhex("02 00C4 0016"))
        bits = read_response(2, 22, bytes.fromhex("02 03 AC DB 35"))
        self.assertEqual(bits, [0, 0, 1, 1, 0, 1, 0, 1, 1, 1, 0, 1, 1, 0, 1, 1, 1, 0, 1, 0, 1, 1])

    def test_read_holding_registers(self):  # 6.3: registers 108-110
        self.assertEqual(read_request(3, 107, 3), bytes.fromhex("03 006B 0003"))
        self.assertEqual(read_response(3, 3, bytes.fromhex("03 06 022B 0000 0064")), [555, 0, 100])

    def test_read_input_registers(self):  # 6.4: register 9
        self.assertEqual(read_request(4, 8, 1), bytes.fromhex("04 0008 0001"))
        self.assertEqual(read_response(4, 1, bytes.fromhex("04 02 000A")), [10])

    def test_exceptions_and_malformed_responses(self):  # 7: an exception response is the function + 0x80
        with self.assertRaises(ModbusException) as caught:
            read_response(3, 1, bytes.fromhex("83 02"))
        self.assertEqual(caught.exception.code, 2)
        for bad in ("04 02 000A", "03 04 0001", "03 02 00"):
            with self.assertRaises(ModbusError):
                read_response(3, 1, bytes.fromhex(bad))

    def test_limits(self):
        read_request(3, 0, 125)
        read_request(1, 0, 2000)
        for args in ((3, 0, 126), (1, 0, 2001), (3, 0, 0), (3, 65535, 2), (5, 0, 1)):
            with self.assertRaises(ValueError):
                read_request(*args)


class DecodeTest(unittest.TestCase):
    def test_types(self):
        self.assertEqual(decode([0xFFFF], "int16"), -1)
        self.assertEqual(decode([0xFFFF], "uint16"), 65535)
        self.assertEqual(decode([0x0001, 0x0000], "uint32"), 65536)
        self.assertEqual(decode([0x0000, 0x0001], "uint32", "little"), 65536)
        self.assertEqual(decode([0xFFFF, 0xFFFE], "int32"), -2)
        self.assertAlmostEqual(decode([0x4049, 0x0FDB], "float32"), math.pi, places=6)
        self.assertAlmostEqual(decode([0x0FDB, 0x4049], "float32", "little"), math.pi, places=6)
        self.assertEqual([decode([0b1010], "bool", bit=b) for b in range(4)], [0, 1, 0, 1])

    def test_scale_and_not_a_number(self):
        self.assertEqual(Point("p", "input", 0, "int16", scale=0.1, offset=-40).value([1000]), 60)
        self.assertIsNone(Point("p", "input", 0, "float32").value([0x7FC0, 0]))

    def test_points_that_cannot_be(self):
        for args, kw in ((("p", "coil", 0, "uint16"), {}), (("p", "input", 0, "uint16"), {"bit": 3}),
                         (("p", "input", 0, "bool"), {"bit": 16}), (("p", "input", 65535, "int32"), {}),
                         (("p", "file", 0, "uint16"), {})):
            with self.assertRaises(ValueError):
                Point(*args, **kw)


class PlanTest(unittest.TestCase):
    def test_reads(self):
        points = [Point("a", "input", 10, "uint16"), Point("b", "input", 11, "int32"),
                  Point("c", "input", 15, "uint16"), Point("d", "holding", 11, "uint16"),
                  Point("e", "coil", 3, "bool")]
        self.assertEqual([repr(b) for b in plan(points)], ["coil 3..3", "holding 11..11", "input 10..12",
                                                           "input 15..15"])
        self.assertEqual([repr(b) for b in plan(points, max_gap=2)], ["coil 3..3", "holding 11..11",
                                                                      "input 10..15"])

    def test_limit(self):
        points = [Point(f"p{i}", "input", i, "uint16") for i in range(200)]
        self.assertEqual([repr(b) for b in plan(points)], ["input 0..124", "input 125..199"])


class FakeDevice(socketserver.ThreadingTCPServer):
    """A Modbus TCP server for the tests, after the specification: tables of values, refused addresses, and a
    gateway that can lose its device."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, unit=1):
        self.tables = {1: {}, 2: {}, 3: {}, 4: {}}
        self.refuse = {}  # (function, address) -> exception code
        self.unit, self.requests = unit, 0
        self.connections = []
        super().__init__(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server_address[1]

    def stop(self):
        self.shutdown()
        self.server_close()
        for c in self.connections:  # a device that goes away takes its connections with it
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def answer(self, unit, pdu):
        self.requests += 1
        function, address, count = struct.unpack(">BHH", pdu)
        for a in range(address, address + count):
            if (function, a) in self.refuse:
                return bytes([function | 0x80, self.refuse[(function, a)]])
        if unit != self.unit:
            return bytes([function | 0x80, 11])
        values = [self.tables[function].get(a, 0) for a in range(address, address + count)]
        if function in (1, 2):
            data = bytearray((count + 7) // 8)
            for i, v in enumerate(values):
                data[i // 8] |= (v & 1) << (i % 8)
            return bytes([function, len(data)]) + bytes(data)
        return bytes([function, 2 * count]) + struct.pack(f">{count}H", *values)


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.connections.append(self.request)
        while True:
            try:
                header = self.request.recv(7)
            except OSError:  # the client went away
                return
            if len(header) < 7:
                return
            tid, _, length, unit = struct.unpack(">HHHB", header)
            pdu = self.request.recv(length - 1)
            out = self.server.answer(unit, pdu)
            self.request.sendall(struct.pack(">HHHB", tid, 0, len(out) + 1, unit) + out)


def modbus_config(port, **points):
    return {"type": "modbus", "host": "127.0.0.1", "port": port, "unit": 1, "timeout": 2, "period": 1,
            "word_order": "big", "max_gap": 0,
            "points": {name: dict({"type": None, "bit": None, "word_order": None, "scale": 1.0, "offset": 0.0}, **p)
                       for name, p in points.items()}}


POINTS = {
    "TI-1": {"table": "input", "address": 0, "type": "int16", "scale": 0.1},
    "TI-2": {"table": "input", "address": 1, "type": "int16", "scale": 0.1},
    "P-1": {"table": "input", "address": 10, "type": "float32"},
    "RUN": {"table": "discrete", "address": 4},
    "ALM-3": {"table": "holding", "address": 100, "bit": 3},
}


class SourceTest(unittest.TestCase):
    def setUp(self):
        self.device = FakeDevice()
        self.device.tables[4].update({0: 655, 1: 0xFFF6, 10: 0x4049, 11: 0x0FDB})
        self.device.tables[2][4] = 1
        self.device.tables[3][100] = 0b1000
        self.now = 1000.0
        routes = {"TI-1": ["A"], "TI-2": ["B"], "P-1": ["A", "B"], "RUN": ["A"], "ALM-3": ["B"]}
        self.source = ModbusSource("dev", modbus_config(self.device.port, **POINTS), routes, clock=lambda: self.now)

    def tearDown(self):
        self.source.close()
        self.device.stop()

    def test_poll(self):
        got = self.source.poll({})
        a = {r.data["tag"]: (r.data["value"], r.data["quality"], r.time, r.seq) for r in got["A"] if "tag" in r.data}
        self.assertEqual(set(a), {"TI-1", "P-1", "RUN"})
        self.assertAlmostEqual(a["TI-1"][0], 65.5)
        self.assertAlmostEqual(a["P-1"][0], math.pi, places=6)
        self.assertEqual(a["RUN"][:3], (1.0, GOOD, 1000.0))
        b = {r.data["tag"]: r.data["value"] for r in got["B"] if "tag" in r.data}
        self.assertAlmostEqual(b["TI-2"], -1.0)
        self.assertEqual(b["ALM-3"], 1.0)
        self.assertEqual(got["A"][-1].data, {"cursor": 1000.0})
        self.assertEqual([r.seq for r in got["A"]], [0, 1, 2, 3])
        self.assertEqual(self.device.requests, 4)  # holding 100, input 0..1, input 10..11, discrete 4
        # the next poll goes on numbering from the cursors; the same time is not read twice
        cursors = {"A": {"dev": {"position": 1000.0, "next": 4}}}
        self.assertEqual(self.source.poll(cursors)["A"], [])
        self.now = 1001.0
        self.assertEqual([r.seq for r in self.source.poll(cursors)["A"]], [4, 5, 6, 7])

    def test_refused_read_makes_its_points_bad(self):
        self.device.refuse[(4, 10)] = 2
        with self.assertLogs("sequentialist.monitor", "ERROR"):
            values = self.source.read()
        self.assertEqual(values["P-1"], (None, BAD))
        self.assertEqual(values["TI-1"][1], GOOD)
        del self.device.refuse[(4, 10)]
        self.assertEqual(self.source.read()["P-1"][1], GOOD)

    def test_device_down(self):
        self.device.refuse[(4, 0)] = 11  # the gateway cannot reach the device
        with self.assertRaises(SourceError):
            self.source.poll({})
        del self.device.refuse[(4, 0)]
        self.source.poll({})
        self.device.stop()
        with self.assertRaises(SourceError):
            self.source.poll({})
        self.device = FakeDevice()
        self.source.client.port = self.device.port
        self.assertEqual(self.source.read()["TI-1"], (0.0, GOOD))  # a new connection after the failure

    def test_no_history(self):
        with self.assertRaises(SourceError):
            self.source.history(0, 1)

    def test_record_then_read_as_csv(self):
        with tempfile.TemporaryDirectory() as d:
            recorder = Recorder(d, "dev")
            self.now = 1793491199.75  # a quarter of a second before midnight UTC

            def sleep(seconds):
                self.now += seconds

            polls = record(self.source, recorder, until=1793491201.0, sleep=sleep)
            files = sorted(p.name for p in Path(d).iterdir())
            self.assertEqual(files, ["dev-2026-10-31.csv", "dev-2026-11-01.csv"])  # one file per UTC day
            self.assertEqual(polls, 2)
            csv_source = CsvSource("rec", {"directory": d, "pattern": "dev-*.csv", "delimiter": ",",
                                           "quality_codes": "text",
                                           "columns": {"tag": "tag", "time": "time", "value": "value",
                                                       "quality": "quality"}},
                                   {"TI-1": ["A"], "P-1": ["A"]})
            got = csv_source.history(0, 2e9)
            self.assertEqual([(r.data["tag"], r.time) for r in got],
                             [("P-1", 1793491199.75), ("TI-1", 1793491199.75), ("P-1", 1793491200.75),
                              ("TI-1", 1793491200.75)])
            self.assertAlmostEqual(got[1].data["value"], 65.5)


class ConfigTest(unittest.TestCase):
    def test_plant_with_a_modbus_source(self):
        data = plant_dict()
        uid, unit = next(iter(data["units"].items()))
        unit["source"] = "dev"
        tags = {(t.get("source_tag") or tid) for tid, t in unit["tags"].items()}
        data["sources"]["dev"] = {"type": "modbus", "host": "10.0.0.5",
                                  "points": {t: {"table": "input", "address": i} for i, t in enumerate(sorted(tags))}}
        plant = load_plant(data)
        self.assertEqual(plant.problems(), [])
        point = plant.data["sources"]["dev"]["points"][sorted(tags)[0]]
        self.assertEqual((point["type"], point["scale"]), (None, 1.0))
        data["sources"]["dev"]["points"].pop(sorted(tags)[0])
        data["sources"]["dev"]["points"]["X"] = {"table": "coil", "address": 0, "type": "int16"}
        with self.assertRaises(SpecError) as caught:
            load_plant(data)
        problems = dict(caught.exception.errors)
        self.assertIn("/sources/dev/points/X", problems)
        self.assertTrue(any(p.startswith(f"/units/{uid}/tags/") for p in problems))


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ReferenceServerTest(unittest.TestCase):
    """The client against the server of pymodbus (BSD-3-Clause), an independent implementation, when present."""

    def test_against_pymodbus(self):
        try:
            from pymodbus.datastore import ModbusDeviceContext, ModbusSequentialDataBlock, ModbusServerContext
            from pymodbus.server import ModbusTcpServer
        except ImportError:
            self.skipTest("pymodbus is not installed")
        logging.getLogger("pymodbus").setLevel(logging.ERROR)
        device = ModbusDeviceContext(  # in pymodbus a block at 1 holds PDU address 0
            ir=ModbusSequentialDataBlock(1, [655, 0xFFF6] + [0] * 8 + [0x4049, 0x0FDB]),
            hr=ModbusSequentialDataBlock(1, [0] * 100 + [0b1000]),
            di=ModbusSequentialDataBlock(1, [0, 0, 0, 0, 1]),
            co=ModbusSequentialDataBlock(1, [0] * 8))
        port = _free_port()
        loop = asyncio.new_event_loop()
        servers = []

        async def serve():
            servers.append(ModbusTcpServer(ModbusServerContext(devices={1: device}, single=False),
                                           address=("127.0.0.1", port)))
            await servers[0].serve_forever()

        thread = threading.Thread(target=lambda: loop.run_until_complete(serve()), daemon=True)
        thread.start()
        try:
            source = None
            for _ in range(50):
                try:
                    source = ModbusSource("dev", modbus_config(port, **POINTS), {})
                    values = source.read()
                    break
                except SourceError:
                    source.close()
                    time.sleep(0.1)
            else:
                self.fail("the pymodbus server did not start")
            self.assertAlmostEqual(values["TI-1"][0], 65.5)
            self.assertAlmostEqual(values["TI-2"][0], -1.0)
            self.assertAlmostEqual(values["P-1"][0], math.pi, places=6)
            self.assertEqual((values["RUN"], values["ALM-3"]), ((1.0, GOOD), (1.0, GOOD)))
            with self.assertRaises(ModbusException) as caught:
                source.client.read("input", 50, 10)
            self.assertEqual(caught.exception.code, 2)
            source.close()
        finally:
            asyncio.run_coroutine_threadsafe(servers[0].shutdown(), loop).result(10)
            thread.join(10)
            loop.close()


if __name__ == "__main__":
    unittest.main()
