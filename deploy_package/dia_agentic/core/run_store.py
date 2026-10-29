import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "agentic_runs.db"


@contextmanager
def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


def init_db():
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id      TEXT PRIMARY KEY,
                user_id     INTEGER DEFAULT 0,
                created_at  TEXT,
                filename    TEXT,
                total_rows  INTEGER,
                report_json TEXT,
                rows_json   TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS test_sessions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id       TEXT,
                user_id      INTEGER DEFAULT 0,
                created_at   TEXT,
                tests_json   TEXT,
                results_json TEXT
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS saved_tests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER DEFAULT 0,
                name        TEXT NOT NULL,
                prompt      TEXT,
                code        TEXT,
                description TEXT,
                explanation TEXT,
                severity    TEXT DEFAULT 'warning',
                weightage   INTEGER DEFAULT 7,
                created_at  TEXT DEFAULT (datetime('now'))
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS review_decisions (
                run_id       TEXT,
                row_idx      INTEGER,
                user_id      INTEGER DEFAULT 0,
                status       TEXT DEFAULT 'pending',
                note         TEXT DEFAULT '',
                updated_at   TEXT,
                PRIMARY KEY (run_id, row_idx)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS ai_cache (
                run_id       TEXT,
                signature    TEXT,
                payload_json TEXT,
                created_at   TEXT,
                PRIMARY KEY (run_id, signature)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS level1_routing_decisions (
                run_id          TEXT,
                row_idx         INTEGER,
                user_id         INTEGER DEFAULT 0,
                suggested_route TEXT NOT NULL,
                final_route     TEXT NOT NULL,
                confidence      REAL NOT NULL,
                reason          TEXT NOT NULL,
                model           TEXT NOT NULL,
                answers_json    TEXT NOT NULL,
                state_json      TEXT NOT NULL,
                usage_json      TEXT NOT NULL,
                reviewer_action TEXT DEFAULT 'pending',
                reviewer_note   TEXT DEFAULT '',
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                PRIMARY KEY (run_id, row_idx)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS agent_review_cases (
                run_id TEXT NOT NULL,
                row_idx INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                result_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, row_idx, user_id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS agent_review_jobs (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                batch_size INTEGER NOT NULL,
                completed_json TEXT NOT NULL DEFAULT '[]',
                error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS payment_imports (
                run_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                total INTEGER NOT NULL,
                matched INTEGER NOT NULL,
                ambiguous INTEGER NOT NULL,
                unmatched INTEGER NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS payment_records (
                run_id TEXT NOT NULL,
                payment_idx INTEGER NOT NULL,
                match_status TEXT NOT NULL,
                candidate_count INTEGER NOT NULL,
                evidence_json TEXT NOT NULL,
                PRIMARY KEY (run_id, payment_idx)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS payment_candidates (
                run_id TEXT NOT NULL,
                row_idx INTEGER NOT NULL,
                payment_idx INTEGER NOT NULL,
                PRIMARY KEY (run_id, row_idx, payment_idx)
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS payment_candidates_invoice
            ON payment_candidates(run_id, row_idx)
        """)
        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS active_agent_review_job
            ON agent_review_jobs(run_id) WHERE status IN ('queued', 'running')
        """)
        c.commit()
        # migrate existing tables — add user_id if missing
        for tbl in ('runs', 'test_sessions', 'saved_tests', 'review_decisions'):
            try:
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN user_id INTEGER DEFAULT 0")
                c.commit()
            except Exception:
                pass


def get_review_decision(run_id: str, row_idx: int) -> dict:
    with _conn() as c:
        row = c.execute(
            "SELECT status, note FROM review_decisions WHERE run_id=? AND row_idx=?",
            (run_id, row_idx)
        ).fetchone()
    return {"status": row["status"], "note": row["note"]} if row else {"status": "pending", "note": ""}


def save_review_decision(run_id: str, row_idx: int, status: str, note: str):
    with _conn() as c:
        c.execute("""
            INSERT INTO review_decisions (run_id, row_idx, status, note, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(run_id, row_idx) DO UPDATE SET status=excluded.status, note=excluded.note, updated_at=excluded.updated_at
        """, (run_id, row_idx, status, note, datetime.now(timezone.utc).isoformat()))
        c.commit()


def list_saved_tests(user_id: int = 0) -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM saved_tests WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
    return [{k: v for k, v in dict(r).items() if k != "red_flag"} for r in rows]


def save_test_to_library(test: dict, user_id: int = 0) -> int:
    with _conn() as c:
        cur = c.execute("""
            INSERT INTO saved_tests (user_id, name, prompt, code, description, explanation, severity, weightage)
            VALUES (?,?,?,?,?,?,?,?)
        """, (user_id, test.get("test_name",""), test.get("prompt",""), test.get("code",""),
              test.get("description",""), test.get("explanation",""),
              test.get("severity","warning"), test.get("weightage",7)))
        c.commit()
        return cur.lastrowid


def delete_saved_test(test_id: int, user_id: int = 0):
    with _conn() as c:
        c.execute("DELETE FROM saved_tests WHERE id=? AND user_id=?", (test_id, user_id))
        c.commit()


def save_bulk_decisions(run_id: str, row_indices: list[int], status: str, note: str = ""):
    ts = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.executemany("""
            INSERT INTO review_decisions (run_id, row_idx, status, note, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(run_id, row_idx) DO UPDATE SET status=excluded.status, note=excluded.note, updated_at=excluded.updated_at
        """, [(run_id, int(i), status, note, ts) for i in row_indices])
        c.commit()


def get_ai_cache(run_id: str, signature: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT payload_json FROM ai_cache WHERE run_id=? AND signature=?",
            (run_id, signature)
        ).fetchone()
    return json.loads(row["payload_json"]) if row else None


def set_ai_cache(run_id: str, signature: str, payload: dict):
    with _conn() as c:
        c.execute("""
            INSERT INTO ai_cache (run_id, signature, payload_json, created_at)
            VALUES (?,?,?,?)
            ON CONFLICT(run_id, signature) DO UPDATE SET payload_json=excluded.payload_json, created_at=excluded.created_at
        """, (run_id, signature, json.dumps(payload), datetime.now(timezone.utc).isoformat()))
        c.commit()


def get_all_decisions(run_id: str) -> dict[int, dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT row_idx, status, note FROM review_decisions WHERE run_id=?", (run_id,)
        ).fetchall()
    return {r["row_idx"]: {"status": r["status"], "note": r["note"]} for r in rows}


def get_agent_review_cases(run_id: str, user_id: int) -> dict[int, dict]:
    with _conn() as c:
        records = c.execute(
            "SELECT row_idx, result_json, updated_at FROM agent_review_cases "
            "WHERE run_id=? AND user_id=? ORDER BY updated_at DESC",
            (run_id, user_id),
        ).fetchall()
    return {
        row["row_idx"]: {**json.loads(row["result_json"]), "updated_at": row["updated_at"]}
        for row in records
    }


def save_agent_review_case(run_id: str, row_idx: int, user_id: int, result: dict):
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.execute("""
            INSERT INTO agent_review_cases (run_id, row_idx, user_id, result_json, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(run_id, row_idx, user_id) DO UPDATE SET
                result_json=excluded.result_json, updated_at=excluded.updated_at
        """, (run_id, row_idx, user_id, json.dumps(result), now))
        c.commit()


def create_agent_review_job(run_id: str, user_id: int, size: int) -> str | None:
    """Only one active batch per run, including across users and app workers."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        # A killed worker cannot hold the run forever. Its saved cases remain.
        cutoff = (now - timedelta(minutes=15)).isoformat()
        c.execute("""
            UPDATE agent_review_jobs SET status='failed',
                error='Worker stopped or timed out; retry the remaining cases',
                updated_at=?
            WHERE run_id=? AND status IN ('queued', 'running') AND updated_at<?
        """, (now.isoformat(), run_id, cutoff))
        active = c.execute("""
            SELECT id FROM agent_review_jobs
            WHERE run_id=? AND status IN ('queued', 'running')
        """, (run_id,)).fetchone()
        if active:
            return None
        job_id = uuid.uuid4().hex
        c.execute("""
            INSERT INTO agent_review_jobs
            (id, run_id, user_id, status, batch_size, completed_json, created_at, updated_at)
            VALUES (?,?,?,'queued',?,'[]',?,?)
        """, (job_id, run_id, user_id, size, now.isoformat(), now.isoformat()))
        c.commit()
        return job_id


def update_agent_review_job(job_id: str, status: str, completed: list[dict],
                            error: str = ""):
    with _conn() as c:
        c.execute("""
            UPDATE agent_review_jobs
            SET status=?, completed_json=?, error=?, updated_at=?
            WHERE id=? AND status IN ('queued','running')
        """, (status, json.dumps(completed), error,
              datetime.now(timezone.utc).isoformat(), job_id))
        c.commit()


def _expire_agent_review_jobs(c, run_id: str):
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=15)).isoformat()
    c.execute("""
        UPDATE agent_review_jobs SET status='failed',
            error='Worker stopped or timed out; retry the remaining cases',
            updated_at=?
        WHERE run_id=? AND status IN ('queued','running') AND updated_at<?
    """, (now.isoformat(), run_id, cutoff))
    c.commit()


def get_agent_review_job(job_id: str, run_id: str, user_id: int) -> dict | None:
    with _conn() as c:
        _expire_agent_review_jobs(c, run_id)
        row = c.execute("""
            SELECT id, status, batch_size, completed_json, error, created_at, updated_at
            FROM agent_review_jobs WHERE id=? AND run_id=? AND user_id=?
        """, (job_id, run_id, user_id)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["completed"] = json.loads(result.pop("completed_json"))
    return result


def get_active_agent_review_job(run_id: str, user_id: int) -> dict | None:
    with _conn() as c:
        _expire_agent_review_jobs(c, run_id)
        row = c.execute("""
            SELECT id FROM agent_review_jobs
            WHERE run_id=? AND user_id=? AND status IN ('queued','running')
            ORDER BY created_at DESC LIMIT 1
        """, (run_id, user_id)).fetchone()
    return get_agent_review_job(row["id"], run_id, user_id) if row else None


def approve_agent_recommendation(run_id: str, row_idx: int, user_id: int,
                                 reviewer: str) -> str | None:
    """A human explicitly resolves a still-pending case; never overwrite a decision."""
    statuses = {"confirm": "confirmed", "dismiss": "false_positive",
                "request_evidence": "needs_info"}
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        case = c.execute("""
            SELECT result_json FROM agent_review_cases
            WHERE run_id=? AND row_idx=? AND user_id=?
        """, (run_id, row_idx, user_id)).fetchone()
        result = json.loads(case["result_json"]) if case else {}
        if result.get("schema_version") != 5:
            return None
        status = statuses.get(result.get("recommendation"))
        if status == "needs_info" and not result.get("missing_fields"):
            return None
        current = c.execute("""
            SELECT status FROM review_decisions WHERE run_id=? AND row_idx=?
        """, (run_id, row_idx)).fetchone()
        if not status or (current and current["status"] != "pending"):
            return None
        note = (
            f"Agent recommendation approved by {reviewer}: "
            f"{result['recommendation']}. {result.get('reason', '')[:500]}"
        )
        c.execute("""
            INSERT INTO review_decisions (run_id, row_idx, user_id, status, note, updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(run_id, row_idx) DO UPDATE SET
                status=excluded.status, note=excluded.note, updated_at=excluded.updated_at
        """, (run_id, row_idx, user_id, status, note,
              datetime.now(timezone.utc).isoformat()))
        c.commit()
        return status


def save_level1_routes(run_id: str, routes: list[dict], user_id: int = 0):
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.executemany("""
            INSERT INTO level1_routing_decisions (
                run_id, row_idx, user_id, suggested_route, final_route,
                confidence, reason, model, answers_json, state_json, usage_json,
                reviewer_action, reviewer_note, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id, row_idx) DO UPDATE SET
                suggested_route=excluded.suggested_route,
                final_route=excluded.suggested_route,
                confidence=excluded.confidence,
                reason=excluded.reason,
                model=excluded.model,
                answers_json=excluded.answers_json,
                state_json=excluded.state_json,
                usage_json=excluded.usage_json,
                reviewer_action='pending',
                reviewer_note='',
                updated_at=excluded.updated_at
            WHERE level1_routing_decisions.reviewer_action='pending'
        """, [(
            run_id, int(route["row_idx"]), user_id, route["suggested_route"],
            route["suggested_route"], float(route["confidence"]), route["reason"],
            route["model"], json.dumps(route["answers"]), json.dumps(route["state"]),
            json.dumps(route.get("usage") or {}), "pending", "", now, now,
        ) for route in routes])
        c.commit()


def get_level1_routes(run_id: str, user_id: int = 0) -> dict[int, dict]:
    with _conn() as c:
        rows = c.execute("""
            SELECT row_idx, suggested_route, final_route, confidence, reason,
                   model, answers_json, reviewer_action, reviewer_note,
                   created_at, updated_at
            FROM level1_routing_decisions
            WHERE run_id=? AND user_id=?
            ORDER BY row_idx
        """, (run_id, user_id)).fetchall()
    result = {}
    for row in rows:
        item = dict(row)
        item["answers"] = json.loads(item.pop("answers_json"))
        result[item["row_idx"]] = item
    return result


def auto_approve_clear_routes(run_id: str, row_indices: list[int],
                              model: str, user_id: int = 0) -> int:
    """Atomically resolve only still-pending, policy-matching clear routes."""
    if not row_indices:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    note = f"Auto-approved by review policy {model}: low risk, one warning-level test, evidence complete."
    approved = 0
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        for row_idx in row_indices:
            cur = c.execute("""
                UPDATE level1_routing_decisions
                SET reviewer_action='auto_approved', reviewer_note=?, updated_at=?
                WHERE run_id=? AND row_idx=? AND user_id=?
                  AND reviewer_action='pending' AND final_route='auto_clear'
                  AND model=?
                  AND NOT EXISTS (
                      SELECT 1 FROM review_decisions d
                      WHERE d.run_id=? AND d.row_idx=level1_routing_decisions.row_idx
                        AND d.status!='pending'
                  )
            """, (note, now, run_id, row_idx, user_id, model, run_id))
            if cur.rowcount:
                c.execute("""
                    INSERT INTO review_decisions (run_id, row_idx, user_id, status, note, updated_at)
                    VALUES (?,?,?,'false_positive',?,?)
                    ON CONFLICT(run_id, row_idx) DO UPDATE SET
                        status=excluded.status, note=excluded.note, updated_at=excluded.updated_at
                """, (run_id, row_idx, user_id, note, now))
                approved += 1
        c.commit()
    return approved


def reopen_auto_approved(run_id: str, row_idx: int, user_id: int = 0) -> bool:
    """Put an automatic decision into manual review without auto-approving it again."""
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        cur = c.execute("""
            UPDATE level1_routing_decisions
            SET final_route='needs_evidence', reviewer_action='reopened',
                reviewer_note='Reopened by reviewer for manual assessment', updated_at=?
            WHERE run_id=? AND row_idx=? AND user_id=?
              AND reviewer_action='auto_approved'
        """, (now, run_id, row_idx, user_id))
        if cur.rowcount:
            c.execute("""
                UPDATE review_decisions SET status='pending',
                    note='Auto-approval reopened for manual review', updated_at=?
                WHERE run_id=? AND row_idx=?
            """, (now, run_id, row_idx))
        c.commit()
        return bool(cur.rowcount)


def update_level1_route(
    run_id: str,
    row_idx: int,
    final_route: str,
    reviewer_action: str,
    reviewer_note: str,
    user_id: int = 0,
) -> bool:
    with _conn() as c:
        cur = c.execute("""
            UPDATE level1_routing_decisions
            SET final_route=?, reviewer_action=?, reviewer_note=?, updated_at=?
            WHERE run_id=? AND row_idx=? AND user_id=?
        """, (
            final_route, reviewer_action, reviewer_note,
            datetime.now(timezone.utc).isoformat(), run_id, row_idx, user_id,
        ))
        c.commit()
        return cur.rowcount > 0


def bulk_update_level1_routes(
    run_id: str,
    row_indices: list[int],
    reviewer_action: str,
    reviewer_note: str,
    user_id: int = 0,
) -> int:
    """Accept/override many routes at once, keeping each row's suggested route."""
    if not row_indices:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as c:
        cur = c.executemany("""
            UPDATE level1_routing_decisions
            SET final_route=suggested_route, reviewer_action=?, reviewer_note=?, updated_at=?
            WHERE run_id=? AND row_idx=? AND user_id=?
        """, [
            (reviewer_action, reviewer_note, now, run_id, int(i), user_id)
            for i in row_indices
        ])
        c.commit()
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(row_indices)


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:12]


def save_run(run_id: str, report: dict, rows: list[dict], filename: str, user_id: int = 0):
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO runs (run_id, user_id, created_at, filename, total_rows, report_json, rows_json) VALUES (?,?,?,?,?,?,?)",
            (run_id, user_id, datetime.now(timezone.utc).isoformat(), filename, len(rows),
             json.dumps(report), json.dumps(rows)),
        )
        c.commit()


def get_rows(run_id: str) -> list[dict]:
    with _conn() as c:
        row = c.execute("SELECT rows_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return json.loads(row["rows_json"]) if row else []


def get_run(run_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT report_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return json.loads(row["report_json"]) if row else None


def run_belongs_to_user(run_id: str, user_id: int) -> bool:
    with _conn() as c:
        return c.execute(
            "SELECT 1 FROM runs WHERE run_id=? AND user_id=?", (run_id, user_id)
        ).fetchone() is not None


def run_row_count(run_id: str, user_id: int) -> int | None:
    with _conn() as c:
        row = c.execute(
            "SELECT total_rows FROM runs WHERE run_id=? AND user_id=?",
            (run_id, user_id),
        ).fetchone()
    return row["total_rows"] if row else None


def save_payment_import(run_id: str, user_id: int, filename: str,
                        summary: dict, records: list, links: list):
    """Replace only this run's evidence atomically; do not alter audit sessions."""
    with _conn() as c:
        c.execute("BEGIN IMMEDIATE")
        if not c.execute("SELECT 1 FROM runs WHERE run_id=? AND user_id=?",
                         (run_id, user_id)).fetchone():
            raise ValueError("Run not found for this user")
        c.execute("DELETE FROM payment_candidates WHERE run_id=?", (run_id,))
        c.execute("DELETE FROM payment_records WHERE run_id=?", (run_id,))
        c.executemany(
            "INSERT INTO payment_records VALUES (?,?,?,?,?)",
            ((run_id, idx, status, count, json.dumps(evidence))
             for idx, status, count, evidence in records),
        )
        c.executemany(
            "INSERT INTO payment_candidates VALUES (?,?,?)",
            ((run_id, row_idx, payment_idx) for row_idx, payment_idx in links),
        )
        c.execute("""
            INSERT INTO payment_imports
            (run_id, user_id, filename, imported_at, total, matched, ambiguous, unmatched)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id) DO UPDATE SET
                user_id=excluded.user_id, filename=excluded.filename,
                imported_at=excluded.imported_at, total=excluded.total,
                matched=excluded.matched, ambiguous=excluded.ambiguous,
                unmatched=excluded.unmatched
        """, (run_id, user_id, Path(filename).name[:120],
              datetime.now(timezone.utc).isoformat(),
              summary["total"], summary["matched"], summary["ambiguous"], summary["unmatched"]))
        c.commit()


def get_payment_import(run_id: str, user_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT filename, imported_at, total, matched, ambiguous, unmatched "
            "FROM payment_imports WHERE run_id=? AND user_id=?",
            (run_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def get_invoice_payment_evidence(run_id: str, user_id: int, row_idx: int,
                                 limit: int = 20) -> dict:
    summary = get_payment_import(run_id, user_id)
    if summary is None:
        return {"imported": False, "records": [], "total": 0}
    with _conn() as c:
        total = c.execute(
            "SELECT COUNT(*) FROM payment_candidates WHERE run_id=? AND row_idx=?",
            (run_id, row_idx),
        ).fetchone()[0]
        rows = c.execute("""
            SELECT r.match_status, r.candidate_count, r.evidence_json
            FROM payment_candidates AS p
            JOIN payment_records AS r ON r.run_id=p.run_id AND r.payment_idx=p.payment_idx
            WHERE p.run_id=? AND p.row_idx=? ORDER BY p.payment_idx LIMIT ?
        """, (run_id, row_idx, limit)).fetchall()
    return {
        "imported": True, "total": total, "truncated": total > limit,
        "records": [{**json.loads(row["evidence_json"]),
                     "match_status": row["match_status"],
                     "candidate_count": row["candidate_count"]} for row in rows],
        "source": {"filename": summary["filename"], "imported_at": summary["imported_at"]},
    }


def list_runs(limit: int = 20, user_id: int = 0) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT run_id, created_at, filename, total_rows FROM runs WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def save_test_session(run_id: str, tests: list[dict], results: list[dict], user_id: int = 0):
    with _conn() as c:
        c.execute(
            "INSERT INTO test_sessions (run_id, user_id, created_at, tests_json, results_json) VALUES (?,?,?,?,?)",
            (run_id, user_id, datetime.now(timezone.utc).isoformat(), json.dumps(tests), json.dumps(results)),
        )
        c.commit()


def get_last_test_session(run_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT tests_json, results_json FROM test_sessions WHERE run_id=? ORDER BY created_at DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    if not row:
        return None
    # Historical sessions can still carry the retired field. Keep stored audit
    # snapshots intact, but do not expose it to current scoring or the UI.
    tests = json.loads(row["tests_json"])
    results = json.loads(row["results_json"])
    for item in tests + results:
        item.pop("red_flag", None)
    return {"tests": tests, "results": results}
