import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dia_agentic.core import run_store
from dia_agentic.core.agent_review import investigate
from dia_agentic.core.risk_scorer import RowRisk


class FakeCompletions:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(next(self.replies)))
        )])


class AgentReviewTests(unittest.TestCase):
    def test_one_bounded_tool_then_saved_recommendation(self):
        completions = FakeCompletions([
            {"next": "related", "reason": "Check vendor history"},
            {"recommendation": "confirm", "reason": "Two test hits support investigation",
             "missing_fields": []},
        ])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        risk = RowRisk(0, 81, "Critical", "critical", [
            {"test_id": "duplicate", "test_name": "Duplicate", "severity": "error"},
            {"test_id": "date", "test_name": "Date", "severity": "warning"},
        ], 2, 5000)
        rows = [
            {"vendor_number": "V1", "physical_invoice_no": "A", "amount_excl_lc": 5000},
            {"vendor_number": "V1", "physical_invoice_no": "B", "amount_excl_lc": 4000},
        ]
        result = investigate(rows[0], risk, rows, (client, "mock"))
        self.assertEqual(result["recommendation"], "confirm")
        self.assertEqual(result["trace"][3]["result"]["related_count"], 1)
        self.assertEqual(len(completions.calls), 2)
        self.assertLessEqual(completions.calls[0]["max_tokens"], 170)
        sent = json.dumps(completions.calls)
        self.assertNotIn('"risk_score"', sent)
        self.assertNotIn('"risk_band"', sent)
        self.assertNotIn('"Critical"', sent)
        self.assertEqual(result["trace"][1]["result"]["source_fields"], {})
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            run_store, "DB_PATH", Path(tmp) / "review.db"
        ):
            run_store.init_db()
            run_store.save_agent_review_case("run", 0, 7, result)
            self.assertEqual(
                run_store.get_agent_review_cases("run", 7)[0]["recommendation"], "confirm"
            )
            self.assertEqual(run_store.get_review_decision("run", 0)["status"], "pending")
            self.assertEqual(
                run_store.approve_agent_recommendation("run", 0, 7, "reviewer"),
                "confirmed",
            )
            self.assertEqual(run_store.get_review_decision("run", 0)["status"], "confirmed")
            self.assertIsNone(
                run_store.approve_agent_recommendation("run", 0, 7, "reviewer")
            )

    def test_unsupported_tool_is_rejected_without_verdict(self):
        completions = FakeCompletions([{"next": "write_decision", "reason": "skip"}])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        risk = RowRisk(0, 70, "High", "high", [], 0, 100)
        with self.assertRaises(ValueError):
            investigate({}, risk, [{}], (client, "mock"))
        self.assertEqual(len(completions.calls), 1)

    def test_unverified_missing_field_cannot_be_approved(self):
        completions = FakeCompletions([
            {"next": "evidence", "reason": "Check actual fields"},
            {"recommendation": "request_evidence",
             "reason": "Please supply a vendor address", "missing_fields": ["vendor_address"]},
        ])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        risk = RowRisk(0, 51, "High", "high", [
            {"test_id": "round_dollar", "test_name": "Round Dollar",
             "severity": "warning", "required_fields": ["amount_excl_lc"]},
        ], 1, 3000)
        result = investigate({"amount_excl_lc": 3000}, risk, [{}], (client, "mock"))
        self.assertEqual(result["recommendation"], "manual_review")
        self.assertEqual(result["missing_fields"], [])
        self.assertIn("amount_excl_lc", json.dumps(completions.calls))

    def test_verified_blank_required_field_allows_needs_info(self):
        completions = FakeCompletions([
            {"next": "conclude", "reason": "Check missing required payment date"},
            {"recommendation": "request_evidence", "reason": "Payment date is blank",
             "missing_fields": ["payment_date", "vendor_address"]},
        ])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        risk = RowRisk(0, 55, "High", "high", [
            {"test_id": "payment_timing", "test_name": "Payment timing",
             "severity": "warning", "required_fields": ["payment_date"]},
        ], 1, 100)
        result = investigate({"payment_date": None}, risk, [{}], (client, "mock"))
        self.assertEqual(result["recommendation"], "request_evidence")
        self.assertEqual(result["missing_fields"], ["payment_date"])
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            run_store, "DB_PATH", Path(tmp) / "review.db"
        ):
            run_store.init_db()
            run_store.save_agent_review_case("run", 0, 7, result)
            self.assertEqual(
                run_store.approve_agent_recommendation("run", 0, 7, "reviewer"),
                "needs_info",
            )
            self.assertEqual(run_store.get_review_decision("run", 0)["status"], "needs_info")
            run_store.save_agent_review_case("run", 1, 7, {
                "recommendation": "request_evidence", "missing_fields": [],
                "reason": "Unverified legacy claim",
            })
            self.assertIsNone(run_store.approve_agent_recommendation("run", 1, 7, "reviewer"))

    def test_sentinel_payment_date_is_not_evidence_and_dismissal_becomes_needs_info(self):
        completions = FakeCompletions([
            {"next": "conclude", "reason": "Test matched"},
            {"recommendation": "dismiss",
             "reason": "Same-day capture and scheduled payment may be an error",
             "missing_fields": []},
        ])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        risk = RowRisk(0, 57, "High", "high", [
            {"test_id": "same_day_payment", "test_name": "Same Day Capture and Payment",
             "severity": "error",
             "required_fields": ["date_captured", "scheduled_pay_date"]},
        ], 1, 17200000)
        row = {"date_captured": "12-11-2013", "scheduled_pay_date": "12-11-2013",
               "payment_date": "01-01-1900", "actual_pay_date": "01-01-1900"}
        result = investigate(row, risk, [row], (client, "mock"))
        first_prompt = json.loads(completions.calls[0]["messages"][1]["content"])
        self.assertNotIn("risk_score", first_prompt)
        self.assertNotIn("risk_band", first_prompt)
        self.assertNotIn("payment_date", first_prompt["source_fields"])
        self.assertNotIn("actual_pay_date", first_prompt["source_fields"])
        self.assertIn("scheduled", first_prompt["review_check"].lower())
        self.assertEqual(result["recommendation"], "request_evidence")
        self.assertEqual(result["missing_fields"], ["actual_pay_date"])
        self.assertEqual(result["schema_version"], 5)

    def test_dismissal_without_counter_evidence_requires_manual_review(self):
        completions = FakeCompletions([
            {"next": "evidence", "reason": "Check test evidence"},
            {"recommendation": "dismiss", "reason": "Test matched", "missing_fields": []},
        ])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        risk = RowRisk(0, 56, "High", "high", [
            {"test_id": "round_dollar", "test_name": "Round Dollar",
             "severity": "warning", "required_fields": ["amount_excl_lc"]},
        ], 1, 24000000)
        result = investigate({"amount_excl_lc": 24000000}, risk, [{}], (client, "mock"))
        self.assertEqual(result["recommendation"], "manual_review")
        self.assertTrue(any(s["step"] == "validation" for s in result["trace"]))

    def test_round_amount_uses_same_vendor_currency_peers_and_specific_check(self):
        completions = FakeCompletions([
            {"next": "conclude", "reason": "Round amount flagged"},
            {"recommendation": "request_evidence",
             "reason": "Need more information", "missing_fields": []},
        ])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        risk = RowRisk(0, 48, "Medium", "medium", [
            {"test_id": "round_dollar", "test_name": "Round Dollar",
             "severity": "warning", "required_fields": ["amount_excl_lc"]},
        ], 1, 9000)
        rows = [
            {"vendor_number": "V1", "reference_currency": "USD", "amount_excl_lc": 9000},
            {"vendor_number": "V1", "reference_currency": "USD", "amount_excl_lc": 1000},
            {"vendor_number": "V1", "reference_currency": "USD", "amount_excl_lc": 2000},
            {"vendor_number": "V1", "reference_currency": "USD", "amount_excl_lc": 3000},
            {"vendor_number": "V1", "reference_currency": "EUR", "amount_excl_lc": 100},
            {"vendor_number": "V2", "reference_currency": "USD", "amount_excl_lc": 100},
        ]
        result = investigate(rows[0], risk, rows, (client, "mock"))
        context = result["vendor_comparison"]
        self.assertEqual(context["peer_count"], 3)
        self.assertEqual(context["median_peer_amount"], 2000)
        self.assertEqual(context["amount_vs_median"], 4.5)
        self.assertEqual(result["recommendation"], "manual_review")
        self.assertEqual(result["review_action"], "investigate_vendor_outlier")
        self.assertIn("4.5×", result["reason"])
        self.assertIn("contract", result["suggested_check"])
        first = json.loads(completions.calls[0]["messages"][1]["content"])
        self.assertEqual(first["vendor_comparison"]["peer_count"], 3)
        self.assertNotIn("risk_score", first)
        self.assertNotIn("risk_band", first)

    def test_single_round_hit_cannot_be_confirmed_with_too_few_peers(self):
        completions = FakeCompletions([
            {"next": "conclude", "reason": "Round amount"},
            {"recommendation": "confirm",
             "reason": "Round invoice is suspicious", "missing_fields": []},
        ])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        risk = RowRisk(0, 52, "High", "high", [
            {"test_id": "round_dollar", "test_name": "Round Dollar",
             "severity": "warning", "required_fields": ["amount_excl_lc"]},
        ], 1, 10000000)
        rows = [
            {"vendor_number": "V1", "reference_currency": "USD", "amount_excl_lc": 10000000},
            {"vendor_number": "V1", "reference_currency": "USD", "amount_excl_lc": 2000000},
            {"vendor_number": "V1", "reference_currency": "USD", "amount_excl_lc": 3000000},
        ]
        result = investigate(rows[0], risk, rows, (client, "mock"))
        self.assertEqual(result["recommendation"], "manual_review")
        self.assertEqual(result["review_action"], "verify_documents")
        self.assertFalse(result["vendor_comparison"]["baseline_available"])
        self.assertIn("Only 2 comparable", result["reason"])

    def test_batch_lock_and_retry_preserve_completed_cases(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            run_store, "DB_PATH", Path(tmp) / "review.db"
        ):
            run_store.init_db()
            job = run_store.create_agent_review_job("run", 7, 5)
            self.assertIsNotNone(job)
            self.assertIsNone(run_store.create_agent_review_job("run", 8, 10))
            run_store.save_agent_review_case("run", 0, 7, {
                "schema_version": 5, "recommendation": "manual_review",
                "reason": "Needs human evidence",
            })
            run_store.update_agent_review_job(
                job, "failed", [{"row_idx": 0, "result": "manual_review"}], "Retry remaining"
            )
            self.assertEqual(run_store.get_agent_review_job(job, "run", 7)["status"], "failed")
            self.assertIsNone(run_store.get_agent_review_job(job, "run", 8))
            self.assertIsNotNone(run_store.create_agent_review_job("run", 7, 5))
            self.assertEqual(
                run_store.get_agent_review_cases("run", 7)[0]["recommendation"],
                "manual_review",
            )

    def test_transient_agent_failure_retries_once(self):
        import dia_agentic.app as app_module
        row = {"vendor_number": "V1", "amount_excl_lc": "100000"}
        session = {"results": [{
            "test_id": "round_dollar", "test_name": "Round",
            "severity": "error", "weightage": 10, "applicable": True,
            "hit_count": 1, "all_hit_indices": [0],
        }]}
        brief = {"schema_version": 5, "recommendation": "manual_review",
                 "reason": "Review the invoice", "trace": []}
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            run_store, "DB_PATH", Path(tmp) / "review.db"
        ), patch.object(app_module, "get_rows", return_value=[row]), patch.object(
            app_module, "get_last_test_session", return_value=session
        ), patch.object(
            app_module, "agent_investigate", side_effect=[ValueError("transient"), brief]
        ) as model:
            run_store.init_db()
            job = run_store.create_agent_review_job("run", 7, 5)
            app_module._process_agent_review_batch(job, "run", 7, 5)
            self.assertEqual(model.call_count, 2)
            self.assertEqual(run_store.get_agent_review_job(job, "run", 7)["status"], "completed")
            self.assertEqual(
                run_store.get_agent_review_cases("run", 7)[0]["recommendation"],
                "manual_review",
            )

    def test_authenticated_batch_to_approved_decision_on_disposable_db(self):
        from fastapi.testclient import TestClient
        import dia_agentic.app as app_module

        row = {"vendor_number": "V1", "vendor_name": "Test Vendor",
               "amount_excl_lc": 100000, "physical_invoice_no": "TEST-1",
               "date_captured": "12-11-2013", "scheduled_pay_date": "12-11-2013"}
        session = {"results": [{
            "test_id": "same_day_payment", "test_name": "Same Day Capture and Payment",
            "severity": "error", "weightage": 9, "type": "preset",
            "required_fields": ["date_captured", "scheduled_pay_date"],
            "applicable": True, "hit_count": 1, "all_hit_indices": [0],
        }]}
        result = {
            "schema_version": 5, "recommendation": "request_evidence",
            "missing_fields": ["actual_pay_date"], "reason": "Obtain payment record",
            "model": "mock", "trace": [], "risk_score": 60, "risk_band": "High",
            "vendor": "Test Vendor", "invoice": "TEST-1", "amount": 100000,
        }
        # Use a real row with a blank payment field for an approvable evidence request.
        row["actual_pay_date"] = "01-01-1900"
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            run_store, "DB_PATH", Path(tmp) / "review.db"
        ), patch.object(app_module, "get_rows", return_value=[row]), patch.object(
            app_module, "get_last_test_session", return_value=session
        ), patch.object(app_module, "agent_investigate", return_value=result):
            run_store.init_db()
            app_module.app.dependency_overrides[app_module.require_auth] = lambda: {
                "user_id": 7, "username": "test-reviewer"
            }
            try:
                with TestClient(app_module.app) as client:
                    started = client.post("/api/runs/run_test/agent-review/batch", json={"size": 5})
                    self.assertEqual(started.status_code, 200)
                    job = client.get(
                        f"/api/runs/run_test/agent-review/jobs/{started.json()['job']}"
                    ).json()
                    self.assertEqual(job["status"], "completed")
                    self.assertEqual(len(job["completed"]), 1)
                    brief = client.get("/api/runs/run_test/agent-review").json()
                    self.assertEqual(brief["remaining"], 0)
                    self.assertEqual(brief["cases"][0]["recommendation"], "request_evidence")
                    approved = client.post("/api/runs/run_test/agent-review/0/approve")
                    self.assertEqual(approved.json()["review_status"], "needs_info")
                    self.assertEqual(run_store.get_review_decision("run_test", 0)["status"], "needs_info")
                    filtered = client.get(
                        "/api/runs/run_test/agent-review?filter=needs_info"
                    ).json()
                    self.assertEqual(len(filtered["cases"]), 1)
            finally:
                app_module.app.dependency_overrides.clear()

    def test_disposable_upload_through_export(self):
        from fastapi.testclient import TestClient
        import dia_agentic.app as app_module
        from dia_agentic.core import schema_mapper

        registry = SimpleNamespace(
            exact_lookup=lambda *args, **kwargs: None,
            vector_search=lambda *args, **kwargs: None,
        )
        csv_bytes = (
            b"vendor_number,vendor_name,physical_invoice_no,amount_excl_lc,"
            b"date_captured,scheduled_pay_date,actual_pay_date,reference_currency\n"
            b"V1,Test High,TEST-H,1000000000001,12-11-2013,14-11-2013,12-11-2013,USD\n"
            b"V2,Test Low,TEST-L,1000,12-11-2013,14-11-2013,15-11-2013,USD\n"
        )
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            run_store, "DB_PATH", Path(tmp) / "review.db"
        ), patch.object(schema_mapper, "get_registry", return_value=registry):
            run_store.init_db()
            app_module.app.dependency_overrides[app_module.require_auth] = lambda: {
                "user_id": 7, "username": "test-reviewer"
            }
            try:
                with TestClient(app_module.app) as client:
                    uploaded = client.post(
                        "/api/validate",
                        files={"file": ("synthetic.csv", csv_bytes, "text/csv")},
                    )
                    self.assertEqual(uploaded.status_code, 200, uploaded.text)
                    report = uploaded.json()
                    run_id = report["run_id"]
                    self.assertEqual(report["total_rows"], 2)
                    tests = [test for test in report["preset_tests"]
                             if test["id"] in ("same_day_actual_payment", "round_dollar")]
                    executed = client.post(
                        f"/api/runs/{run_id}/tests/run", json={"tests": tests}
                    )
                    self.assertEqual(executed.status_code, 200, executed.text)
                    self.assertEqual(executed.json()["risk_summary"]["total_hit_rows"], 2)
                    queue = client.get(f"/api/runs/{run_id}/level1").json()
                    self.assertEqual(
                        queue["auto_approved"]["count"], 1,
                        (executed.json()["risk_summary"], queue["lanes"]),
                    )
                    self.assertEqual(queue["summary"]["pending"], 1)
                    with patch.object(app_module, "agent_investigate", return_value={
                        "schema_version": 5, "recommendation": "confirm",
                        "missing_fields": [], "reason": "Human review of matching dates",
                        "model": "mock", "trace": [], "risk_score": 60,
                        "risk_band": "High", "vendor": "Test High", "invoice": "TEST-H",
                        "amount": 1000000000001,
                    }):
                        started = client.post(
                            f"/api/runs/{run_id}/agent-review/batch", json={"size": 5}
                        )
                        self.assertEqual(started.status_code, 200, started.text)
                        job = client.get(
                            f"/api/runs/{run_id}/agent-review/jobs/{started.json()['job']}"
                        ).json()
                        self.assertEqual(job["status"], "completed")
                        row_idx = job["completed"][0]["row_idx"]
                    approved = client.post(
                        f"/api/runs/{run_id}/agent-review/{row_idx}/approve"
                    )
                    self.assertEqual(approved.json()["review_status"], "confirmed")
                    exported = client.get(f"/api/runs/{run_id}/export?status=confirmed")
                    self.assertEqual(exported.status_code, 200)
                    self.assertIn("TEST-H", exported.text)
                    self.assertNotIn("TEST-L", exported.text)
            finally:
                app_module.app.dependency_overrides.clear()


if __name__ == "__main__":
    unittest.main()
