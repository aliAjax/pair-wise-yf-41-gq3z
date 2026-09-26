import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine, evaluate_reports
from src.service import DomainService


def _reports(*rows):
    return [
        {"station": station, "time_offset": offset, "distance_km": distance}
        for station, offset, distance in rows
    ]


class EvaluateReportsTest(unittest.TestCase):
    def test_duplicate_station_keeps_smaller_time_offset(self):
        result = evaluate_reports(_reports(
            ("STA-1", 90, 1.0),
            ("STA-1", 10, 2.0),
            ("STA-2", 5, 1.0),
        ))
        self.assertEqual(result["duplicate_count"], 1)
        self.assertEqual(result["duplicate_reports"][0]["time_offset"], 90)
        adopted_sta1 = [
            report for report in result["adopted_reports"] if report["station"] == "STA-1"
        ]
        self.assertEqual(len(adopted_sta1), 1)
        self.assertEqual(adopted_sta1[0]["time_offset"], 10)

    def test_threshold_boundaries(self):
        result = evaluate_reports(_reports(
            ("STA-1", 120, 3.0),    # 恰好等于阈值 -> 有效
            ("STA-2", 121, 1.0),    # 超过 120 秒 -> 存疑
            ("STA-3", 0, 3.1),      # 超过 3 公里 -> 存疑
        ))
        self.assertEqual([r["station"] for r in result["adopted_reports"]], ["STA-1"])
        self.assertEqual(
            [r["station"] for r in result["suspicious_reports"]], ["STA-2", "STA-3"]
        )

    def test_review_blocked_rules(self):
        too_few = evaluate_reports(_reports(("STA-1", 1, 1.0), ("STA-2", 2, 1.0)))
        self.assertTrue(too_few["review_blocked"])  # 有效报文少于 3 条
        far = evaluate_reports(_reports(
            ("STA-1", 1, 2.5), ("STA-2", 2, 2.5), ("STA-3", 3, 2.5),
        ))
        self.assertTrue(far["review_blocked"])  # 平均距离超过 2 公里
        ok = evaluate_reports(_reports(
            ("STA-1", 1, 1.0), ("STA-2", 2, 1.0), ("STA-3", 3, 1.0),
        ))
        self.assertFalse(ok["review_blocked"])
        self.assertEqual(ok["avg_distance_km"], 1.0)

    def test_quality_score(self):
        result = evaluate_reports(_reports(
            ("STA-1", 1, 1.0),
            ("STA-2", 2, 1.0),
            ("STA-3", 3, 1.0),
            ("STA-4", 200, 1.0),   # 存疑 -15
        ))
        # 100 - 15(存疑) - 10*1.0(平均距离) = 75
        self.assertEqual(result["quality_score"], 75.0)


class QualityGateWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.analyst = Actor("analyst", "analyst")
        self.reviewer = Actor("reviewer", "reviewer")

    def tearDown(self):
        self.tmp.cleanup()

    def _create_event(self, reports):
        return self.service.create(
            self.admin,
            "event",
            {
                "title": "Event-Q",
                "origin_time": "2026-01-01T00:00:00Z",
                "location": "Region-Q",
                "reports": reports,
            },
        )

    def test_association_snapshot_frozen_against_supplement_and_revise(self):
        event = self._create_event(_reports(
            ("STA-1", 1, 1.0), ("STA-2", 2, 1.0), ("STA-3", 3, 1.0),
        ))
        associated = self.service.transition(self.analyst, event["id"], "associate", {})
        snapshot = associated["data"]["association"]
        self.assertEqual(snapshot["version"], associated["version"])
        self.assertEqual(snapshot["valid_count"], 3)
        self.assertEqual(snapshot["suspicious_count"], 0)
        self.assertEqual(snapshot["duplicate_count"], 0)

        # 补报只能发生在关联前；关联后补报被拒绝
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.analyst, event["id"], "supplement",
                {"reports": [{"station": "STA-9", "time_offset": 5, "distance_km": 1.0}]},
            )

        reviewed = self.service.transition(
            self.reviewer, event["id"], "review",
            {"reviewer": "R-1", "magnitude": 4.2},
        )
        published = self.service.transition(
            self.reviewer, event["id"], "publish", {"communication_id": "C-1"}
        )
        revised = self.service.transition(
            self.reviewer, event["id"], "revise",
            {"reason": "new station data", "magnitude": 4.3},
        )
        # 修订后快照逐字节不变
        self.assertEqual(revised["data"]["association"], snapshot)
        self.assertEqual(published["data"]["association"], snapshot)
        self.assertEqual(reviewed["data"]["association"], snapshot)

    def test_supplement_before_association_then_dedup_on_associate(self):
        event = self._create_event(_reports(("STA-1", 1, 1.0), ("STA-2", 2, 1.0)))
        supplemented = self.service.transition(
            self.analyst, event["id"], "supplement",
            {"reports": [
                {"station": "STA-1", "time_offset": 90, "distance_km": 1.0},
                {"station": "STA-3", "time_offset": 3, "distance_km": 1.0},
            ]},
        )
        self.assertEqual(supplemented["status"], "candidate")
        self.assertEqual(len(supplemented["data"]["reports"]), 4)
        associated = self.service.transition(self.analyst, event["id"], "associate", {})
        snapshot = associated["data"]["association"]
        # 同台站重复上报只留时间偏移更小的一份（offset=1 的那条）
        self.assertEqual(snapshot["duplicate_count"], 1)
        self.assertEqual(snapshot["duplicate_reports"][0]["time_offset"], 90)
        self.assertEqual(snapshot["valid_count"], 3)
        sta1 = [
            r for r in snapshot["adopted_reports"] if r["station"] == "STA-1"
        ]
        self.assertEqual(sta1[0]["time_offset"], 1)

    def test_review_blocked_for_reviewer_and_released_by_admin(self):
        event = self._create_event(_reports(
            ("STA-1", 1, 1.0),
            ("STA-2", 2, 1.0),
            ("STA-3", 200, 1.0),   # 存疑 -> 有效报文只有 2 条
        ))
        self.service.transition(self.analyst, event["id"], "associate", {})

        # 复核员被质量关口拦截
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.reviewer, event["id"], "review",
                {"reviewer": "R-1", "magnitude": 4.2},
            )
        # 管理员也必须写明依据
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin, event["id"], "review",
                {"reviewer": "R-1", "magnitude": 4.2},
            )
        # 管理员写明依据后放行
        reviewed = self.service.transition(
            self.admin, event["id"], "review",
            {"reviewer": "R-1", "magnitude": 4.2,
             "override_reason": "已电话核实 STA-3 时钟漂移，波形可用"},
        )
        self.assertEqual(reviewed["status"], "reviewed")
        override = reviewed["data"]["quality_override"]
        self.assertEqual(override["reason"], "已电话核实 STA-3 时钟漂移，波形可用")
        self.assertEqual(override["valid_count"], 2)

    def test_far_reports_block_review(self):
        event = self._create_event(_reports(
            ("STA-1", 1, 2.5), ("STA-2", 2, 2.5), ("STA-3", 3, 2.5),
        ))
        self.service.transition(self.analyst, event["id"], "associate", {})
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.reviewer, event["id"], "review",
                {"reviewer": "R-1", "magnitude": 4.2},
            )

    def test_list_stages_and_detail_with_versions(self):
        candidate = self._create_event(_reports(
            ("STA-1", 1, 1.0), ("STA-2", 2, 1.0), ("STA-3", 3, 1.0),
        ))
        pending = self._create_event(_reports(
            ("STA-1", 1, 1.0), ("STA-2", 2, 1.0), ("STA-3", 3, 1.0),
        ))
        self.service.transition(self.analyst, pending["id"], "associate", {})
        published = self._create_event(_reports(
            ("STA-1", 1, 1.0), ("STA-2", 2, 1.0), ("STA-3", 3, 1.0),
        ))
        self.service.transition(self.analyst, published["id"], "associate", {})
        self.service.transition(
            self.reviewer, published["id"], "review",
            {"reviewer": "R-1", "magnitude": 4.2},
        )
        self.service.transition(
            self.reviewer, published["id"], "publish", {"communication_id": "C-1"}
        )

        def ids(stage):
            return {item["id"] for item in self.service.list("event", stage=stage)}

        self.assertEqual(ids("candidate"), {candidate["id"]})
        self.assertEqual(ids("review"), {pending["id"]})
        self.assertEqual(ids("published"), {published["id"]})
        with self.assertRaises(ValidationError):
            self.service.list("event", stage="unknown")

        detail = self.service.entity_detail(published["id"])
        self.assertEqual(detail["stage"], "published")
        self.assertEqual(detail["summary"]["association"]["quality_score"], 90.0)
        self.assertEqual(detail["summary"]["magnitude"], 4.2)
        versions = detail["versions"]
        self.assertEqual([v["version"] for v in versions], [1, 2, 3, 4])
        self.assertEqual(
            [v["status"] for v in versions],
            ["candidate", "associated", "reviewed", "published"],
        )
        # 历史版本里能看到关联快照在 v2 固化
        self.assertEqual(versions[1]["data"]["association"]["version"], 2)


if __name__ == "__main__":
    unittest.main()
