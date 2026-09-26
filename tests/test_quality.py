import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine, evaluate_report_quality, event_stage
from src.service import DomainService


def event_data(reports):
    return {
        "title": "Event-Q",
        "origin_time": "2026-01-01T00:00:00Z",
        "location": "Region-Q",
        "reports": reports,
    }


class QualityRulesTest(unittest.TestCase):
    def test_duplicate_station_keeps_smaller_time_offset(self):
        quality = evaluate_report_quality([
            {"station": "A", "time_offset": 50, "distance_km": 1.0},
            {"station": "A", "time_offset": 10, "distance_km": 2.0},
        ])
        accepted = quality["accepted_reports"]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["time_offset"], 10)
        self.assertEqual(quality["metrics"]["duplicate_count"], 1)
        dropped = quality["duplicate_reports"][0]
        self.assertEqual(dropped["time_offset"], 50)

    def test_tie_keeps_first_report(self):
        quality = evaluate_report_quality([
            {"station": "A", "time_offset": 10, "distance_km": 1.0},
            {"station": "A", "time_offset": -10, "distance_km": 2.0},
        ])
        self.assertEqual(len(quality["accepted_reports"]), 1)
        self.assertEqual(quality["accepted_reports"][0]["distance_km"], 1.0)

    def test_suspect_thresholds(self):
        quality = evaluate_report_quality([
            {"station": "A", "time_offset": 120, "distance_km": 1.0},   # 边界内
            {"station": "B", "time_offset": 121, "distance_km": 1.0},   # 超 120 秒
            {"station": "C", "time_offset": 5, "distance_km": 3.1},     # 超 3 公里
        ])
        suspects = {(r["station"]): r for r in quality["suspect_reports"]}
        self.assertEqual(set(suspects), {"B", "C"})
        self.assertEqual(quality["metrics"]["valid_count"], 1)
        self.assertEqual(quality["metrics"]["suspect_count"], 2)
        self.assertFalse(quality["review_gate"]["passes"])

    def test_gate_passes_with_three_valid_and_close_average(self):
        quality = evaluate_report_quality([
            {"station": "A", "time_offset": 1, "distance_km": 1.0},
            {"station": "B", "time_offset": 2, "distance_km": 2.0},
            {"station": "C", "time_offset": 3, "distance_km": 3.0},
        ])
        self.assertEqual(quality["metrics"]["average_distance_km"], 2.0)
        self.assertTrue(quality["review_gate"]["passes"])

    def test_average_distance_over_two_kilometers_blocks(self):
        quality = evaluate_report_quality([
            {"station": "A", "time_offset": 1, "distance_km": 1.1},
            {"station": "B", "time_offset": 2, "distance_km": 2.0},
            {"station": "C", "time_offset": 3, "distance_km": 3.0},
        ])
        self.assertEqual(quality["metrics"]["valid_count"], 3)
        self.assertGreater(quality["metrics"]["average_distance_km"], 2.0)
        self.assertFalse(quality["review_gate"]["passes"])


class QualityGateServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.reviewer = Actor("rv", "reviewer")

    def tearDown(self):
        self.tmp.cleanup()

    def _associated_event(self, reports, actor=None):
        event = self.service.create(actor or self.admin, "event", event_data(reports))
        return self.service.transition(
            actor or self.admin, event["id"], "associate", {}
        )

    def test_reviewer_blocked_when_fewer_than_three_valid(self):
        event = self._associated_event([
            {"station": "A", "time_offset": 1, "distance_km": 1.0},
            {"station": "B", "time_offset": 2, "distance_km": 1.2},
        ])
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.reviewer, event["id"], "review",
                {"reviewer": "rv", "magnitude": 4.0},
            )

    def test_admin_without_basis_is_blocked(self):
        event = self._associated_event([
            {"station": "A", "time_offset": 1, "distance_km": 1.0},
            {"station": "B", "time_offset": 2, "distance_km": 1.2},
        ])
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, event["id"], "review",
                {"reviewer": "admin", "magnitude": 4.0},
            )

    def test_admin_may_release_with_written_basis(self):
        event = self._associated_event([
            {"station": "A", "time_offset": 1, "distance_km": 1.0},
            {"station": "B", "time_offset": 2, "distance_km": 1.2},
        ])
        reviewed = self.service.transition(
            self.admin, event["id"], "review",
            {"reviewer": "admin", "magnitude": 4.0,
             "override_reason": "台站巡检确认 B 校时正常，主管部门电话会纪要 2026-007"},
        )
        self.assertEqual(reviewed["status"], "reviewed")
        override = reviewed["data"]["review_override"]
        self.assertTrue(override["reason"])
        self.assertEqual(override["actor_id"], "admin")
        self.assertFalse(reviewed["data"]["review_quality"]["passes_gate"])

    def test_association_is_frozen_against_later_report_changes(self):
        event = self._associated_event([
            {"station": "A", "time_offset": 50, "distance_km": 1.0},
            {"station": "A", "time_offset": 8, "distance_km": 1.0},
            {"station": "B", "time_offset": 5, "distance_km": 1.2},
            {"station": "C", "time_offset": 6, "distance_km": 1.4},
        ])
        frozen = event["data"]["association"]
        self.assertEqual(frozen["metrics"]["duplicate_count"], 1)
        self.assertEqual(frozen["metrics"]["valid_count"], 3)
        # 复核载荷中夹带被篡改的 association 快照会被拒绝
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.reviewer, event["id"], "review",
                {"reviewer": "rv", "magnitude": 4.1,
                 "association": dict(frozen, quality_score=100)},
            )
        # 复核、发布、修订后快照仍与固化时一致，后补报文改不动
        reviewed = self.service.transition(
            self.reviewer, event["id"], "review",
            {"reviewer": "rv", "magnitude": 4.1},
        )
        published = self.service.transition(
            self.reviewer, reviewed["id"], "publish",
            {"communication_id": "C-2"},
        )
        revised = self.service.transition(
            self.admin, published["id"], "revise",
            {"reason": "后补报文到达", "magnitude": 4.2},
        )
        self.assertEqual(revised["data"]["association"], frozen)

    def test_stages_partition_list(self):
        candidate = self._associated_event([
            {"station": "A", "time_offset": 1, "distance_km": 1.0},
            {"station": "B", "time_offset": 2, "distance_km": 1.0},
            {"station": "C", "time_offset": 3, "distance_km": 1.0},
        ])
        self.assertEqual(event_stage(candidate["status"]), "review")
        fresh = self.service.create(self.admin, "event", event_data([
            {"station": "X", "time_offset": 1, "distance_km": 1.0},
            {"station": "Y", "time_offset": 2, "distance_km": 1.0},
        ]))
        self.assertEqual(event_stage(fresh["status"]), "candidate")
        reviewed = self.service.transition(
            self.reviewer, candidate["id"], "review",
            {"reviewer": "rv", "magnitude": 4.0},
        )
        self.assertEqual(event_stage(reviewed["status"]), "review")
        published = self.service.transition(
            self.reviewer, reviewed["id"], "publish", {"communication_id": "C-3"}
        )
        self.assertEqual(event_stage(published["status"]), "published")

    def test_detail_carries_summary_and_version_history(self):
        event = self._associated_event([
            {"station": "A", "time_offset": 1, "distance_km": 1.0},
            {"station": "B", "time_offset": 2, "distance_km": 1.0},
            {"station": "C", "time_offset": 3, "distance_km": 1.0},
        ])
        detail = self.service.detail(event["id"])
        self.assertEqual(detail["summary"]["valid_count"], 3)
        self.assertEqual(detail["summary"]["quality_score"], 100)
        self.assertEqual(len(detail["versions"]), 2)
        self.assertEqual(detail["versions"][-1]["status"], "associated")


if __name__ == "__main__":
    unittest.main()
