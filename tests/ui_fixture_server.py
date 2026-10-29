"""Isolated, synthetic UI fixture for manual Agent Review browser checks."""
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import uvicorn

from dia_agentic.core import auth, run_store
from dia_agentic.core.preset_tests import PRESET_TESTS
from dia_agentic.core.test_executor import execute_test


def serve():
    with tempfile.TemporaryDirectory(prefix="dia-agent-review-ui-") as directory:
        db = Path(directory) / "fixture.db"
        auth.DB_PATH = db
        run_store.DB_PATH = db
        os.environ["DIA_DEFAULT_USER"] = "synthetic-reviewer"
        os.environ["DIA_DEFAULT_PASSWORD"] = "fixture-only"
        import dia_agentic.app as app_module

        now = datetime.now(timezone.utc)
        with sqlite3.connect(str(db)) as conn:
            conn.execute("""
                INSERT INTO sessions (token, user_id, username, created_at, expires_at)
                VALUES (?,?,?,?,?)
            """, ("local-fixture-token", 1, "synthetic-reviewer",
                  now.isoformat(), (now + timedelta(hours=2)).isoformat()))
            conn.commit()

        rows = [
            {
                "vendor_number": "V1", "vendor_name": "Synthetic High",
                "physical_invoice_no": "TEST-H", "amount_excl_lc": "1000000000001",
                "date_captured": "12-11-2013", "scheduled_pay_date": "14-11-2013",
                "actual_pay_date": "12-11-2013", "reference_currency": "USD",
            },
            {
                "vendor_number": "V2", "vendor_name": "Synthetic Low",
                "physical_invoice_no": "TEST-L", "amount_excl_lc": "1000",
                "date_captured": "12-11-2013", "scheduled_pay_date": "14-11-2013",
                "actual_pay_date": "15-11-2013", "reference_currency": "USD",
            },
        ]
        tests = [test for test in PRESET_TESTS
                 if test["id"] in ("same_day_actual_payment", "round_dollar")]
        run_id = "run_synthetic_review"
        run_store.save_run(run_id, {"run_id": run_id}, rows, "synthetic.csv", user_id=1)
        run_store.save_test_session(
            run_id, tests, [execute_test(test, rows) for test in tests], user_id=1
        )
        uvicorn.run(app_module.app, host="127.0.0.1", port=8078, log_level="warning")


if __name__ == "__main__":
    serve()
