# -*- coding: utf-8 -*-
"""关键业务结论的可执行核对。运行：python3 -m unittest discover -s tests -v"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import relay_audit as ra  # noqa: E402


class PathReconstructionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = ra.load_records(ROOT / "data" / "records.json")
        cls.views = {v["request"]: v for v in ra.all_request_views(cls.payload)}

    def test_transcription_is_not_a_new_receipt(self):
        # R1 两支分别 d4/d5 完成；转抄在 d5/d6，但任一依赖下办结日取 d4。
        self.assertEqual(self.views["R1"]["close_day"], 4)
        self.assertEqual(self.views["R3"]["close_day"], 5)  # 全部取 max

    def test_and_join_rejected_branch_cannot_close(self):
        for rid in ("R0", "R6", "R21", "R27", "R42"):
            self.assertEqual(self.views[rid]["status"],
                             "broken_rejected_branch", rid)
            self.assertIsNone(self.views[rid]["close_day"])
        # 记录末日 d55：前四户 deadline 已过；R42 deadline57，已确定无法办结但 d55 尚未逾期
        for rid in ("R0", "R6", "R21", "R27"):
            self.assertTrue(self.views[rid]["overdue"], rid)
        self.assertFalse(self.views["R42"]["overdue"])
        self.assertEqual(self.views["R42"]["overdue_since"], 58)

    def test_or_join_survives_one_rejection(self):
        self.assertEqual(self.views["R13"]["close_day"], 18)
        self.assertEqual(self.views["R14"]["close_day"], 19)
        self.assertEqual(self.views["R20"]["close_day"], 25)
        self.assertEqual(self.views["R34"]["close_day"], 39)
        self.assertEqual(self.views["R41"]["close_day"], 46)

    def test_amendments_make_r5_r7_all_join(self):
        self.assertEqual(self.views["R5"]["join_effective"], "全部")
        self.assertEqual(self.views["R7"]["join_effective"], "全部")
        self.assertIsNone(self.views["R5"]["close_day"])
        self.assertIsNone(self.views["R7"]["close_day"])
        # 不应用补记时，R5 凭 R5-0C 在 d34 即办结
        old = ra.request_view(self.payload, "R5", use_amendment=False)
        self.assertEqual(old["join_effective"], "任一")
        self.assertEqual(old["close_day"], 34)

    def test_overdue_starts_day_after_deadline(self):
        # R5/R25 deadline38 -> d39 起逾期；R8/R28 deadline37 -> d38；R7 d41
        self.assertEqual(self.views["R5"]["overdue_since"], 39)
        self.assertEqual(self.views["R25"]["overdue_since"], 39)
        self.assertEqual(self.views["R8"]["overdue_since"], 38)
        self.assertEqual(self.views["R28"]["overdue_since"], 38)
        self.assertEqual(self.views["R7"]["overdue_since"], 41)

    def test_snapshot_counts(self):
        self.assertEqual(ra.snapshot(self.payload, 20)["overdue"], ["R0", "R6"])
        s27 = ra.snapshot(self.payload, 27)
        self.assertEqual(s27["overdue"], ["R0", "R6"])
        self.assertIn("R21", s27["broken_not_yet_overdue"])
        s36 = ra.snapshot(self.payload, 36)
        self.assertEqual(s36["overdue"], ["R0", "R6", "R21"])
        self.assertIn("R27", s36["due_today_but_unconfirmed"])

    def test_shared_evidence_only_when_listed(self):
        r4 = self.views["R4"]
        shared_evids = {u["evidence"] for u in r4["shared_evidence_usable"]}
        self.assertIn("R5-0C", shared_evids)
        # 户1 的 R2/R3 没有共用凭据，不得凭户号共用
        self.assertEqual(self.views["R2"]["shared_evidence_usable"], [])

    def test_pauses_do_not_overlap_named_request_wait(self):
        ledger = {x["request"]: x for x in ra.four_ledger(self.payload)}
        for rid in ("R5", "R8"):
            for p in ledger[rid]["household_agreement"]["pauses"]:
                self.assertEqual(p["overlap_days"], 0)
                self.assertFalse(p["applies_to_wait"])

    def test_waiting_parties_at_36(self):
        w = {x["request"]: x for x in ra.waiting_asof(self.payload, 36)}
        parties = {x["branch"]: x["party"] for x in w["R7"]["waiting_on"]}
        self.assertEqual(parties, {"R7-0": "站0", "R7-1": "站1"})
        self.assertEqual(w["R8"]["waiting_on"][0]["party"], "站1")
        self.assertTrue(w["R28"]["structural_gaps"])

    def test_station_average_hides_open_tails(self):
        m = ra.phase_metrics(self.payload)["stations"]
        self.assertEqual(m["站0"]["handle_to_complete_mean"], 2)
        self.assertEqual(m["站1"]["handle_to_complete_mean"], 2)
        self.assertEqual(m["站1"]["open_branches_excluded_from_average"], 4)

    def test_capacity_conflict_station1(self):
        pl = ra.proposal_ledger(self.payload)
        s1 = next(c for c in pl["capacity_conflicts"] if c["provider"] == "站1")
        self.assertEqual(s1["optional_slots"], 1)
        self.assertEqual(s1["contender_count"], 3)
        self.assertTrue(s1["conflict"])
        s0 = next(c for c in pl["capacity_conflicts"] if c["provider"] == "站0")
        self.assertFalse(s0["conflict"])

    def test_proposal_limits_arithmetic(self):
        pl = ra.proposal_ledger(self.payload)["proposals"]
        self.assertEqual(pl["试案东"]["extra_total"], 2)
        self.assertEqual(pl["试案西"]["extra_total"], 2)
        self.assertEqual(pl["交叉"]["extra_total"], 3)
        self.assertFalse(pl["交叉"]["within_weeks_total_cap_reading"])
        for name in ("试案东", "试案西", "交叉"):
            self.assertTrue(pl[name]["within_week37_cap"])
            self.assertTrue(pl[name]["within_week38_cap"])

    def test_r8_draft_cannot_beat_deadline_and_needs_student_consent(self):
        pl = ra.proposal_ledger(self.payload)
        z2 = next(g for g in pl["draft_gates"] if g["option"] == "Z2")
        self.assertFalse(z2["calendar_ontime_possible"])
        self.assertEqual(z2["consent_required_from"], "学生")
        self.assertFalse(z2["consent_on_record"])

    def test_pilot_comparable_busy_weeks(self):
        pe = ra.pilot_evidence(self.payload)
        self.assertEqual(pe["busy_weeks"]["加开"], [1, 6, 9, 14])
        self.assertEqual(pe["busy_weeks"]["普通"], [4, 11, 16])


if __name__ == "__main__":
    unittest.main()
