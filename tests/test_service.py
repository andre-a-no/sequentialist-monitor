# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from sequentialist.core import SQLiteStorage
from sequentialist.monitor.api import Api
from sequentialist.monitor.config import load_plant
from sequentialist.monitor.notify import Announce, EmailSink
from sequentialist.monitor.service import SERVICE_ACTOR, Service
from sequentialist.monitor.sources import SqlSource, routes

from .synthetic import T0, WINDOW, baseline, plant, records

QUERY = "SELECT tag, ts, value, q FROM s WHERE ts >= ? AND ts < ?"
CYCLE = 5.0


class Historian:
    """A historian in SQLite, filled with the synthetic unit's readings up to a time."""

    def __init__(self, inputs):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.db.execute("CREATE TABLE s (tag TEXT, ts REAL, value REAL, q INTEGER)")
        self.pending = sorted(inputs, key=lambda r: r.time)
        self.broken = False

    def fill(self, until):
        rows = [(r.data["tag"], r.time, r.data["value"], 192) for r in self.pending if r.time < until]
        self.pending = [r for r in self.pending if r.time >= until]
        self.db.executemany("INSERT INTO s VALUES (?, ?, ?, ?)", rows)

    def connect(self):
        if self.broken:
            raise sqlite3.OperationalError("the historian does not answer")
        return Connection(self.db)


