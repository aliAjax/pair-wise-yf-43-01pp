import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ReleaseGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.analyst = Actor("analyst-1", "analyst")
        self.reviewer = Actor("authorizer-1", "authorizer")

    def tearDown(self):
        self.tmp.cleanup()

    def _instrument(self, due_at="2099-01-01", uncertainty=0.01):
        instrument = self.service.create(
            self.admin, "instrument", {"name": "Analyzer", "serial": "A-1"}
        )
        self.service.transition(self.admin, instrument["id"], "send_calibration", {})
        self.service.transition(
            self.admin,
            instrument["id"],
            "calibrate",
            {"due_at": due_at, "passed": True},
        )
        calibration = self.service.create(
            self.admin,
            "calibration",
            {"instrument_id": instrument["id"], "requested_at": "2026-01-01"},
        )
        self.service.transition(
            self.admin,
            calibration["id"],
            "perform",
            {
                "result": "passed",
                "performed_at": "2026-01-02",
                "uncertainty": uncertainty,
                "due_at": due_at,
            },
        )
        self.service.transition(
            self.admin, calibration["id"], "approve", {"authorized_by": "QA-1"}
        )
        return instrument["id"], calibration["id"]

    def _method(self, instrument_id, span=(0, 10)):
        method = self.service.create(
            self.admin, "method", {"name": "Assay-A", "version": "v1"}
        )
        self.service.transition(
            self.admin,
            method["id"],
            "validate_method",
            {"parameters": {"range": list(span)}, "instrument_ids": [instrument_id]},
        )
        return method["id"]

    def _result(self):
        return self.service.create(
            self.analyst, "result", {"sample_id": "S-1", "measurement": "assay"}
        )["id"]

    def _release(self, result_id, instrument_id, method_id, **overrides):
        data = {
            "instrument_id": instrument_id,
            "method_id": method_id,
            "value": 4.2,
            "unit": "mg/L",
            "uncertainty": 0.02,
        }
        data.update(overrides)
        return self.service.transition(self.analyst, result_id, "release", data)

    def test_release_passes_when_all_checks_ok(self):
        instrument_id, calibration_id = self._instrument()
        method_id = self._method(instrument_id)
        result_id = self._result()
        entity = self._release(result_id, instrument_id, method_id)
        self.assertEqual(entity["status"], "released")
        self.assertEqual(entity["data"]["review_reasons"], [])
        self.assertEqual(entity["data"]["calibration_id"], calibration_id)
        self.assertEqual(entity["data"]["released_by"], self.analyst.user_id)
        history = entity["data"]["release_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["outcome"], "released")
        self.assertEqual(history[0]["instrument_id"], instrument_id)
        self.assertEqual(history[0]["method_id"], method_id)

    def test_expired_calibration_goes_to_review(self):
        instrument_id, _ = self._instrument(due_at="2020-01-01")
        method_id = self._method(instrument_id)
        result_id = self._result()
        entity = self._release(result_id, instrument_id, method_id)
        self.assertEqual(entity["status"], "review")
        self.assertTrue(
            any("校准" in reason for reason in entity["data"]["review_reasons"])
        )
        history = entity["data"]["release_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["outcome"], "review")
        self.assertEqual(history[0]["reasons"], entity["data"]["review_reasons"])

    def test_revoked_method_goes_to_review(self):
        instrument_id, _ = self._instrument()
        method_id = self._method(instrument_id)
        self.service.transition(
            self.admin, method_id, "revoke_method", {"reason": "superseded"}
        )
        result_id = self._result()
        entity = self._release(result_id, instrument_id, method_id)
        self.assertEqual(entity["status"], "review")
        self.assertTrue(
            any("方法" in reason for reason in entity["data"]["review_reasons"])
        )

    def test_value_outside_method_range_goes_to_review(self):
        instrument_id, _ = self._instrument()
        method_id = self._method(instrument_id, span=(0, 10))
        result_id = self._result()
        entity = self._release(result_id, instrument_id, method_id, value=42.0)
        self.assertEqual(entity["status"], "review")
        self.assertTrue(
            any("覆盖范围" in reason for reason in entity["data"]["review_reasons"])
        )

    def test_missing_uncertainty_goes_to_review(self):
        instrument_id, _ = self._instrument()
        method_id = self._method(instrument_id)
        result_id = self._result()
        entity = self._release(result_id, instrument_id, method_id, uncertainty=None)
        self.assertEqual(entity["status"], "review")
        self.assertTrue(
            any("不确定度" in reason for reason in entity["data"]["review_reasons"])
        )

    def test_uncertainty_below_calibration_floor_goes_to_review(self):
        instrument_id, _ = self._instrument(uncertainty=0.05)
        method_id = self._method(instrument_id)
        result_id = self._result()
        entity = self._release(result_id, instrument_id, method_id, uncertainty=0.02)
        self.assertEqual(entity["status"], "review")
        self.assertTrue(
            any("不确定度" in reason for reason in entity["data"]["review_reasons"])
        )

    def test_all_failures_are_listed_together(self):
        instrument_id, _ = self._instrument(due_at="2020-01-01")
        method_id = self._method(instrument_id)
        self.service.transition(
            self.admin, method_id, "revoke_method", {"reason": "superseded"}
        )
        result_id = self._result()
        entity = self._release(
            result_id, instrument_id, method_id, value=42.0, uncertainty=None
        )
        self.assertEqual(entity["status"], "review")
        reasons = entity["data"]["review_reasons"]
        self.assertTrue(any("校准" in reason for reason in reasons))
        self.assertTrue(any("方法" in reason for reason in reasons))
        self.assertTrue(any("覆盖范围" in reason for reason in reasons))
        self.assertTrue(any("不确定度" in reason for reason in reasons))

    def test_resolve_review_releases_with_valid_binding_and_disposition(self):
        instrument_id, old_calibration_id = self._instrument(due_at="2020-01-01")
        method_id = self._method(instrument_id)
        result_id = self._result()
        attempted = self._release(result_id, instrument_id, method_id)
        self.assertEqual(attempted["status"], "review")
        # 计量组重新校准，产生新的有效校准记录
        _, new_calibration_id = self._new_calibration(instrument_id, "2099-01-01")
        resolved = self.service.transition(
            self.reviewer,
            result_id,
            "resolve_review",
            {
                "instrument_id": instrument_id,
                "method_id": method_id,
                "calibration_id": new_calibration_id,
                "value": 4.2,
                "unit": "mg/L",
                "uncertainty": 0.02,
                "disposition": "已重新校准，改用新校准记录后放行",
            },
        )
        self.assertEqual(resolved["status"], "released")
        self.assertEqual(resolved["data"]["calibration_id"], new_calibration_id)
        self.assertEqual(
            resolved["data"]["disposition"], "已重新校准，改用新校准记录后放行"
        )
        self.assertEqual(resolved["data"]["resolved_by"], self.reviewer.user_id)
        # 原尝试、旧组合和处置意见按时间保留
        history = resolved["data"]["release_history"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["action"], "release")
        self.assertEqual(history[0]["outcome"], "review")
        self.assertEqual(history[0]["calibration_id"], old_calibration_id)
        self.assertEqual(history[1]["action"], "resolve_review")
        self.assertEqual(history[1]["outcome"], "released")
        self.assertEqual(history[1]["calibration_id"], new_calibration_id)
        self.assertEqual(
            history[1]["disposition"], "已重新校准，改用新校准记录后放行"
        )
        self.assertLessEqual(history[0]["at"], history[1]["at"])
        audit = self.service.audit_log(entity_id=result_id)
        actions = [entry["action"] for entry in audit]
        self.assertEqual(actions, ["create", "release", "resolve_review"])

    def test_resolve_review_with_invalid_binding_stays_in_review(self):
        instrument_id, _ = self._instrument(due_at="2020-01-01")
        method_id = self._method(instrument_id)
        result_id = self._result()
        self._release(result_id, instrument_id, method_id)
        still_bad = self.service.transition(
            self.reviewer,
            result_id,
            "resolve_review",
            {
                "instrument_id": instrument_id,
                "method_id": method_id,
                "value": 4.2,
                "unit": "mg/L",
                "uncertainty": 0.02,
                "disposition": "尝试沿用旧校准",
            },
        )
        self.assertEqual(still_bad["status"], "review")
        self.assertTrue(
            any("校准" in reason for reason in still_bad["data"]["review_reasons"])
        )
        history = still_bad["data"]["release_history"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[1]["outcome"], "review")
        self.assertEqual(history[1]["disposition"], "尝试沿用旧校准")

    def test_released_result_cannot_be_rebound(self):
        instrument_id, _ = self._instrument()
        method_id = self._method(instrument_id)
        result_id = self._result()
        released = self._release(result_id, instrument_id, method_id)
        self.assertEqual(released["status"], "released")
        with self.assertRaises(InvalidTransition):
            self._release(result_id, instrument_id, method_id, value=9.9)
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.reviewer,
                result_id,
                "resolve_review",
                {
                    "instrument_id": instrument_id,
                    "method_id": method_id,
                    "value": 9.9,
                    "unit": "mg/L",
                    "uncertainty": 0.02,
                    "disposition": "试图改绑",
                },
            )
        unchanged = self.service.get(result_id)
        self.assertEqual(unchanged["data"]["value"], 4.2)
        self.assertEqual(len(unchanged["data"]["release_history"]), 1)

    def test_resolve_review_requires_disposition(self):
        instrument_id, _ = self._instrument(due_at="2020-01-01")
        method_id = self._method(instrument_id)
        result_id = self._result()
        self._release(result_id, instrument_id, method_id)
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.reviewer,
                result_id,
                "resolve_review",
                {
                    "instrument_id": instrument_id,
                    "method_id": method_id,
                    "value": 4.2,
                    "unit": "mg/L",
                    "uncertainty": 0.02,
                },
            )

    def test_analyst_cannot_resolve_review(self):
        instrument_id, _ = self._instrument(due_at="2020-01-01")
        method_id = self._method(instrument_id)
        result_id = self._result()
        self._release(result_id, instrument_id, method_id)
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.analyst,
                result_id,
                "resolve_review",
                {
                    "instrument_id": instrument_id,
                    "method_id": method_id,
                    "value": 4.2,
                    "unit": "mg/L",
                    "uncertainty": 0.02,
                    "disposition": "分析员试图自行复核",
                },
            )

    def _new_calibration(self, instrument_id, due_at):
        calibration = self.service.create(
            self.admin,
            "calibration",
            {"instrument_id": instrument_id, "requested_at": "2026-02-01"},
        )
        self.service.transition(
            self.admin,
            calibration["id"],
            "perform",
            {
                "result": "passed",
                "performed_at": "2026-02-02",
                "uncertainty": 0.01,
                "due_at": due_at,
            },
        )
        self.service.transition(
            self.admin, calibration["id"], "approve", {"authorized_by": "QA-2"}
        )
        return instrument_id, calibration["id"]


if __name__ == "__main__":
    unittest.main()
