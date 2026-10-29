import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dia_agentic.core import run_store


class AutoReviewTests(unittest.TestCase):
    def test_only_pending_policy_matches_are_approved_and_reopen_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            run_store, "DB_PATH", Path(tmp) / "review.db"
        ):
            run_store.init_db()
            model = "deterministic-v2:low_risk_complete"
            routes = [
                {
                    "row_idx": idx, "suggested_route": "auto_clear",
                    "confidence": 0.9, "reason": "Low risk, evidence complete",
                    "model": model if idx != 3 else "deterministic-v2:uncertain_band",
                    "answers": {}, "state": {}, "usage": {},
                }
                for idx in (1, 2, 3)
            ]
            run_store.save_level1_routes("run_test", routes, user_id=7)
            run_store.save_review_decision("run_test", 2, "confirmed", "Human decision")

            self.assertEqual(
                run_store.auto_approve_clear_routes(
                    "run_test", [1, 2, 3], model, user_id=7
                ), 1
            )
            self.assertEqual(run_store.get_review_decision("run_test", 1)["status"], "false_positive")
            self.assertEqual(run_store.get_review_decision("run_test", 2)["status"], "confirmed")
            self.assertEqual(run_store.get_review_decision("run_test", 3)["status"], "pending")
            self.assertIn("low_risk_complete", run_store.get_review_decision("run_test", 1)["note"])

            self.assertTrue(run_store.reopen_auto_approved("run_test", 1, user_id=7))
            self.assertFalse(run_store.reopen_auto_approved("run_test", 1, user_id=7))
            self.assertEqual(run_store.get_review_decision("run_test", 1)["status"], "pending")
            self.assertEqual(
                run_store.get_level1_routes("run_test", user_id=7)[1]["final_route"],
                "needs_evidence",
            )
            self.assertEqual(
                run_store.auto_approve_clear_routes("run_test", [1], model, user_id=7), 0
            )


if __name__ == "__main__":
    unittest.main()
