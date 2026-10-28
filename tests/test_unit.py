# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
import tempfile
import unittest

from sequentialist.core import Engine, Record, SQLiteStorage, replay, verify_audit
from sequentialist.monitor.unit import KIND, UnitHandler

from .synthetic import T0, WINDOW, records, unit_spec


def run(spec, inputs, **kwargs):
    task = replay(KIND, spec.data, UnitHandler(spec.data), inputs, task_id="u", **kwargs)
    try:
        return task, task.audit()
    finally:
        task.close()


def decisions(entries, transition=None, source=None):
    return [e for e in entries if e.kind in ("decision", "action")
            and (transition is None or e.decision == transition)
            and (source is None or e.basis.get("source") == source)]


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spec = unit_spec()

    def test_streams(self):
        handler = UnitHandler(self.spec.data)
        self.assertEqual(sorted(handler.streams()), ["TI-1.mean.down", "TI-1.mean.model", "TI-1.mean.up",
                                                     "VI-1.mean.detector", "VI-1.mean.model"])

    def test_a_shift_is_detected_and_reported_as_an_opc_ua_event(self):
        # a step of 3 innovation sigmas in the bearing temperature from window 300
        _, entries = run(self.spec, records(400, faults={"TI-1": (300, 3.0)}))
        fired = decisions(entries, "activated", "TI-1.mean")
        self.assertTrue(fired)
        late = [e for e in fired if e.basis["time"] >= T0 + 300 * WINDOW]
        self.assertTrue(late, "the shift was not detected")
        first = late[0]
        self.assertLessEqual(first.basis["time"], T0 + 320 * WINDOW)
        b = first.basis
        self.assertEqual(b["source_name"], "test.pumping.P-1.TI-1")
        self.assertEqual((b["priority"], b["severity"], b["console"]), ("high", 700, "main"))
        self.assertEqual(b["opc_ua"], {"ActiveState": True, "AckedState": False, "ShelvingState": "Unshelved",
                                       "SuppressedState": False, "OutOfServiceState": False})
        self.assertEqual(b["state"], {"before": "normal", "after": "unacknowledged"})
        self.assertTrue(b["notify"])
        self.assertEqual(b["streams"], ["up"])
        self.assertEqual(b["guarantee"], "approximate")  # the CUSUM threshold came from a simulation
        # the evidence: the model and the detector that fired, with their procedures and guarantees
        self.assertEqual([(e["stream"], e["guarantee"]) for e in first.evidence],
                         [("TI-1.mean.model", "model"), ("TI-1.mean.up", "exact")])

    def test_replay_is_deterministic(self):
        inputs = records(200, faults={"TI-1": (120, 3.0)})
        _, a = run(self.spec, inputs)
        _, b = run(self.spec, list(inputs))
        self.assertEqual([e.hash for e in a], [e.hash for e in b])
        self.assertEqual(verify_audit([e.to_json() for e in a], "u"), len(a))

    def test_crash_and_reopen_give_the_uninterrupted_task(self):
        inputs = records(150, faults={"TI-1": (80, 3.0)})
        _, expected = run(self.spec, inputs)
        with tempfile.TemporaryDirectory() as d:
            for cut in (1001, 2003):
                path = f"{d}/{cut}.db"
                engine = Engine(SQLiteStorage(path), clock="event", checkpoint_every=97)
                task = engine.create("u", KIND, self.spec.data, UnitHandler(self.spec.data))
                for i in range(0, cut, 50):
                    task.ingest(inputs[i:min(i + 50, cut)])
                task._lock.release()  # the process dies: no close, no last checkpoint
                engine.storage.close()
                with SQLiteStorage(path) as storage:
                    task = Engine(storage, clock="event", checkpoint_every=97).open("u", UnitHandler)
                    for i in range(cut, len(inputs), 50):
                        task.ingest(inputs[i:i + 50])
                    got = task.audit()
                    task.close()
                self.assertEqual([e.hash for e in got], [e.hash for e in expected])

    def test_a_persistent_shift_keeps_the_alarm_active(self):
        # Under a step of 4 sigma the residual of the AR(1) model keeps a mean of (1 - phi) * 4 = 1.2 sigma, and
        # the restarted CUSUM fires again every few windows: with an off-delay (clear_after) longer than that the
        # alarm stays active and the operator is told once, not at every refire.
        _, entries = run(self.spec, records(400, faults={"TI-1": (100, 4.0)}))
        after = [e for e in decisions(entries, source="TI-1.mean") if e.basis["time"] > T0 + 100 * WINDOW]
        self.assertEqual(after[0].decision, "activated")
        self.assertEqual({e.decision for e in after[1:]}, {"refired"})
        self.assertFalse(any(e.basis["notify"] for e in after[1:]))

    def test_a_short_off_delay_makes_the_alarm_flap(self):
        # the reason for the default: with clear_after = 3 the same step returns and re-activates again and again
        spec = unit_spec(clear_after=3)
        _, entries = run(spec, records(400, faults={"TI-1": (100, 4.0)}))
        after = [e.decision for e in decisions(entries, source="TI-1.mean") if e.basis["time"] > T0 + 100 * WINDOW]
        self.assertGreater(after.count("returned"), 3)

    def test_a_shift_that_ends_returns_to_normal(self):
        inputs = records(400, faults={"TI-1": (100, 4.0)})
        # the step lasts 30 windows: put the readings back on the normal path after window 130
        normal = {(r.data["tag"], r.time): r.data["value"] for r in records(400)}
        inputs = [r if r.data["tag"] != "TI-1" or r.time < T0 + 130 * WINDOW
                  else Record(r.source, r.seq, dict(r.data, value=normal[("TI-1", r.time)]), r.time) for r in inputs]
        _, entries = run(self.spec, inputs)
        returned = [e for e in decisions(entries, "returned", "TI-1.mean") if e.basis["time"] > T0 + 130 * WINDOW]
        self.assertTrue(returned)
        self.assertEqual(returned[0].basis["state"], {"before": "unacknowledged", "after": "returned_unacknowledged"})


