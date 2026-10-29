import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from dia_agentic.core import run_store
from dia_agentic.core.chat_agent import answer_chat, _local_intent
from dia_agentic.core.risk_scorer import compute_risk_scores
from dia_agentic.core.evidence import evidence_for


ROWS = [
    {"physical_invoice_no": "INV-A", "vendor_name": "Alpha",
     "amount_excl_lc": "1000", "payment_date": "01-01-1900",
     "actual_pay_date": "05-01-2024", "date_documented": "10-01-2024"},
    {"physical_invoice_no": "INV-B", "vendor_name": "Beta",
     "amount_excl_lc": "50", "payment_date": "1900-01-01",
     "actual_pay_date": None, "scheduled_pay_date": "05-01-2024",
     "date_documented": "01-01-1900"},
    {"physical_invoice_no": "INV-C", "vendor_name": "Gamma",
     "amount_excl_lc": "20", "payment_date": "invalid",
     "actual_pay_date": "bad", "date_documented": "09-01-2024"},
]
TESTS = [{"test_id": "audit", "test_name": "Audit Test", "applicable": True,
          "hit_count": 1, "all_hit_indices": [0], "weightage": 8,
          "severity": "warning", "type": "preset"}]


class ChatGroundingTests(unittest.TestCase):
    def setUp(self):
        self.scored = compute_risk_scores(ROWS, TESTS)
        self.decisions = {0: {"status": "confirmed", "note": "Reviewed by Jane"}}

    def ask(self, intent, **extra):
        with patch("dia_agentic.core.chat_agent._parse_intent_llm",
                   return_value={"intent": intent, **extra}):
            return answer_chat("synthetic question", ROWS, self.scored,
                               self.decisions, evidence_for, test_results=TESTS,
                               agent_cases={0: {
                                   "recommendation": "confirm",
                                   "reason": "Test hit needs follow-up",
                                   "suggested_check": "Check source invoice",
                                   "missing_fields": [],
                               }})

    def test_all_invoice_count_includes_unflagged_rows(self):
        result = self.ask("run_stats", stat_field="all_invoices", stat_condition="total")
        self.assertEqual((result["count"], result["total"]), (3, 3))
        self.assertEqual(len(result["sample_invoices"]), 3)
        self.assertIn("sources", result)

    def test_invalid_primary_uses_fallback_but_not_scheduled(self):
        missing = self.ask("run_stats", stat_field="actual_payment_date",
                           stat_condition="missing")
        present = self.ask("run_stats", stat_field="actual_payment_date",
                           stat_condition="present")
        self.assertEqual(missing["count"], 2)
        self.assertEqual(present["count"], 1)
        self.assertEqual(present["sample_invoices"][0]["invoice"], "INV-A")
        documented = self.ask("run_stats", stat_field="date_documented",
                              stat_condition="missing")
        self.assertEqual(documented["sample_invoices"][0]["invoice"], "INV-B")

    def test_review_counts_are_recorded_decisions_not_agent_recommendations(self):
        result = self.ask("review_overview")
        self.assertEqual(result["counts"]["confirmed"], 1)
        self.assertEqual(sum(result["counts"].values()), 1)

    def test_case_keeps_agent_recommendation_separate_from_decision(self):
        result = self.ask("review_case", row_ref="INV-A")
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["recommendation"], "confirm")
        self.assertIn("Reviewed by Jane", result["message"])
        self.assertIn("separate", result["message"])
        no_case = self.ask("review_case", row_ref="INV-B")
        self.assertEqual(no_case["status"], "pending")
        self.assertIsNone(no_case["recommendation"])
        self.assertIn("No Agent Review recommendation", no_case["message"])

    def test_unknown_or_ambiguous_invoice_never_guesses(self):
        self.assertEqual(self.ask("review_case", row_ref="INV")["type"], "not_found")
        self.assertEqual(self.ask("review_case", row_ref="INV-X")["type"], "not_found")
        self.assertEqual(self.ask("review_case")["type"], "clarify")
        with patch("dia_agentic.core.chat_agent._parse_intent_llm",
                   return_value={"intent": "review_case", "row_ref": "INV-A"}):
            duplicated = answer_chat("status", [ROWS[0], dict(ROWS[0])], self.scored,
                                     self.decisions, evidence_for)
        self.assertIn("More than one", duplicated["message"])

    def test_unflagged_invoice_can_be_found_without_inventing_risk(self):
        result = self.ask("explain_row", row_ref="INV-B")
        self.assertEqual(result["type"], "row_detail")
        self.assertIn("no risk score", result["message"])
        self.assertEqual(result["evidence"], [])
        self.assertEqual(self.ask("explain_row", row_ref="INV")["type"], "not_found")

    def test_field_and_risk_explanations_use_app_rules(self):
        field = self.ask("explain_field", field_name="scheduled_pay_date")
        self.assertIn("does not prove", field["message"])
        self.assertEqual(self.ask("explain_field", field_name="invented")["type"], "clarify")
        score = self.ask("explain_risk", row_ref="INV-A")
        self.assertIn(f"scores {self.scored[0].risk_score}", score["message"])
        self.assertIn("not a fraud probability", score["message"])
        unflagged = self.ask("explain_risk", row_ref="INV-B")
        self.assertIn("no risk score", unflagged["message"])
        actual = self.ask("invoice_field", row_ref="INV-A", field_name="actual_payment_date")
        self.assertIn("2024-01-05", actual["message"])
        placeholder = self.ask("invoice_field", row_ref="INV-A", field_name="payment_date")
        self.assertIn("placeholder", placeholder["message"])
        self.assertEqual(self.ask("invoice_field", field_name="payment_date")["type"], "clarify")

    def test_unimplemented_filtered_all_run_stats_are_not_silently_broadened(self):
        result = self.ask("run_stats", stat_field="actual_payment_date",
                          stat_condition="missing", filters={"vendor_name": "Alpha"})
        self.assertEqual(result["type"], "clarify")

    def test_common_questions_work_without_model_but_date_comparisons_do_not_get_misrouted(self):
        questions = [
            ("How many invoices have no actual payment date?", "run_stats_result"),
            ("How many invoices are pending review?", "review_overview_result"),
            ("What did Agent Review recommend for invoice INV-A?", "review_case_result"),
            ("How is risk score calculated?", "risk_help_result"),
            ("What does scheduled_pay_date mean?", "field_help_result"),
        ]
        with patch("dia_agentic.core.chat_agent._parse_intent_llm",
                   side_effect=AssertionError("unnecessary model call")):
            for question, result_type in questions:
                result = answer_chat(question, ROWS, self.scored, self.decisions,
                                     evidence_for, test_results=TESTS)
                self.assertEqual(result["type"], result_type, question)
        self.assertIsNone(_local_intent(
            "How many invoices have payment dates before invoice dates?"
        ))
        self.assertIsNone(_local_intent(
            "How many invoices have no actual payment date for vendor Alpha?"
        ))
        self.assertEqual(_local_intent(
            "Is actual_pay_date blank for invoice INV-A?"
        )["intent"], "invoice_field")

    def test_authenticated_chat_uses_saved_run_and_scoped_agent_cases(self):
        import dia_agentic.app as app_module
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            run_store, "DB_PATH", Path(tmp) / "chat.db"
        ):
            run_store.init_db()
            run_store.save_run("chat_run", {"run_id": "chat_run"}, ROWS,
                               "synthetic.csv", user_id=7)
            run_store.save_test_session("chat_run", [], TESTS, user_id=7)
            run_store.save_agent_review_case("chat_run", 0, 7, {
                "schema_version": 5, "recommendation": "confirm",
                "reason": "Check actual invoice", "missing_fields": [],
            })
            app_module.app.dependency_overrides[app_module.require_auth] = lambda: {
                "user_id": 7, "username": "reviewer"
            }
            try:
                with patch("dia_agentic.core.chat_agent._parse_intent_llm",
                           return_value={"intent": "review_case", "row_ref": "INV-A"}):
                    with TestClient(app_module.app) as client:
                        response = client.post("/api/runs/chat_run/chat",
                                               json={"message": "What did Agent Review recommend for INV-A?"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["recommendation"], "confirm")
                self.assertEqual(response.json()["status"], "pending")
            finally:
                app_module.app.dependency_overrides.clear()


if __name__ == "__main__":
    unittest.main()
