"""收件核销服务的领域规则单测。"""
import unittest
from decimal import Decimal

from recon.ledger import (Piece, active_bridges, aggregate_window, classify_fragment,
                          effective_rows, effective_switch, reading_delta)


SWITCH = [
    {"id": "M1", "port": "枪-A", "window": "2026-11-20", "known": 0, "from": "10:00",
     "until": "11:30", "meter": "OLD", "epoch": "g18"},
    {"id": "M2", "port": "枪-A", "window": "2026-11-20", "known": 0, "from": "11:30",
     "until": "22:00", "meter": "NEW", "epoch": "g19"},
    {"id": "M3", "port": "枪-B", "window": "2026-11-20", "known": 0, "from": "11:30",
     "until": "22:00", "meter": "NEW", "epoch": "g19"},
    {"id": "M4", "port": "枪-C", "window": "2026-11-20", "known": 0, "from": "10:00",
     "until": "20:00", "meter": "EAST", "epoch": "g1"},
    {"id": "M5", "port": "枪-A", "window": "2026-11-20", "known": 4,
     "interval_uncertain": ["11:29", "11:31"], "replaces": ["M1", "M2"],
     "candidates": [["OLD", "g18"], ["NEW", "g19"]]},
]
BRIDGES = [
    {"id": "B9", "links": [["legacy", "m17"], ["new", "n41"]], "day": "2026-11-20",
     "known": 0, "state": "active", "revision": 1},
    {"id": "B9", "links": [["legacy", "m17"], ["new", "n41"]], "day": "2026-11-20",
     "known": 3, "state": "withdrawn", "revision": 2},
]


class DomainTest(unittest.TestCase):
    def test_split_row_is_attributed_before_uncertain_switch_order(self):
        rules = effective_switch(SWITCH, 2)
        row = {"id": "m17", "day": "2026-11-20", "port": "枪-A", "start": "11:20",
               "end": "11:50", "kwh": "4.80",
               "split": [["OLD", "g18", "11:20", "11:30", "1.50"],
                         ["NEW", "g19", "11:30", "11:50", "3.30"]]}
        pieces = classify_fragment("legacy", row, rules)
        self.assertEqual([(p.meter, p.epoch, p.status) for p in pieces],
                         [("OLD", "g18", "attributed"), ("NEW", "g19", "attributed")])

    def test_split_crossing_uncertain_interval_is_unconfirmed(self):
        rules = effective_switch(SWITCH, 4)
        row = {"id": "m17", "day": "2026-11-20", "port": "枪-A", "start": "11:20",
               "end": "11:50", "kwh": "4.80",
               "split": [["OLD", "g18", "11:20", "11:30", "1.50"],
                         ["NEW", "g19", "11:30", "11:50", "3.30"]]}
        pieces = classify_fragment("legacy", row, rules)
        self.assertTrue(all(p.status == "unconfirmed" for p in pieces))
        self.assertTrue(all("M5" in p.reason for p in pieces))

    def test_window_mismatch_and_three_buckets(self):
        reading = {"revision": "1", "bottoms": [["10:00", "100.00"], ["12:00", "105.00"]]}
        self.assertEqual(reading_delta(reading), Decimal("5.00"))
        pieces = [
            Piece("legacy", "a", "d", "枪-A", "10:00", "11:00", Decimal("1.00"),
                  meter="M", epoch="e", status="attributed"),
            Piece("legacy", "b", "d", "枪-A", "11:00", "11:30", Decimal("2.00"),
                  meter="M", epoch="e", status="unconfirmed"),
        ]
        report = aggregate_window("M", "e", "d", pieces, reading, [])
        self.assertTrue(report.balanced)
        self.assertEqual(report.attributed, Decimal("1.00"))
        self.assertEqual(report.unconfirmed, Decimal("2.00"))
        self.assertEqual(report.meter_only, Decimal("2.00"))  # 余量留在共表窗口

        over = pieces + [Piece("new", "c", "d", "枪-B", "11:30", "12:00", Decimal("9.00"),
                               meter="M", epoch="e", status="attributed")]
        bad = aggregate_window("M", "e", "d", over, reading, [])
        self.assertFalse(bad.balanced)
        self.assertIn("对不平", bad.issue)

    def test_missing_reading_and_cross_epoch_never_merged(self):
        report = aggregate_window("M", "e", "d", [], None, [])
        self.assertFalse(report.balanced)
        self.assertIsNone(report.window_total)
        # 旧表清零（末值小于首值）不能与新表读数拼成累计段。
        self.assertIsNone(reading_delta({"bottoms": [["t", "5.00"], ["t", "0.00"]]}))

    def test_bridge_withdrawal_only_invalidates_named_bridge(self):
        self.assertEqual(set(active_bridges(BRIDGES, 2)), {"B9"})
        self.assertEqual(active_bridges(BRIDGES, 4), {})

    def test_archive_rows_replace_live_view_and_retract_is_kept_out(self):
        ds = {"live_rows": [{"id": "x", "kwh": "1"}], "retracted": ["y"],
              "archive_complete": True, "archive_rows": [{"id": "z", "kwh": "2"}]}
        self.assertEqual([r["id"] for r in effective_rows(ds)], ["z"])
        ds["archive_complete"] = False
        self.assertEqual([r["id"] for r in effective_rows(ds)], ["x"])

    def test_switch_rule_must_match_window_day(self):
        # 21 日的账片不能错配到 20 日的施工单（取 M5 生效前的确定性规则集）。
        row = {"id": "t02", "day": "2026-11-21", "port": "枪-C", "start": "09:00",
               "end": "11:00", "kwh": "2.40"}
        pieces = classify_fragment("new", row, effective_switch(SWITCH, 0))
        self.assertEqual(len(pieces), 1)
        self.assertEqual(pieces[0].status, "unconfirmed")
        self.assertIsNone(pieces[0].meter)


if __name__ == "__main__":
    unittest.main()
