import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class ReleaseReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.analyst = Actor("analyst-1", "analyst")
        self.reviewer = Actor("reviewer-1", "reviewer")

    def tearDown(self):
        self.tmp.cleanup()

    def _instrument(self):
        return self.service.create(
            self.admin, "instrument", {"name": "Analyzer", "serial": "A-1"}
        )

    def _calibration(self, instrument_id, due_at="2099-01-01", uncertainty=0.01, approve=True):
        calibration = self.service.create(
            self.admin,
            "calibration",
            {"instrument_id": instrument_id, "requested_at": "2026-01-01"},
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
        if approve:
            self.service.transition(
                self.admin, calibration["id"], "approve", {"authorized_by": "QA-1"}
            )
        return self.service.get(calibration["id"])

    def _method(self, instrument_id, parameters=None, validate=True):
        method = self.service.create(
            self.admin, "method", {"name": "Assay-A", "version": "v1"}
        )
        if validate:
            self.service.transition(
                self.admin,
                method["id"],
                "validate_method",
                {
                    "parameters": parameters if parameters is not None else {"range": [0, 10]},
                    "instrument_ids": [instrument_id],
                },
            )
        return self.service.get(method["id"])

    def _result(self):
        return self.service.create(
            self.admin, "result", {"sample_id": "S-1", "measurement": "initial"}
        )

    def _release_data(self, instrument, calibration, method, **overrides):
        data = {
            "instrument_id": instrument["id"],
            "calibration_id": calibration["id"],
            "method_id": method["id"],
            "value": 4.2,
            "unit": "mg/L",
            "uncertainty": 0.05,
        }
        data.update(overrides)
        return data

    def _pending_review_result(self):
        instrument = self._instrument()
        expired = self._calibration(instrument["id"], due_at="2020-01-01")
        method = self._method(instrument["id"])
        result = self._result()
        attempted = self.service.transition(
            self.analyst,
            result["id"],
            "release",
            self._release_data(instrument, expired, method),
        )
        assert attempted["status"] == "pending_review"
        return instrument, method, result

    def test_compliant_release_is_released(self):
        instrument = self._instrument()
        calibration = self._calibration(instrument["id"])
        method = self._method(instrument["id"])
        result = self._result()
        released = self.service.transition(
            self.analyst,
            result["id"],
            "release",
            self._release_data(instrument, calibration, method),
        )
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["data"]["released_by"], "analyst-1")
        self.assertEqual(released["data"]["review_reasons"], [])
        history = released["data"]["release_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["action"], "release")
        self.assertEqual(history[0]["outcome"], "released")

    def test_expired_calibration_goes_to_pending_review(self):
        instrument = self._instrument()
        calibration = self._calibration(instrument["id"], due_at="2020-01-01")
        method = self._method(instrument["id"])
        result = self._result()
        attempted = self.service.transition(
            self.analyst,
            result["id"],
            "release",
            self._release_data(instrument, calibration, method),
        )
        self.assertEqual(attempted["status"], "pending_review")
        self.assertIn("calibration is expired", attempted["data"]["review_reasons"])
        entry = attempted["data"]["release_history"][-1]
        self.assertEqual(entry["outcome"], "pending_review")
        self.assertEqual(entry["calibration_id"], calibration["id"])

    def test_unapproved_calibration_goes_to_pending_review(self):
        instrument = self._instrument()
        calibration = self._calibration(instrument["id"], approve=False)
        method = self._method(instrument["id"])
        result = self._result()
        attempted = self.service.transition(
            self.analyst,
            result["id"],
            "release",
            self._release_data(instrument, calibration, method),
        )
        self.assertEqual(attempted["status"], "pending_review")
        self.assertIn("calibration is not approved", attempted["data"]["review_reasons"])

    def test_revoked_method_goes_to_pending_review(self):
        instrument = self._instrument()
        calibration = self._calibration(instrument["id"])
        method = self._method(instrument["id"])
        self.service.transition(
            self.admin, method["id"], "revoke_method", {"reason": "superseded"}
        )
        result = self._result()
        attempted = self.service.transition(
            self.analyst,
            result["id"],
            "release",
            self._release_data(instrument, calibration, method),
        )
        self.assertEqual(attempted["status"], "pending_review")
        self.assertIn(
            "method is not validated or has been withdrawn",
            attempted["data"]["review_reasons"],
        )

    def test_method_must_cover_instrument(self):
        instrument = self._instrument()
        other = self._instrument()
        calibration = self._calibration(instrument["id"])
        method = self._method(other["id"])
        result = self._result()
        attempted = self.service.transition(
            self.analyst,
            result["id"],
            "release",
            self._release_data(instrument, calibration, method),
        )
        self.assertEqual(attempted["status"], "pending_review")
        self.assertIn(
            "method does not cover this instrument",
            attempted["data"]["review_reasons"],
        )

    def test_value_outside_method_range_goes_to_pending_review(self):
        instrument = self._instrument()
        calibration = self._calibration(instrument["id"])
        method = self._method(instrument["id"], parameters={"range": [0, 10]})
        result = self._result()
        attempted = self.service.transition(
            self.analyst,
            result["id"],
            "release",
            self._release_data(instrument, calibration, method, value=42),
        )
        self.assertEqual(attempted["status"], "pending_review")
        self.assertIn(
            "measurement is outside the method coverage range",
            attempted["data"]["review_reasons"],
        )

    def test_uncertainty_checks(self):
        instrument = self._instrument()
        calibration = self._calibration(instrument["id"], uncertainty=0.01)
        method = self._method(
            instrument["id"], parameters={"range": [0, 10], "max_uncertainty": 0.1}
        )
        low = self._result()
        attempted = self.service.transition(
            self.analyst,
            low["id"],
            "release",
            self._release_data(instrument, calibration, method, uncertainty=0.001),
        )
        self.assertIn(
            "measurement uncertainty is below the calibration uncertainty",
            attempted["data"]["review_reasons"],
        )
        high = self._result()
        attempted = self.service.transition(
            self.analyst,
            high["id"],
            "release",
            self._release_data(instrument, calibration, method, uncertainty=0.5),
        )
        self.assertIn(
            "measurement uncertainty exceeds the method limit",
            attempted["data"]["review_reasons"],
        )
        missing = self._result()
        data = self._release_data(instrument, calibration, method)
        data.pop("uncertainty")
        attempted = self.service.transition(
            self.analyst, missing["id"], "release", data
        )
        self.assertIn(
            "measurement uncertainty is missing or not positive",
            attempted["data"]["review_reasons"],
        )

    def test_multiple_reasons_are_all_listed(self):
        instrument = self._instrument()
        calibration = self._calibration(instrument["id"], due_at="2020-01-01")
        method = self._method(instrument["id"])
        self.service.transition(
            self.admin, method["id"], "revoke_method", {"reason": "withdrawn"}
        )
        result = self._result()
        attempted = self.service.transition(
            self.analyst,
            result["id"],
            "release",
            self._release_data(instrument, calibration, method, uncertainty=0.001),
        )
        reasons = attempted["data"]["review_reasons"]
        self.assertIn("calibration is expired", reasons)
        self.assertIn("method is not validated or has been withdrawn", reasons)
        self.assertIn(
            "measurement uncertainty is below the calibration uncertainty", reasons
        )

    def test_review_replaces_binding_and_releases(self):
        instrument, method, result = self._pending_review_result()
        expired_id = self.service.get(result["id"])["data"]["calibration_id"]
        valid = self._calibration(instrument["id"], due_at="2099-01-01")
        reviewed = self.service.transition(
            self.reviewer,
            result["id"],
            "review",
            {
                "instrument_id": instrument["id"],
                "calibration_id": valid["id"],
                "method_id": method["id"],
                "disposition": "原校准已到期，更换为有效校准后发布",
            },
        )
        self.assertEqual(reviewed["status"], "released")
        self.assertEqual(reviewed["data"]["calibration_id"], valid["id"])
        self.assertEqual(reviewed["data"]["released_by"], "reviewer-1")
        self.assertEqual(
            reviewed["data"]["disposition"], "原校准已到期，更换为有效校准后发布"
        )
        history = reviewed["data"]["release_history"]
        self.assertEqual([entry["action"] for entry in history], ["release", "review"])
        self.assertEqual(history[0]["calibration_id"], expired_id)
        self.assertEqual(history[0]["outcome"], "pending_review")
        self.assertEqual(
            history[1]["disposition"], "原校准已到期，更换为有效校准后发布"
        )
        self.assertEqual(history[1]["outcome"], "released")
        self.assertLessEqual(history[0]["at"], history[1]["at"])

    def test_failed_review_stays_pending_review(self):
        instrument, method, result = self._pending_review_result()
        still_expired = self._calibration(instrument["id"], due_at="2021-01-01")
        reviewed = self.service.transition(
            self.reviewer,
            result["id"],
            "review",
            {
                "instrument_id": instrument["id"],
                "calibration_id": still_expired["id"],
                "method_id": method["id"],
                "disposition": "尝试另一份校准记录",
            },
        )
        self.assertEqual(reviewed["status"], "pending_review")
        self.assertIn("calibration is expired", reviewed["data"]["review_reasons"])
        history = reviewed["data"]["release_history"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[-1]["action"], "review")
        self.assertEqual(history[-1]["outcome"], "pending_review")

    def test_review_requires_disposition(self):
        instrument, method, result = self._pending_review_result()
        valid = self._calibration(instrument["id"])
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.reviewer,
                result["id"],
                "review",
                {
                    "instrument_id": instrument["id"],
                    "calibration_id": valid["id"],
                    "method_id": method["id"],
                },
            )

    def test_analyst_cannot_review(self):
        instrument, method, result = self._pending_review_result()
        valid = self._calibration(instrument["id"])
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.analyst,
                result["id"],
                "review",
                {
                    "instrument_id": instrument["id"],
                    "calibration_id": valid["id"],
                    "method_id": method["id"],
                    "disposition": "越权复核",
                },
            )

    def test_released_result_cannot_be_rebound(self):
        instrument = self._instrument()
        calibration = self._calibration(instrument["id"])
        method = self._method(instrument["id"])
        other_method = self._method(instrument["id"])
        result = self._result()
        released = self.service.transition(
            self.analyst,
            result["id"],
            "release",
            self._release_data(instrument, calibration, method),
        )
        self.assertEqual(released["status"], "released")
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.analyst,
                result["id"],
                "release",
                self._release_data(instrument, calibration, other_method),
            )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.reviewer,
                result["id"],
                "review",
                {
                    "instrument_id": instrument["id"],
                    "calibration_id": calibration["id"],
                    "method_id": other_method["id"],
                    "disposition": "尝试改绑",
                },
            )
        after = self.service.get(result["id"])
        self.assertEqual(after["status"], "released")
        self.assertEqual(after["data"]["method_id"], method["id"])
        self.assertEqual(after["data"]["calibration_id"], calibration["id"])

    def test_pending_review_can_be_blocked_and_reanalyzed(self):
        instrument, method, result = self._pending_review_result()
        blocked = self.service.transition(
            self.analyst, result["id"], "block", {"reason": "样品作废"}
        )
        self.assertEqual(blocked["status"], "blocked")
        reopened = self.service.transition(
            self.analyst, result["id"], "reanalyze", {"reason": "重新检测"}
        )
        self.assertEqual(reopened["status"], "pending")


if __name__ == "__main__":
    unittest.main()