class Connection:
    """The service's connection to the historian: closing it leaves the historian's database open."""

    def __init__(self, db):
        self.db = db

    def cursor(self):
        return self.db.cursor()

    def close(self):
        pass


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.plant = plant()
        self.baseline = baseline(self.plant)
        self.historian = Historian(records(260, faults={"TI-1": (150, 5.0)}))
        self.announced = []

    def tearDown(self):
        self.historian.db.close()
        self.dir.cleanup()

    def service(self, plant_spec=None, **kwargs):
        p = plant_spec or self.plant
        cfg = dict(p.data["sources"]["hist"], query=QUERY, time_param="epoch", paramstyle="qmark")
        source = SqlSource("hist", cfg, routes(p.data, "hist"), connect=self.historian.connect)
        sinks = [Announce(self.announced.append, ["urgent", "high", "medium", "low"])]
        return Service(p, self.baseline, storage=SQLiteStorage(f"{self.dir.name}/monitor.db"),
                       sources={"hist": source}, sinks=sinks, clock=lambda: self.now, environ={}, **kwargs)

    def run_until(self, service, end):
        while self.now < end:
            self.now += CYCLE
            self.historian.fill(self.now)
            service.cycle(self.now)

    def test_cycles_detect_restart_and_go_on(self):
        self.now = T0
        s = self.service()
        self.run_until(s, T0 + 200 * WINDOW)
        h = s.handler("P-1")
        self.assertTrue(h.alarms["TI-1.mean"].active)
        announced = [e for e in self.announced if e.basis["source"] == "TI-1.mean"]
        self.assertEqual(announced[0].decision, "activated")
        self.assertGreaterEqual(announced[0].basis["time"], T0 + 150 * WINDOW)
        self.assertTrue(all(e.basis["notify"] for e in self.announced))
        snap = s.snapshot["units"]["P-1"]
        self.assertEqual(snap["alarms"]["TI-1.mean"]["state"], "unacknowledged")
        self.assertIsNotNone(snap["features"]["TI-1.mean"]["last"]["residual"])
        task_id, n = s.tasks["P-1"].task_id, len(s.storage_audit(s.tasks["P-1"].task_id))
        # the process ends without closing the tasks (no last checkpoint), and starts again on the same storage
        for task in s.tasks.values():
            task._lock.release()
        s.storage.close()
        again = self.service()
        self.assertEqual(again.tasks["P-1"].task_id, task_id)
        self.assertGreaterEqual(len(again.storage_audit(task_id)), n)
        self.run_until(again, T0 + 260 * WINDOW)
        h = again.handler("P-1")
        self.assertEqual(h.counters, {"late": 0, "unknown_tag": 0, "untimed": 0})
        self.assertEqual(again.tasks["P-1"].counters["duplicates"], 0)
        seen = again.tasks["P-1"].counters["records"]
        again.close()
        # every reading was taken once: 2 tags * 5 readings * windows before the last poll, and the cursors
        readings = sum(1 for r in records(260) if r.time < self.now - 0)
        self.assertGreaterEqual(seen, readings)

    def test_a_source_that_cannot_be_read(self):
        self.now = T0
        s = self.service()
        self.run_until(s, T0 + 20 * WINDOW)
        self.historian.broken = True
        s.sources["hist"].close()
        with self.assertLogs("sequentialist.monitor", "ERROR"):
            self.run_until(s, T0 + 22 * WINDOW)
        self.assertTrue(s.handler("P-1").alarms["@hist"].active)
        self.assertIn("does not answer", s.snapshot["sources"]["hist"]["error"])
        self.historian.broken = False
        self.run_until(s, T0 + 25 * WINDOW)
        self.assertFalse(s.handler("P-1").alarms["@hist"].active)
        decisions = [e.decision for e in self.announced if e.basis["source"] == "@hist"]
        self.assertEqual(decisions, ["activated", "returned"])
        self.assertEqual(self.announced[0].basis["priority"], "high")
        # the readings of the outage were read once the source answered again: no window was lost
        self.assertEqual(s.handler("P-1").bad, {"TI-1": 0, "VI-1": 0})
        s.close()

    def test_commands_from_another_thread(self):
        self.now = T0
        s = self.service()
        self.run_until(s, T0 + 200 * WINDOW)
        results = {}

        def operator():
            results["ack"] = s.submit("P-1", "ack", {"source": "TI-1.mean"}, "op-17", "seen")
            results["again"] = s.submit("P-1", "ack", {"source": "TI-1.mean"}, "op-17", "again")
            results["nowhere"] = s.submit("P-9", "ack", {"source": "*"}, "op-17", "x")

        thread = threading.Thread(target=operator)
        thread.start()
        while thread.is_alive():
            s._run_commands()
        self.assertEqual(results["ack"].status, 202)
        self.assertEqual(results["ack"].result["entries"][0]["transition"], "acknowledged")
        self.assertEqual((results["again"].status, results["again"].result), (409, {"error": "already acknowledged"}))
        self.assertEqual(results["nowhere"].status, 404)
        s.close()

    def test_actors_are_pseudonyms_with_a_key(self):
        data = self.plant.to_json()
        data["actor_key_env"] = "ACTOR_KEY"
        p = load_plant(data)
        self.now = T0
        cfg = dict(p.data["sources"]["hist"], query=QUERY, time_param="epoch")
        s = Service(p, baseline(p), storage=SQLiteStorage(f"{self.dir.name}/m.db"), sinks=[],
                    sources={"hist": SqlSource("hist", cfg, routes(p.data, "hist"), connect=self.historian.connect)},
                    clock=lambda: self.now, environ={"ACTOR_KEY": "k" * 32})
        self.run_until(s, T0 + 200 * WINDOW)
        done = threading.Thread(target=lambda: s.submit("P-1", "ack", {"source": "*"}, "Иванов И. И.", "seen"))
        done.start()
        while done.is_alive():
            s._run_commands()
        actors = {e.actor for e in s.storage_audit(s.tasks["P-1"].task_id) if e.actor}
        self.assertEqual(len(actors), 1)
        self.assertRegex(actors.pop(), r"^[0-9a-f]{64}$")
        s.close()

    def test_a_new_spec_carries_shelving_over(self):
        self.now = T0
        s = self.service()
        self.run_until(s, T0 + 30 * WINDOW)
        t = threading.Thread(target=lambda: s.submit("P-1", "shelve", {"source": "VI-1.mean", "duration": 3600},
                                                     "op-1", "maintenance"))
        t.start()
        while t.is_alive():
            s._run_commands()
        old = s.tasks["P-1"].task_id
        s.close()
        changed = plant(clear_after=40)  # another off-delay: another spec, another task
        self.baseline = baseline(changed)
        s = self.service(changed)
        self.assertNotEqual(s.tasks["P-1"].task_id, old)
        alarm = s.handler("P-1").alarms["VI-1.mean"]
        self.assertEqual(alarm.shelving, "TimedShelved")
        carried = [e for e in s.storage_audit(s.tasks["P-1"].task_id) if e.actor]
        self.assertEqual(carried[0].actor, SERVICE_ACTOR)
        self.assertIn(old, carried[0].reason)
        s.close()