class WindowTest(unittest.TestCase):
    def test_late_unknown_and_bad_readings(self):
        spec = unit_spec()
        inputs = records(30, quality={"TI-1": {10, 11, 12, 13}})
        inputs.append(Record("hist", 10**6, {"tag": "TI-1", "value": 60.0}, time=T0))  # its window is closed
        inputs.append(Record("hist", 10**6 + 1, {"tag": "XX-9", "value": 1.0}, time=T0 + 300))
        task, entries = run(spec, inputs)
        state = task.handler.state()
        self.assertEqual(state["counters"], {"late": 1, "unknown_tag": 1, "untimed": 0})
        # three bad windows in a row raise the quality alarm, the next good window clears it
        quality = decisions(entries, source="TI-1.quality")
        self.assertEqual([(e.decision, e.basis["time"]) for e in quality],
                         [("activated", T0 + 13 * WINDOW), ("returned", T0 + 15 * WINDOW)])

    def test_out_of_range_is_bad(self):
        spec = unit_spec()
        inputs = [r if r.data["tag"] != "TI-1" else Record(r.source, r.seq, dict(r.data, value=500.0), r.time)
                  for r in records(10)]
        _, entries = run(spec, inputs)
        self.assertTrue(decisions(entries, "activated", "TI-1.quality"))

    def test_windows_close_on_the_watermark_without_readings(self):
        spec = unit_spec()
        inputs = [*records(20), T0 + 40 * WINDOW]  # the source falls silent; time passes
        task, entries = run(spec, inputs)
        self.assertEqual(task.handler.next_window, int(T0 / WINDOW) + 40)
        self.assertEqual(len(decisions(entries, "activated", "TI-1.quality")), 1)


