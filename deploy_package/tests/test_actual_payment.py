import unittest

from dia_agentic.core.data_quality import analyse
from dia_agentic.core.evidence import evidence_for
from dia_agentic.core.preset_tests import PRESET_TESTS
from dia_agentic.core.test_executor import execute_test


class ActualPaymentPresetTests(unittest.TestCase):
    def test_only_valid_actual_payment_dates_can_match(self):
        preset = next(t for t in PRESET_TESTS if t["id"] == "same_day_actual_payment")
        rows = [
            {"date_captured": "12-11-2013", "scheduled_pay_date": "14-11-2013",
             "payment_date": "12-11-2013", "amount_excl_lc": "10000"},
            {"date_captured": "12-11-2013", "scheduled_pay_date": "14-11-2013",
             "payment_date": "01-01-1900", "actual_pay_date": "12-11-2013",
             "amount_excl_lc": "20000"},
            {"date_captured": "12-11-2013", "scheduled_pay_date": "12-11-2013",
             "payment_date": "1900-01-01", "actual_pay_date": "",
             "amount_excl_lc": "30000"},
            {"date_captured": "01/01/1900", "payment_date": "12-11-2013",
             "amount_excl_lc": "40000"},
            {"date_captured": "12-11-2013", "payment_date": "13-11-2013",
             "amount_excl_lc": "50000"},
            {"date_captured": "12-11-2013", "payment_date": "13-11-2013",
             "actual_pay_date": "12-11-2013", "amount_excl_lc": "60000"},
        ]
        result = execute_test(preset, rows)
        self.assertEqual(result["all_hit_indices"], [0, 1])
        self.assertEqual(result["hit_count"], 2)
        self.assertEqual(result["skipped_missing_dates"], 2)
        evidence = evidence_for(preset["id"], rows[1])
        self.assertEqual([item["field"] for item in evidence],
                         ["date_captured", "actual_pay_date"])
        self.assertIn("actually paid", evidence[1]["note"])
        self.assertEqual(analyse(rows)["field_stats"]["payment_date"]["sentinel_count"], 2)

    def test_all_missing_payment_dates_marks_test_not_applicable(self):
        preset = next(t for t in PRESET_TESTS if t["id"] == "same_day_actual_payment")
        result = execute_test(preset, [{
            "date_captured": "12-11-2013", "scheduled_pay_date": "12-11-2013",
            "payment_date": "01-01-1900", "actual_pay_date": None,
            "amount_excl_lc": "100000",
        }])
        self.assertFalse(result["applicable"])
        self.assertEqual(result["hit_count"], 0)
        self.assertEqual(result["skipped_missing_dates"], 1)
        self.assertIn("actual payment date", result["not_applicable_reason"])

    def test_historical_scheduled_evidence_is_not_relabelled(self):
        evidence = evidence_for("same_day_payment", {
            "date_captured": "12-11-2013", "scheduled_pay_date": "12-11-2013"
        })
        self.assertIn("scheduled", evidence[1]["note"])
        self.assertIn("legacy", evidence[1]["note"])


if __name__ == "__main__":
    unittest.main()
