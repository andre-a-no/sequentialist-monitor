# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Novikov Laboratories LLC
# Commercial licenses for use outside the AGPL: see COMMERCIAL.md
import unittest

from sequentialist.core import AuditEntry
from sequentialist.monitor.kpi import kpi

T = 1793491200.0


def entry(n, t, decision, source="TI-1", condition="mean", active=True, notify=True, priority="high",
          kind="decision", actor=None, reason=None, chattering=False):
    basis = {"source_name": f"s.a.u.{source}", "condition": condition, "console": "main", "priority": priority,
             "time": t, "notify": notify, "chattering": chattering,
             "opc_ua": {"ActiveState": active, "AckedState": not active}}
    return AuditEntry(n, "u", "x", kind, decision, t, basis, (), actor, reason, "", "")


class KpiTest(unittest.TestCase):
    def test_counts_by_hand(self):
        entries = [
            # interval 0: three alarms, one of them silenced (shelved), and a refire that is not annunciated
            entry(0, T + 10, "activated"),
            entry(1, T + 20, "activated", source="TI-2", priority="low"),
            entry(2, T + 30, "activated", source="TI-3", notify=False),
            entry(3, T + 40, "refired", notify=False),
            entry(4, T + 50, "returned", source="TI-2", active=False),
            # interval 2: a flood of 10 alarms
            *[entry(5 + i, T + 1200 + i, "activated", source=f"X-{i}", priority="medium") for i in range(10)],
            *[entry(15 + i, T + 1300 + i, "returned", source=f"X-{i}", active=False) for i in range(10)],
            # a quality alarm and an action
            entry(25, T + 1500, "activated", source="TI-4", condition="quality", chattering=True),
            entry(26, T + 1600, "shelved", source="TI-1", kind="action", actor="op-1", reason="maintenance"),
        ]
        out = kpi(entries, T, T + 1800, flood=10, stale_after=1000, budget_per_hour=3.0)["main"]
        # annunciated: TI-1 and TI-2 in the first interval, ten and the quality alarm in the third
        self.assertEqual(out["annunciated"], 13)
        self.assertEqual(out["per_10_minutes"], {"mean": 13 / 3, "max": 11, "flood_share": 1 / 3})
        # TI-1 active since T + 10 and TI-3 since T + 30, both longer than 1000 s at T + 1800; TI-4 not
        self.assertEqual(out["stale"], ["s.a.u.TI-1/mean", "s.a.u.TI-3/mean"])
        self.assertEqual(out["chattering"], ["s.a.u.TI-4/quality"])
        self.assertEqual(out["top_sources"][0]["alarms"], 1)
        self.assertAlmostEqual(out["priorities"]["medium"], 10 / 13)
        self.assertEqual(out["actions"], [{"time": T + 1600, "source": "s.a.u.TI-1/mean", "action": "shelved",
                                           "actor": "op-1", "reason": "maintenance"}])
        # detector fires, silenced or not: 13 activations (TI-1..3, X-0..9) and a refire, over half an hour
        self.assertAlmostEqual(out["detector_fires_per_hour"], (13 + 1) / 0.5)
        self.assertEqual(out["guarantee"], "none")

    def test_period(self):
        with self.assertRaises(ValueError):
            kpi([], T, T)


if __name__ == "__main__":
    unittest.main()