class RunningTest(unittest.TestCase):
    def test_a_stopped_unit_suppresses_its_alarms_and_restarts_its_models(self):
        spec = unit_spec(running=True)
        flags = [True] * 60 + [False] * 20 + [True] * 60
        task, entries = run(spec, records(140, running=flags, faults={"TI-1": (60, 6.0)}))
        suppressed = decisions(entries, "suppressed", "TI-1.mean")
        self.assertEqual([e.basis["time"] for e in suppressed], [T0 + 61 * WINDOW])
        self.assertEqual([e.basis["time"] for e in decisions(entries, "unsuppressed", "TI-1.mean")],
                         [T0 + 81 * WINDOW])
        segments = [e for e in task.audit() if e.kind == "segment" and e.basis["reason"] == "unit stopped"]
        self.assertTrue(segments)
        # nothing is fed while the unit is stopped: no alarm between its stop and its start
        quiet = [e for e in decisions(entries) if T0 + 61 * WINDOW < e.basis["time"] < T0 + 81 * WINDOW]
        self.assertEqual([e.decision for e in quiet], [])


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.spec = unit_spec()
        self.handler = UnitHandler(self.spec.data)

    def fired_task(self):
        task = replay(KIND, self.spec.data, UnitHandler(self.spec.data), records(200, faults={"TI-1": (100, 5.0)}),
                      task_id="u")
        self.assertTrue(task.handler.alarms["TI-1.mean"].active)
        return task

    def test_ack_and_refusals(self):
        task = self.fired_task()
        try:
            h = task.handler
            self.assertIsNone(h.check("ack", {"source": "TI-1.mean"}))
            task.command("ack", {"source": "TI-1.mean"}, actor="op-17", reason="seen")
            self.assertEqual(h.check("ack", {"source": "TI-1.mean"}), "already acknowledged")
            self.assertEqual(h.check("ack", {"source": "nowhere"}), "no alarm source 'nowhere'")
            self.assertEqual(h.check("shelve", {"source": "*"}), "only ack applies to every source at once")
            # a refused command is recorded, it does not fail the task
            task.command("ack", {"source": "TI-1.mean"}, actor="op-17", reason="again")
            last = task.audit()[-1]
            self.assertEqual((last.kind, last.decision, last.actor), ("action", "refused", "op-17"))
            self.assertIsNone(task.failed)
            acks = decisions(task.audit(), "acknowledged", "TI-1.mean")
            self.assertEqual(acks[0].basis["state"], {"before": "unacknowledged", "after": "acknowledged"})
        finally:
            task.close()

    def test_timed_shelving_expires_on_the_data_clock(self):
        task = self.fired_task()
        try:
            now = task.watermark
            task.command("shelve", {"source": "TI-1.mean", "duration": 600}, actor="op-17", reason="maintenance")
            alarm = task.handler.alarms["TI-1.mean"]
            self.assertEqual((alarm.shelving, alarm.isa_state()), ("TimedShelved", "shelved"))
            task.advance(now + 599)
            self.assertEqual(alarm.shelving, "TimedShelved")
            task.advance(now + 600)
            self.assertEqual(alarm.shelving, "Unshelved")
            self.assertEqual(task.audit()[-1].decision, "unshelved")
        finally:
            task.close()

    def test_one_shot_shelving_and_service(self):
        task = self.fired_task()
        try:
            h = task.handler
            self.assertIn("duration must be", h.check("shelve", {"source": "TI-1.mean", "duration": 10**9}))
            task.command("shelve", {"source": "TI-1.mean", "duration": "oneshot"}, actor="a", reason="b")
            self.assertEqual(h.alarms["TI-1.mean"].shelving, "OneShotShelved")
            task.command("remove_from_service", {"source": "TI-1"}, actor="a", reason="sensor replaced")
            self.assertTrue(all(h.alarms[k].out_of_service for k in ("TI-1.mean", "TI-1.quality")))
            self.assertEqual(h.check("shelve", {"source": "TI-1.mean", "duration": 60}), "out of service")
            task.command("return_to_service", {"source": "TI-1"}, actor="a", reason="sensor replaced")
            self.assertFalse(h.alarms["TI-1.mean"].out_of_service)
            self.assertEqual(h.fed["TI-1.mean"], {"scale": None, "seen": 0})  # its model starts afresh
        finally:
            task.close()


if __name__ == "__main__":
    unittest.main()
