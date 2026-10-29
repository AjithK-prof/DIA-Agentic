import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from dia_agentic.core import run_store
from dia_agentic.core.payment_link import match_payments


INVOICES = [
    {"company_code": "C1", "system_invoice_no": "INV1",
     "vendor_number": "000123", "fiscal_year": "2024", "amount_excl_lc": "100"},
    {"company_code": "C1", "system_invoice_no": "INV2",
     "vendor_number": "V2", "fiscal_year": "2024"},
    {"company_code": "C1", "system_invoice_no": "INV2",
     "vendor_number": "V2", "fiscal_year": "2024"},
    {"company_code": "C1", "system_invoice_no": "INV3",
     "vendor_number": "V3", "fiscal_year": "2024"},
]
HEADER = ("CompanyCode,SystemInvoiceNo,VendorNumber,FiscalYear,SystemPaymentNo,"
          "PaymentStatus,DocumentType,DatePay,DateCleared,AmountIncl_LC,CurrencyCode,"
          "BankAccount,IBAN,PaymentDescription,FraudFlag\n")
PAYMENTS = HEADER + (
    "C1,INV1,123,2024,P1,Cleared,KZ-Vendor Payment,01-01-2024,02-01-2024,100,USD,secret-1,secret-2,private detail,1\n"
    "C1,INV1,000123,2024,P2,Voided,KZ-Vendor Payment,03-01-2024,,100,USD,,,,\n"
    "C1,INV2,V2,2024,P3,Cleared,KR-Vendor Invoice,1900-01-01,,200,USD,,,,\n"
    "C1,INV-X,V3,2024,P4,Cleared,KZ-Vendor Payment,05-01-2024,,300,USD,,,,\n"
    "C1,INV3,V3,,P5,Cleared,KZ-Vendor Payment,05-01-2024,,300,USD,,,,\n"
)


class PaymentLinkTests(unittest.TestCase):
    def test_matches_only_unique_complete_keys_without_assuming_paid(self):
        summary, records, links = match_payments(INVOICES, io.StringIO(PAYMENTS))
        self.assertEqual(summary, {"total": 5, "matched": 2,
                                   "ambiguous": 1, "unmatched": 2})
        self.assertEqual(records[1][1], "matched")  # Voided still evidence, not cleared
        self.assertEqual(records[2][1:3], ("ambiguous", 2))
        self.assertIsNone(records[2][3]["date_pay"])
        self.assertEqual(links, [(0, 0), (0, 1), (1, 2), (2, 2)])
        self.assertNotIn("IBAN", str(records))
        self.assertNotIn("secret", str(records))
        self.assertNotIn("private detail", str(records))
        self.assertEqual(records[0][3]["date_pay"], "2024-01-01")

    def test_invalid_upload_is_rejected_without_partial_import(self):
        with self.assertRaisesRegex(ValueError, "Missing payment columns"):
            match_payments(INVOICES, io.StringIO("CompanyCode,SystemInvoiceNo\nC1,INV1\n"))
        with self.assertRaisesRegex(ValueError, "no data rows"):
            match_payments(INVOICES, io.StringIO(HEADER))

    def test_authenticated_import_evidence_replacement_and_no_audit_changes(self):
        import dia_agentic.app as app_module
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            run_store, "DB_PATH", Path(tmp) / "pay.db"
        ):
            run_store.init_db()
            run_store.save_run("run_pay", {"run_id": "run_pay"}, INVOICES,
                               "invoices.csv", user_id=7)
            run_store.save_test_session("run_pay", [], [{
                "test_id": "audit", "test_name": "Audit",
                "applicable": True, "hit_count": 1,
                "all_hit_indices": [0], "weightage": 8,
            }], user_id=7)
            run_store.save_review_decision("run_pay", 0, "confirmed", "Reviewed")
            app_module.app.dependency_overrides[app_module.require_auth] = lambda: {
                "user_id": 7, "username": "reviewer"
            }
            try:
                with TestClient(app_module.app) as client:
                    url = "/api/runs/run_pay/payments"
                    upload = client.post(url, files={"file": ("payments.csv", PAYMENTS, "text/csv")})
                    self.assertEqual(upload.status_code, 200, upload.text)
                    self.assertEqual(upload.json()["ambiguous"], 1)
                    evidence = client.get(url + "/invoices/0")
                    self.assertEqual(evidence.status_code, 200)
                    self.assertEqual(evidence.json()["total"], 2)
                    self.assertEqual(evidence.json()["records"][1]["payment_status"], "Voided")
                    self.assertNotIn("BankAccount", evidence.text)
                    self.assertNotIn("secret", evidence.text)
                    ambiguous = client.get(url + "/invoices/1").json()
                    self.assertEqual(ambiguous["records"][0]["match_status"], "ambiguous")
                    self.assertEqual(ambiguous["records"][0]["candidate_count"], 2)
                    self.assertEqual(client.get(url + "/invoices/3").json()["records"], [])
                    self.assertEqual(client.get(url + "/invoices/99").status_code, 404)
                    failed = client.post(url, files={"file": ("bad.csv", "bad\n1\n", "text/csv")})
                    self.assertEqual(failed.status_code, 422)
                    self.assertEqual(client.get(url).json()["total"], 5)
                    replacement = client.post(url, files={
                        "file": ("replacement.csv", HEADER + PAYMENTS.splitlines(True)[1], "text/csv")
                    })
                    self.assertEqual(replacement.status_code, 200)
                    self.assertEqual(client.get(url + "/invoices/0").json()["total"], 1)
                    self.assertEqual(client.get(url + "/invoices/1").json()["total"], 0)
                    self.assertEqual(run_store.get_review_decision("run_pay", 0)["status"], "confirmed")
                    self.assertEqual(len(run_store.get_last_test_session("run_pay")["results"]), 1)
                    self.assertEqual(client.get("/api/runs/run_pay/review?per_page=1").status_code, 200)
                    app_module.app.dependency_overrides[app_module.require_auth] = lambda: {
                        "user_id": 8, "username": "other"
                    }
                    self.assertEqual(client.get(url).status_code, 404)
                    self.assertEqual(client.get(url + "/invoices/0").status_code, 404)
                    self.assertEqual(client.post(url, files={
                        "file": ("payments.csv", PAYMENTS, "text/csv")
                    }).status_code, 404)
            finally:
                app_module.app.dependency_overrides.clear()


if __name__ == "__main__":
    unittest.main()
