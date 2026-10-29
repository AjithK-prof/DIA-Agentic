import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dia_agentic.core.date_utils import actual_payment_date, parse_invoice_date
from dia_agentic.core.evidence import evidence_for
from dia_agentic.core import run_store
from dia_agentic.core.preset_tests import PRESET_TESTS
from dia_agentic.core.test_builder import build_test, payment_timing_errors
from dia_agentic.core.test_executor import execute_test


PROMPT = "Find invoices paid before the documented invoice date"
SAFE_CODE = """def run_test(rows):
    hits = []
    for i, row in enumerate(rows):
        paid = actual_payment_date(row)
        documented = parse_invoice_date(row.get('date_documented'))
        if paid is not None and documented is not None and paid < documented:
            hits.append(i)
    return hits
"""
OLD_CODE = """def run_test(rows):
    from datetime import datetime
    def p(v):
        try: return datetime.strptime(str(v), '%d-%m-%Y').date()
        except ValueError: return None
    hits = []
    for i, row in enumerate(rows):
        pay = p(str(row.get('payment_date') or '').strip() or row.get('actual_pay_date'))
        documented = p(row.get('date_documented'))
        if pay is not None and documented is not None and pay < documented:
            hits.append(i)
    return hits
"""


class FakeCompletions:
    def __init__(self, codes):
        self.codes = iter(codes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        result = {
            "test_name": "Payment Before Invoice",
            "description": "Actual payment predates invoice documentation",
            "severity": "error",
            "required_fields": ["date_documented"],
            "explanation": "Payment before invoice documentation warrants investigation.",
            "code": next(self.codes),
        }
        return SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content=json.dumps(result)))
        ])


class GeneratedPaymentDateTests(unittest.TestCase):
    def test_shared_date_helper_falls_back_only_after_validity_check(self):
        self.assertIsNone(parse_invoice_date("01-01-1900"))
        self.assertIsNone(parse_invoice_date("1900-01-01"))
        self.assertIsNone(parse_invoice_date("01/01/1900"))
        self.assertEqual(str(actual_payment_date({
            "payment_date": "01-01-1900",
            "actual_pay_date": "05-01-2024",
            "scheduled_pay_date": "20-01-2024",
        })), "2024-01-05")
        self.assertIsNone(actual_payment_date({
            "payment_date": "1900-01-01", "actual_pay_date": None,
            "scheduled_pay_date": "05-01-2024",
        }))
        evidence = evidence_for("payment_before_invoice", {
            "date_documented": "10-01-2024", "payment_date": "01-01-1900",
            "actual_pay_date": "05-01-2024",
        })
        self.assertIn("actual_pay_date", {item["field"] for item in evidence})
        self.assertNotIn("payment_date", {item["field"] for item in evidence})

    def test_old_code_is_rejected_but_other_tests_still_run(self):
        self.assertTrue(payment_timing_errors(OLD_CODE, PROMPT))
        rows = [{
            "date_documented": "10-01-2024", "payment_date": "01-01-1900",
            "actual_pay_date": "05-01-2024", "amount_excl_lc": "1000",
        }]
        old = execute_test({
            "id": "payment_before_invoice", "type": "generated",
            "test_name": "Payment Before Invoice", "prompt": PROMPT, "code": OLD_CODE,
        }, rows)
        self.assertFalse(old["applicable"])
        self.assertIn("regeneration", old["not_applicable_reason"])
        round_result = execute_test(
            next(test for test in PRESET_TESTS if test["id"] == "round_dollar"), rows
        )
        self.assertEqual(round_result["all_hit_indices"], [0])

    def test_valid_code_passes_sentinel_and_fallback_fixtures(self):
        self.assertEqual(payment_timing_errors(SAFE_CODE, PROMPT), [])
        rows = [
            {"date_documented": "10-01-2024", "payment_date": "01-01-1900",
             "actual_pay_date": "05-01-2024"},
            {"date_documented": "10-01-2024", "payment_date": "1900-01-01",
             "actual_pay_date": None, "scheduled_pay_date": "05-01-2024"},
            {"date_documented": "01-01-1900", "payment_date": "05-01-2024"},
        ]
        result = execute_test({
            "id": "payment_before_invoice", "type": "generated",
            "test_name": "Payment Before Invoice", "prompt": PROMPT, "code": SAFE_CODE,
        }, rows)
        self.assertEqual(result["all_hit_indices"], [0])

    def test_generator_retries_then_rejects_invalid_payment_code(self):
        completions = FakeCompletions([OLD_CODE, OLD_CODE])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        with patch("dia_agentic.core.test_builder._get_code_client", return_value=(client, "mock")):
            result = build_test(PROMPT, [])
        self.assertIn("error", result)
        self.assertIn("payment-date", result["error"])
        self.assertEqual(len(completions.calls), 2)

    def test_generator_accepts_corrected_code_after_retry(self):
        completions = FakeCompletions([OLD_CODE, SAFE_CODE])
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        with patch("dia_agentic.core.test_builder._get_code_client", return_value=(client, "mock")):
            result = build_test(PROMPT, [])
        self.assertNotIn("error", result)
        self.assertEqual(result["code"], SAFE_CODE)
        self.assertEqual(result["validation_warnings"], [])
        self.assertEqual(len(completions.calls), 2)

    def test_generated_test_api_runs_without_changing_other_test_hits(self):
        from fastapi.testclient import TestClient
        import dia_agentic.app as app_module

        rows = [
            {"vendor_number": "V1", "date_documented": "10-01-2024",
             "payment_date": "01-01-1900", "actual_pay_date": "05-01-2024",
             "amount_excl_lc": "1250"},
            {"vendor_number": "V2", "date_documented": "10-01-2024",
             "payment_date": "01-01-1900", "actual_pay_date": None,
             "scheduled_pay_date": "05-01-2024", "amount_excl_lc": "1000"},
        ]
        completions = FakeCompletions([SAFE_CODE])
        client_stub = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            run_store, "DB_PATH", Path(tmp) / "audit.db"
        ), patch("dia_agentic.core.test_builder._get_code_client",
                 return_value=(client_stub, "mock")):
            run_store.init_db()
            run_store.save_run("run_generated", {"run_id": "run_generated"},
                               rows, "synthetic.csv", user_id=7)
            app_module.app.dependency_overrides[app_module.require_auth] = lambda: {
                "user_id": 7, "username": "test-reviewer"
            }
            try:
                with TestClient(app_module.app) as client:
                    generated = client.post(
                        "/api/runs/run_generated/tests/generate", json={"prompt": PROMPT}
                    )
                    self.assertEqual(generated.status_code, 200, generated.text)
                    tests = [generated.json(),
                             next(t for t in PRESET_TESTS if t["id"] == "round_dollar")]
                    run = client.post("/api/runs/run_generated/tests/run", json={"tests": tests})
                    self.assertEqual(run.status_code, 200, run.text)
                    self.assertEqual(run.json()["results"][0]["all_hit_indices"], [0])
                    self.assertEqual(run.json()["results"][1]["all_hit_indices"], [1])
            finally:
                app_module.app.dependency_overrides.clear()


if __name__ == "__main__":
    unittest.main()