class ApiTest(unittest.TestCase):
    def test_status_and_commands_over_http(self):
        test = ServiceTest()
        test.setUp()
        try:
            test.now = T0
            s = test.service()
            test.run_until(s, T0 + 200 * WINDOW)
            api = Api(s, "127.0.0.1", 0, "secret-token")
            api.start()
            stop = threading.Event()

            def service_thread():  # what Service.run does between cycles
                while not stop.wait(0.01):
                    s._run_commands()

            worker = threading.Thread(target=service_thread)
            worker.start()
            base = "http://{}:{}".format(*api.address)

            def call(method, path, body=None, token=None):
                data = json.dumps(body).encode() if body is not None else None
                request = urllib.request.Request(base + path, data=data, method=method)
                if token:
                    request.add_header("Authorization", f"Bearer {token}")
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        return response.status, json.loads(response.read())
                except urllib.error.HTTPError as error:
                    with error:
                        return error.code, json.loads(error.read())

            try:
                self.assertEqual(call("GET", "/health")[1]["ok"], True)
                _, units = call("GET", "/units")
                self.assertEqual(units["P-1"]["alarms"], 1)
                _, alarms = call("GET", "/alarms")
                self.assertEqual([(a["unit"], a["source"], a["state"]) for a in alarms],
                                 [("P-1", "TI-1.mean", "unacknowledged")])
                command = {"command": "ack", "args": {"source": "TI-1.mean"}, "actor": "op-17", "reason": "seen"}
                self.assertEqual(call("POST", "/units/P-1/commands", command)[0], 401)
                self.assertEqual(call("POST", "/units/P-1/commands", command, "wrong")[0], 401)
                self.assertEqual(call("POST", "/units/P-1/commands", command, "secret-token")[0], 202)
                self.assertEqual(call("POST", "/units/P-1/commands", command, "secret-token")[0], 409)
                self.assertEqual(call("POST", "/units/P-1/commands", {"command": "ack"}, "secret-token")[0], 400)
                self.assertEqual(call("GET", "/units/P-1")[1]["alarms"]["TI-1.mean"]["state"], "acknowledged")
                _, found = call("GET", f"/kpi?from={T0}&to={T0 + 200 * WINDOW}")
                self.assertEqual(found["main"]["annunciated"], 1)
                self.assertEqual(call("GET", "/nowhere")[0], 404)
            finally:
                stop.set()
                worker.join()
                api.close()
                s.close()
        finally:
            test.tearDown()


class EmailTest(unittest.TestCase):
    def test_message(self):
        sent = []
        sink = EmailSink("smtp.example.org", 25, "monitor@example.org", ["shift@example.org"], send=sent.append)
        from sequentialist.core import AuditEntry

        basis = {"priority": "high", "message": "Pump P-1: Bearing temperature, window mean: activated",
                 "source_name": "s.a.P-1.TI-1", "condition": "mean", "transition": "activated",
                 "state": {"before": "normal", "after": "unacknowledged"}, "severity": 700, "guarantee": "model",
                 "notify": True}
        sink(AuditEntry(3, "t", "x", "decision", "activated", T0, basis, (), None, None, "", "h"))
        sink.close()
        self.assertEqual(sent[0]["Subject"], "[high] Pump P-1: Bearing temperature, window mean: activated")
        body = sent[0].get_content()
        self.assertIn("Guarantee: model", body)
        self.assertIn("not a safety instrumented system", body)


if __name__ == "__main__":
    unittest.main()
