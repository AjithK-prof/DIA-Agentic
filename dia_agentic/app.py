import asyncio
import csv
import io
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from dia_agentic.core.ingestor import ingest_csv
from dia_agentic.core.preset_tests import PRESET_TESTS
from dia_agentic.core.test_builder import build_test
from dia_agentic.core.test_executor import execute_test
from dia_agentic.core.risk_scorer import compute_risk_scores
from dia_agentic.core.data_quality import analyse, compare_runs
from dia_agentic.core.run_store import (
    init_db, new_run_id, save_run, get_rows, get_run,
    list_runs, save_test_session, get_last_test_session,
    save_review_decision, get_all_decisions, save_bulk_decisions,
    get_ai_cache, set_ai_cache,
    list_saved_tests, save_test_to_library, delete_saved_test,
    save_level1_routes, get_level1_routes, update_level1_route,
    bulk_update_level1_routes, auto_approve_clear_routes, reopen_auto_approved,
    get_agent_review_cases, save_agent_review_case, approve_agent_recommendation,
    create_agent_review_job, update_agent_review_job, get_agent_review_job,
    get_active_agent_review_job,
    run_belongs_to_user, run_row_count, save_payment_import, get_payment_import,
    get_invoice_payment_evidence,
)
from dia_agentic.core.payment_link import match_payments
from dia_agentic.core.cluster_engine import build_clusters
from dia_agentic.core.anomaly_detect import detect_anomalies
from dia_agentic.core.ai_review import (
    generate_triage, polish_finding, polish_investigation, suggest_tests,
)
from dia_agentic.core.evidence import evidence_for
from dia_agentic.core.risk_links import build_connected_risks
from dia_agentic.core.investigate import build_investigation_facts, deterministic_investigation
from dia_agentic.core.finding_report import (
    build_finding_facts, deterministic_finding, finding_signature, render_plain_text,
)
from dia_agentic.core.chat_agent import answer_chat
# TypeSafe JEV is no longer a runtime dependency — Level-1 now routes with the
# deterministic rules in auto_router.py. jev_router.py is kept for reference.
# from dia_agentic.core.jev_router import (
#     JevError, MAX_BATCH_SIZE as JEV_MAX_BATCH_SIZE,
#     is_configured as jev_is_configured, route_invoices,
# )
from dia_agentic.core.auto_router import (
    LANE_LABELS, ROUTES, ROUTE_TO_REVIEW_STATUS, ROUTER_VERSION,
    route_invoices as auto_route_invoices,
)
from dia_agentic.core.agent_review import investigate as agent_investigate
from dia_agentic.core.auth import (
    init_auth_tables, seed_default_user, login as auth_login,
    logout as auth_logout, require_auth, create_user, list_users, change_password,
)

# Load .env
from pathlib import Path as _P
import os as _os
_env = _P(__file__).parent.parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            _os.environ.setdefault(_k.strip(), _v.strip())

app = FastAPI(title="Digital Integrity Analytics")
init_db()
init_auth_tables()
seed_default_user()

_SERVER_SESSION = str(uuid.uuid4())

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


def _row_evidence(flagged_by: list, row: dict) -> list:
    """Combine evidence from every test that flagged this row, deduplicated by field.

    For preset tests: evidence_for() returns hardcoded, specific evidence.
    For AI-generated tests: _generic_evidence() inside evidence_for() infers
    evidence from the test_id keywords.

    The required_fields fallback is intentionally removed: it produced misleading
    "flagged by X" labels on fields that the test code had actually skipped
    (e.g. blank payment dates on payment-timing tests).
    """
    seen_fields = set()
    result = []
    for fb in flagged_by:
        specific = evidence_for(fb.get("test_id", ""), row)
        for ev in specific:
            if ev["field"] not in seen_fields:
                seen_fields.add(ev["field"])
                result.append(ev)
    return result


@app.get("/")
def home():
    return FileResponse(static_dir / "index.html")

@app.get("/login")
def login_page():
    return FileResponse(static_dir / "login.html")

# ── Auth endpoints ───────────────────────────────────────────────────────────

@app.post("/api/auth/login")
async def api_login(request: Request):
    body = await request.json()
    result = auth_login(body.get("username",""), body.get("password",""))
    if not result:
        return JSONResponse({"error": "Invalid username or password"}, status_code=401)
    return result

@app.post("/api/auth/logout")
async def api_logout(request: Request, sess=Depends(require_auth)):
    token = request.headers.get("x-auth-token","")
    auth_logout(token)
    return {"ok": True}

@app.get("/api/auth/me")
def api_me(sess=Depends(require_auth)):
    return sess

@app.post("/api/auth/users")
async def api_create_user(request: Request, sess=Depends(require_auth)):
    body = await request.json()
    ok = create_user(body.get("username",""), body.get("password",""), body.get("display_name",""))
    if not ok:
        return JSONResponse({"error": "Username already exists"}, status_code=400)
    return {"ok": True}

@app.get("/api/auth/users")
def api_list_users(sess=Depends(require_auth)):
    return list_users()

@app.post("/api/auth/change-password")
async def api_change_password(request: Request, sess=Depends(require_auth)):
    body = await request.json()
    change_password(sess["user_id"], body.get("new_password",""))
    return {"ok": True}

# ── Session (kept for compatibility) ─────────────────────────────────────────

@app.get("/api/session")
def session():
    return {"session": _SERVER_SESSION}

@app.get("/api/runs")
def runs_list(limit: int = 20, sess=Depends(require_auth)):
    return list_runs(limit, user_id=sess["user_id"])

@app.get("/api/runs/{run_id}")
def run_detail(run_id: str, sess=Depends(require_auth)):
    r = get_run(run_id)
    if not r:
        return JSONResponse({"error": "not found"}, status_code=404)
    return r


@app.post("/api/runs/{run_id}/payments")
def import_payments(run_id: str, file: UploadFile = File(...),
                    sess=Depends(require_auth)):
    """Attach sanitized payment evidence, without rerunning or changing tests."""
    if not run_belongs_to_user(run_id, sess["user_id"]):
        return JSONResponse({"error": "Run not found"}, status_code=404)
    if Path(file.filename or "").suffix.lower() != ".csv":
        return JSONResponse({"error": "Upload a payments CSV file"}, status_code=400)
    file.file.seek(0, 2)
    if file.file.tell() > 40 * 1024 * 1024:
        return JSONResponse({"error": "Payment CSV exceeds 40 MB"}, status_code=413)
    file.file.seek(0)
    try:
        with io.TextIOWrapper(file.file, encoding="utf-8-sig", errors="strict",
                              newline="") as stream:
            summary, records, links = match_payments(get_rows(run_id), stream)
    except (ValueError, UnicodeError, csv.Error) as exc:
        return JSONResponse({"error": str(exc)[:200]}, status_code=422)
    save_payment_import(run_id, sess["user_id"], file.filename or "payments.csv",
                        summary, records, links)
    return {**summary, "imported": True,
            "note": "Evidence only. Existing test hits, risk scores and decisions are unchanged."}


@app.get("/api/runs/{run_id}/payments")
def payment_import_summary(run_id: str, sess=Depends(require_auth)):
    if not run_belongs_to_user(run_id, sess["user_id"]):
        return JSONResponse({"error": "Run not found"}, status_code=404)
    result = get_payment_import(run_id, sess["user_id"])
    return {"imported": bool(result), **(result or {})}


@app.get("/api/runs/{run_id}/payments/invoices/{row_idx}")
def invoice_payments(run_id: str, row_idx: int, sess=Depends(require_auth)):
    if not run_belongs_to_user(run_id, sess["user_id"]):
        return JSONResponse({"error": "Run not found"}, status_code=404)
    if row_idx < 0:
        return JSONResponse({"error": "Invoice row not found"}, status_code=404)
    if row_idx >= run_row_count(run_id, sess["user_id"]):
        return JSONResponse({"error": "Invoice row not found"}, status_code=404)
    return get_invoice_payment_evidence(run_id, sess["user_id"], row_idx)


@app.post("/api/validate")
async def validate(
    file: UploadFile = File(...),
    use_llm: bool = Form(False),
    erp_type: str = Form(""),
    sess=Depends(require_auth),
):
    suffix = Path(file.filename or "upload.csv").suffix or ".csv"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file.file, tmp)
        temp_path = tmp.name

    try:
        ingest = ingest_csv(temp_path, use_llm_fallback=use_llm, erp_type=erp_type or None)
    finally:
        Path(temp_path).unlink(missing_ok=True)

    run_id = new_run_id()

    mapped_fields = {k for row in ingest.rows for k, v in row.items() if v is not None}
    applicable_presets = []
    for t in PRESET_TESTS:
        missing = [f for f in t.get("required_fields", []) if f not in mapped_fields]
        if (t["id"] == "same_day_actual_payment"
                and not ({"payment_date", "actual_pay_date"} & mapped_fields)):
            missing.append("payment_date or actual_pay_date")
        if missing:
            applicable_presets.append({**t, "applicable": False, "missing_fields": missing})
        else:
            applicable_presets.append({**t, "applicable": True, "missing_fields": []})

    dq = analyse(ingest.rows)

    report = {
        "run_id": run_id,
        "source_file": file.filename,
        "total_rows": ingest.total_rows,
        "schema_mapping": ingest.mapping_result,
        "preset_tests": applicable_presets,
        "sample_rows": ingest.rows[:5],
        "data_quality": dq,
    }

    save_run(run_id, report, ingest.rows, file.filename or "upload.csv", user_id=sess["user_id"])
    return report


@app.post("/api/runs/{run_id}/tests/generate")
async def generate_test(run_id: str, request: Request, sess=Depends(require_auth)):
    body = await request.json()
    prompt = body.get("prompt", "").strip()
    if not prompt:
        return JSONResponse({"error": "prompt is required"}, status_code=400)

    rows = get_rows(run_id)
    if not rows:
        return JSONResponse({"error": "Run not found"}, status_code=404)

    result = build_test(prompt, rows[:5])
    if "error" in result:
        return JSONResponse(result, status_code=422)
    return result


@app.post("/api/runs/{run_id}/tests/run")
async def run_tests(run_id: str, request: Request, sess=Depends(require_auth)):
    body = await request.json()
    tests = body.get("tests", [])

    rows = get_rows(run_id)
    if not rows:
        return JSONResponse({"error": "Run not found"}, status_code=404)

    results = []
    for test in tests:
        result = execute_test(test, rows)
        results.append(result)

    scored = compute_risk_scores(rows, results)
    risk_summary = {
        "total_hit_rows": len(scored),
        "critical": sum(1 for r in scored if r.band_css == "critical"),
        "high": sum(1 for r in scored if r.band_css == "high"),
        "medium": sum(1 for r in scored if r.band_css == "medium"),
        "low": sum(1 for r in scored if r.band_css == "low"),
        "top_risks": [
            {
                "row_idx": r.row_idx,
                "risk_score": r.risk_score,
                "band_label": r.band_label,
                "band_css": r.band_css,
                "flagged_by": r.flagged_by,
                "overlap_count": r.overlap_count,
                "amount": r.amount,
            }
            for r in scored[:100]
        ],
    }

    save_test_session(run_id, tests, results, user_id=sess["user_id"])
    return {"results": results, "risk_summary": risk_summary}


@app.get("/api/runs/{run_id}/review")
def review_data(run_id: str, page: int = 1, per_page: int = 50, sess=Depends(require_auth),
                band: str = "", test_id: str = "", vendor: str = "", status: str = "",
                min_amount: float = 0, max_amount: float = 0,
                overlap_only: bool = False,
                row_idx: int = -1):
    sess = get_last_test_session(run_id)
    if not sess:
        return JSONResponse({"error": "No test session found"}, status_code=404)

    rows = get_rows(run_id)
    if not rows:
        return JSONResponse({"error": "Run not found"}, status_code=404)

    scored = compute_risk_scores(rows, sess["results"])
    decisions = get_all_decisions(run_id)

    # Build full hit row list
    results_by_id = {r["test_id"]: r for r in sess["results"]}
    hit_rows = []
    for rs in scored:
        row = rows[rs.row_idx]
        dec = decisions.get(rs.row_idx, {"status": "pending", "note": ""})
        hit_rows.append({
            "row_idx": rs.row_idx,
            "row_key": str(rs.row_idx),
            "risk_score": rs.risk_score,
            "band_label": rs.band_label,
            "band_css": rs.band_css,
            "overlap_count": rs.overlap_count,
            "amount": rs.amount,
            "flagged_by": rs.flagged_by,
            "review_status": dec["status"],
            "review_note": dec["note"],
            # invoice fields
            "vendor_number": row.get("vendor_number"),
            "vendor_name": row.get("vendor_name"),
            "physical_invoice_no": row.get("physical_invoice_no"),
            "system_invoice_no": row.get("system_invoice_no"),
            "date_authorized": row.get("date_authorized"),
            "date_documented": row.get("date_documented"),
            "date_captured": row.get("date_captured"),
            "scheduled_pay_date": row.get("scheduled_pay_date"),
            "amount_excl_lc": row.get("amount_excl_lc"),
            "amount_incl_lc": row.get("amount_incl_lc"),
            "reference_currency": row.get("reference_currency"),
            "invoice_description": row.get("invoice_description"),
            "company_code": row.get("company_code"),
            "payment_type": row.get("payment_type"),
            "fiscal_year": row.get("fiscal_year"),
            "captured_by": row.get("captured_by"),
            "authorized_by": row.get("authorized_by"),
            "evidence": _row_evidence(rs.flagged_by, row),
        })

    # Filters
    if row_idx >= 0:
        hit_rows = [r for r in hit_rows if r["row_idx"] == row_idx]
    if band:
        hit_rows = [r for r in hit_rows if r["band_css"] == band]
    if test_id:
        hit_rows = [r for r in hit_rows if any(f["test_id"] == test_id for f in r["flagged_by"])]
    if vendor:
        v = vendor.lower()
        hit_rows = [r for r in hit_rows if v in (r["vendor_name"] or "").lower() or v in (r["vendor_number"] or "").lower()]
    if min_amount:
        hit_rows = [r for r in hit_rows if r["amount"] >= min_amount]
    if max_amount:
        hit_rows = [r for r in hit_rows if r["amount"] <= max_amount]
    if overlap_only:
        hit_rows = [r for r in hit_rows if r["overlap_count"] >= 2]
    if status:
        hit_rows = [r for r in hit_rows if r["review_status"] == status]

    total_filtered = len(hit_rows)
    total_pages = max(1, (total_filtered + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    page_rows = hit_rows[(page - 1) * per_page: page * per_page]

    # Dashboard stats
    all_scored = compute_risk_scores(rows, sess["results"])
    confirmed = sum(1 for r in all_scored if decisions.get(r.row_idx, {}).get("status") == "confirmed")
    fp = sum(1 for r in all_scored if decisions.get(r.row_idx, {}).get("status") == "false_positive")
    needs_info = sum(1 for r in all_scored if decisions.get(r.row_idx, {}).get("status") == "needs_info")
    reviewed = confirmed + fp + needs_info
    total_hit = len(all_scored)
    unreviewed_amt = sum(r.amount for r in all_scored if decisions.get(r.row_idx, {}).get("status", "pending") == "pending")

    # Unique test ids for filter panel
    test_ids = list({f["test_id"] for r in all_scored for f in r.flagged_by})

    return {
        "rows": page_rows,
        "page": page,
        "total_pages": total_pages,
        "total_filtered": total_filtered,
        "dashboard": {
            "total_hit_rows": total_hit,
            "critical": sum(1 for r in all_scored if r.band_css == "critical"),
            "high": sum(1 for r in all_scored if r.band_css == "high"),
            "medium": sum(1 for r in all_scored if r.band_css == "medium"),
            "low": sum(1 for r in all_scored if r.band_css == "low"),
            "confirmed": confirmed,
            "false_positive": fp,
            "needs_info": needs_info,
            "reviewed_pct": round(reviewed / total_hit * 100) if total_hit else 0,
            "unreviewed_amount": unreviewed_amt,
        },
        "test_ids": sorted(test_ids),
    }


@app.patch("/api/runs/{run_id}/review/{row_idx}")
async def save_review(run_id: str, row_idx: int, request: Request, sess=Depends(require_auth)):
    body = await request.json()
    save_review_decision(run_id, row_idx, body.get("status", "pending"), body.get("note", ""))
    return {"ok": True}


@app.post("/api/runs/{run_id}/review/bulk")
async def save_review_bulk(run_id: str, request: Request, sess=Depends(require_auth)):
    body = await request.json()
    row_indices = body.get("row_indices", [])
    status = body.get("status", "pending")
    note = body.get("note", "")
    if not isinstance(row_indices, list) or not row_indices:
        return JSONResponse({"error": "row_indices required"}, status_code=400)
    save_bulk_decisions(run_id, row_indices, status, note)
    return {"ok": True, "updated": len(row_indices)}


# ── Level-1 review: governed automatic clearance and human review ────────────

def _lane_item(row: dict, risk, route: dict | None) -> dict:
    """One decision card. Carries only what a reviewer needs to decide."""
    return {
        "row_idx": risk.row_idx,
        "vendor_number": row.get("vendor_number"),
        "vendor_name": row.get("vendor_name"),
        "invoice_number": row.get("physical_invoice_no") or row.get("system_invoice_no"),
        "amount": risk.amount,
        "currency": row.get("reference_currency"),
        "risk_score": risk.risk_score,
        "risk_band": risk.band_label,
        "band_css": risk.band_css,
        "overlap_count": risk.overlap_count,
        "flagged_by": [
            {"test_id": h.get("test_id"), "test_name": h.get("test_name")}
            for h in risk.flagged_by
        ],
        # Only the two strongest evidence items render on the card; the rest stay
        # one click away in the existing evidence panel.
        "evidence": _row_evidence(risk.flagged_by, row)[:2],
        "routing": route,
    }


def _auto_route_pending(run_id: str, rows: list, scored: list, decisions: dict,
                        existing: dict, user_id: int) -> dict:
    """Route new invoices and refresh outdated, unreviewed routes.

    Deterministic and free, so it runs on load instead of making the reviewer
    click a batch button. Reviewer-approved routes are never replaced.
    """
    todo = [
        risk for risk in scored
        if decisions.get(risk.row_idx, {}).get("status", "pending") == "pending"
        and (
            risk.row_idx not in existing
            or (
                existing[risk.row_idx]["reviewer_action"] == "pending"
                and not existing[risk.row_idx]["model"].startswith(ROUTER_VERSION + ":")
            )
        )
    ]
    if not todo:
        return existing
    routed = auto_route_invoices(rows, todo)
    save_level1_routes(run_id, routed, user_id=user_id)
    return get_level1_routes(run_id, user_id=user_id)


@app.get("/api/runs/{run_id}/level1")
def level1_queue(run_id: str, limit: int = 100, sess=Depends(require_auth)):
    session = get_last_test_session(run_id)
    rows = get_rows(run_id)
    if not session or not rows:
        return JSONResponse({"error": "Run or test session not found"}, status_code=404)

    limit = max(1, min(limit, 300))
    scored = compute_risk_scores(rows, session["results"])
    decisions = get_all_decisions(run_id)
    routes = get_level1_routes(run_id, user_id=sess["user_id"])
    routes = _auto_route_pending(run_id, rows, scored, decisions, routes, sess["user_id"])

    # Only the explicit low-risk rule can act unattended. The store enforces
    # pending status and writes routing + review decisions in one transaction.
    model = f"{ROUTER_VERSION}:low_risk_complete"
    candidates = [
        risk.row_idx for risk in scored
        if decisions.get(risk.row_idx, {}).get("status", "pending") == "pending"
        and routes.get(risk.row_idx, {}).get("model") == model
        and routes[risk.row_idx]["final_route"] == "auto_clear"
    ]
    if auto_approve_clear_routes(run_id, candidates, model, user_id=sess["user_id"]):
        decisions = get_all_decisions(run_id)
        routes = get_level1_routes(run_id, user_id=sess["user_id"])

    lanes = {route: [] for route in ROUTES}
    auto_approved = []
    for risk in scored:
        route = routes.get(risk.row_idx)
        if (route and route["reviewer_action"] == "auto_approved"
                and decisions.get(risk.row_idx, {}).get("status") == "false_positive"):
            auto_approved.append(_lane_item(rows[risk.row_idx], risk, route))
            continue
        if decisions.get(risk.row_idx, {}).get("status", "pending") != "pending":
            continue
        lane = (route or {}).get("final_route")
        if lane not in lanes:
            continue
        lanes[lane].append(_lane_item(rows[risk.row_idx], risk, route))

    total = len(scored)
    decided = sum(
        1 for risk in scored
        if decisions.get(risk.row_idx, {}).get("status", "pending") != "pending"
    )
    return {
        "lanes": {
            route: {
                "label": LANE_LABELS[route],
                "count": len(items),
                "amount": round(sum(i["amount"] for i in items), 2),
                "items": items[:limit],
                "truncated": len(items) > limit,
            }
            for route, items in lanes.items()
        },
        "auto_approved": {
            "count": len(auto_approved),
            "amount": round(sum(i["amount"] for i in auto_approved), 2),
            "items": auto_approved[:limit],
            "truncated": len(auto_approved) > limit,
        },
        "summary": {
            "total_flagged": total,
            "decided": decided,
            "pending": total - decided,
            "progress_pct": round(decided / total * 100) if total else 0,
            "router": ROUTER_VERSION,
        },
    }


@app.post("/api/runs/{run_id}/level1/{row_idx}/reopen")
def reopen_level1_auto_approval(run_id: str, row_idx: int, sess=Depends(require_auth)):
    if not reopen_auto_approved(run_id, row_idx, user_id=sess["user_id"]):
        return JSONResponse({"error": "Auto-approved invoice not found"}, status_code=404)
    return {"ok": True, "review_status": "pending"}


def _agent_review_queue(run_id: str, user_id: int):
    session = get_last_test_session(run_id)
    rows = get_rows(run_id)
    if not session or not rows:
        return None
    scored = compute_risk_scores(rows, session["results"])
    decisions = get_all_decisions(run_id)
    cases = get_agent_review_cases(run_id, user_id)
    pending = [
        risk for risk in scored
        if decisions.get(risk.row_idx, {}).get("status", "pending") == "pending"
        and cases.get(risk.row_idx, {}).get("schema_version") != 5
    ]
    # Scores are descending; amount breaks ties consistently.
    pending.sort(key=lambda risk: (-risk.risk_score, -risk.amount, risk.row_idx))
    return rows, scored, cases, pending


@app.get("/api/runs/{run_id}/agent-review")
def agent_review_queue(run_id: str, filter: str = "all", sess=Depends(require_auth)):
    if filter not in ("all", "needs_info", "legacy"):
        return JSONResponse({"error": "Unknown brief filter"}, status_code=400)
    data = _agent_review_queue(run_id, sess["user_id"])
    if data is None:
        return JSONResponse({"error": "Run or test session not found"}, status_code=404)
    rows, scored, cases, pending = data
    decisions = get_all_decisions(run_id)
    routes = get_level1_routes(run_id, user_id=sess["user_id"])
    case_items = [
        {"row_idx": idx, **case,
         "review_status": decisions.get(idx, {}).get("status", "pending")}
        for idx, case in sorted(cases.items(), key=lambda entry: entry[1]["updated_at"], reverse=True)
    ]
    if filter == "needs_info":
        case_items = [
            case for case in case_items
            if case["review_status"] == "needs_info"
            or case.get("recommendation") == "request_evidence"
        ]
    elif filter == "legacy":
        case_items = [
            case for case in case_items
            if case.get("schema_version") != 5 and case["review_status"] != "pending"
        ]
    return {
        "remaining": len(pending),
        "processed": len(cases),
        "active_job": get_active_agent_review_job(run_id, sess["user_id"]),
        "legacy_decisions": sum(
            1 for idx, case in cases.items()
            if case.get("schema_version") != 5
            and decisions.get(idx, {}).get("status", "pending") != "pending"
        ),
        "auto_approved": sum(
            1 for route in routes.values()
            if route["reviewer_action"] == "auto_approved"
            and decisions.get(route["row_idx"], {}).get("status") == "false_positive"
        ),
        "next": [
            {"row_idx": risk.row_idx, "score": risk.risk_score,
             "band": risk.band_label, "amount": risk.amount,
             "vendor": rows[risk.row_idx].get("vendor_name")}
            for risk in pending[:10]
        ],
        "cases": case_items[:100],
        "cases_truncated": len(case_items) > 100,
    }


def _process_agent_review_batch(job_id: str, run_id: str, user_id: int, size: int):
    completed = []
    try:
        update_agent_review_job(job_id, "running", completed)
        data = _agent_review_queue(run_id, user_id)
        if data is None:
            raise ValueError("Run or test session not found")
        rows, scored, cases, pending = data
        selected = pending[:size]
        decisions = get_all_decisions(run_id)
        routes = get_level1_routes(run_id, user_id=user_id)
        routes = _auto_route_pending(run_id, rows, selected, decisions, routes, user_id)
        policy = f"{ROUTER_VERSION}:low_risk_complete"
        clear = [
            risk.row_idx for risk in selected
            if routes.get(risk.row_idx, {}).get("model") == policy
            and routes[risk.row_idx]["final_route"] == "auto_clear"
        ]
        auto_approve_clear_routes(run_id, clear, policy, user_id=user_id)
        decisions = get_all_decisions(run_id)
        for risk in selected:
            if decisions.get(risk.row_idx, {}).get("status") == "false_positive":
                completed.append({"row_idx": risk.row_idx, "result": "auto_approved"})
            elif decisions.get(risk.row_idx, {}).get("status", "pending") == "pending":
                for attempt in range(2):
                    try:
                        result = agent_investigate(rows[risk.row_idx], risk, rows)
                        break
                    except RuntimeError:
                        raise  # Missing provider/configuration is not transient.
                    except Exception:
                        if attempt:
                            raise
                        time.sleep(1)
                save_agent_review_case(run_id, risk.row_idx, user_id, result)
                completed.append({"row_idx": risk.row_idx, "result": result["recommendation"]})
            update_agent_review_job(job_id, "running", completed)
        update_agent_review_job(job_id, "completed", completed)
    except Exception:
        # Saved cases survive retry; do not expose provider errors or credentials.
        update_agent_review_job(
            job_id, "failed", completed, "Batch stopped. Retry to process remaining invoices."
        )


@app.post("/api/runs/{run_id}/agent-review/batch")
async def run_agent_review_batch(
    run_id: str, request: Request, background_tasks: BackgroundTasks,
    sess=Depends(require_auth),
):
    body = await request.json()
    if body.get("size") not in (5, 10):
        return JSONResponse({"error": "Batch size must be 5 or 10"}, status_code=400)
    data = _agent_review_queue(run_id, sess["user_id"])
    if data is None:
        return JSONResponse({"error": "Run or test session not found"}, status_code=404)
    if not data[3]:
        return {"job": None, "completed": [], "remaining": 0}
    job_id = create_agent_review_job(run_id, sess["user_id"], body["size"])
    if not job_id:
        return JSONResponse({"error": "A batch is already running for this run"}, status_code=409)
    background_tasks.add_task(
        _process_agent_review_batch, job_id, run_id, sess["user_id"], body["size"]
    )
    return {"job": job_id}


@app.get("/api/runs/{run_id}/agent-review/jobs/{job_id}")
def agent_review_job(run_id: str, job_id: str, sess=Depends(require_auth)):
    job = get_agent_review_job(job_id, run_id, sess["user_id"])
    if not job:
        return JSONResponse({"error": "Batch not found"}, status_code=404)
    return job


@app.post("/api/runs/{run_id}/agent-review/{row_idx}/approve")
def approve_agent_case(run_id: str, row_idx: int, sess=Depends(require_auth)):
    status = approve_agent_recommendation(
        run_id, row_idx, sess["user_id"], sess.get("username") or "reviewer"
    )
    if not status:
        return JSONResponse(
            {"error": "Case not found or invoice already decided"}, status_code=409
        )
    return {"ok": True, "review_status": status}


@app.post("/api/runs/{run_id}/level1/bulk-accept")
async def level1_bulk_accept(run_id: str, request: Request, sess=Depends(require_auth)):
    """Accept a whole lane in one click, writing an audit note on every row."""
    body = await request.json()
    lane = str(body.get("lane") or "").strip()
    if lane not in ROUTES:
        return JSONResponse({"error": f"lane must be one of {', '.join(ROUTES)}"}, status_code=400)

    session = get_last_test_session(run_id)
    rows = get_rows(run_id)
    if not session or not rows:
        return JSONResponse({"error": "Run or test session not found"}, status_code=404)

    scored = compute_risk_scores(rows, session["results"])
    decisions = get_all_decisions(run_id)
    routes = get_level1_routes(run_id, user_id=sess["user_id"])

    targets = [
        risk.row_idx for risk in scored
        if decisions.get(risk.row_idx, {}).get("status", "pending") == "pending"
        and routes.get(risk.row_idx, {}).get("final_route") == lane
    ]
    if not targets:
        return {"ok": True, "updated": 0, "message": f"No pending invoices in the {LANE_LABELS[lane]} lane."}

    note = (
        f"Bulk-accepted {LANE_LABELS[lane]} lane by {sess.get('username') or 'reviewer'} "
        f"— deterministic rule {ROUTER_VERSION}; risk scores and test results unchanged."
    )
    bulk_update_level1_routes(run_id, targets, "accepted", note, user_id=sess["user_id"])
    save_bulk_decisions(run_id, targets, ROUTE_TO_REVIEW_STATUS[lane], note)
    return {"ok": True, "updated": len(targets), "status": ROUTE_TO_REVIEW_STATUS[lane]}


@app.patch("/api/runs/{run_id}/level1/{row_idx}")
async def review_level1_route(
    run_id: str,
    row_idx: int,
    request: Request,
    sess=Depends(require_auth),
):
    """Accept or override one route. Also resolves the invoice in the main review
    queue so a single click finishes it instead of leaving it pending twice."""
    body = await request.json()
    action = str(body.get("action") or "").strip()
    final_route = str(body.get("final_route") or "").strip()
    note = str(body.get("note") or "").strip()
    if action not in ("accepted", "overridden"):
        return JSONResponse({"error": "action must be accepted or overridden"}, status_code=400)
    if final_route not in ROUTES:
        return JSONResponse({"error": f"final_route must be one of {', '.join(ROUTES)}"}, status_code=400)
    if action == "overridden" and not note:
        return JSONResponse({"error": "A note is required when overriding the router"}, status_code=400)

    updated = update_level1_route(
        run_id, row_idx, final_route, action, note, user_id=sess["user_id"]
    )
    if not updated:
        return JSONResponse({"error": "Level-1 route not found"}, status_code=404)

    status = ROUTE_TO_REVIEW_STATUS[final_route]
    audit = note or f"Accepted {LANE_LABELS[final_route]} route ({ROUTER_VERSION})."
    save_review_decision(run_id, row_idx, status, audit)
    return {"ok": True, "review_status": status}


@app.get("/api/runs/{run_id}/clusters")
def clusters_data(run_id: str, group_by: str = "signature", sess=Depends(require_auth)):
    session = get_last_test_session(run_id)
    if not session:
        return JSONResponse({"error": "No test session found"}, status_code=404)
    rows = get_rows(run_id)
    if not rows:
        return JSONResponse({"error": "Run not found"}, status_code=404)

    scored = compute_risk_scores(rows, session["results"])
    decisions = get_all_decisions(run_id)
    result = build_clusters(rows, session["results"], scored, decisions, group_by=group_by)
    result["emergent_clusters"] = detect_anomalies(rows, scored, decisions)
    result["linked_entities"] = build_connected_risks(
        rows, result["rule_clusters"] + result["emergent_clusters"]
    )

    # attach any cached AI summaries for the current signature
    cached = get_ai_cache(run_id, result["signature"])
    if cached:
        summaries = cached.get("cluster_summaries", {})
        for c in result["rule_clusters"] + result["emergent_clusters"]:
            c["ai_summary"] = summaries.get(c["cluster_id"])
        result["ai"] = {k: cached.get(k) for k in ("executive_summary", "priorities", "disclaimer")}
        result["ai_cached"] = True
    else:
        result["ai_cached"] = False
    return result


@app.post("/api/runs/{run_id}/ai/triage")
def ai_triage(run_id: str, group_by: str = "signature", sess=Depends(require_auth)):
    session = get_last_test_session(run_id)
    if not session:
        return JSONResponse({"error": "No test session found"}, status_code=404)
    rows = get_rows(run_id)
    if not rows:
        return JSONResponse({"error": "Run not found"}, status_code=404)

    scored = compute_risk_scores(rows, session["results"])
    decisions = get_all_decisions(run_id)
    result = build_clusters(rows, session["results"], scored, decisions, group_by=group_by)
    emergent = detect_anomalies(rows, scored, decisions)
    signature = result["signature"]

    cached = get_ai_cache(run_id, signature)
    if cached:
        cached["cached"] = True
        return cached

    ai = generate_triage(result["rule_clusters"], emergent)
    set_ai_cache(run_id, signature, ai)
    ai["cached"] = False
    return ai


@app.post("/api/runs/{run_id}/anomaly/suggest")
def anomaly_suggest(run_id: str, sess=Depends(require_auth)):
    session = get_last_test_session(run_id)
    if not session:
        return JSONResponse({"error": "No test session found"}, status_code=404)
    rows = get_rows(run_id)
    if not rows:
        return JSONResponse({"error": "Run not found"}, status_code=404)

    scored = compute_risk_scores(rows, session["results"])
    decisions = get_all_decisions(run_id)
    emergent = detect_anomalies(rows, scored, decisions)
    if not emergent:
        return {"suggestions": [], "disclaimer": ""}
    return suggest_tests(emergent)


@app.post("/api/runs/{run_id}/clusters/investigate")
async def investigate_cluster(
    run_id: str,
    request: Request,
    group_by: str = "signature",
    sess=Depends(require_auth),
):
    body = await request.json()
    cluster_id = str(body.get("cluster_id") or "").strip()
    if not cluster_id:
        return JSONResponse({"error": "cluster_id required"}, status_code=400)

    session = get_last_test_session(run_id)
    rows = get_rows(run_id)
    if not session or not rows:
        return JSONResponse({"error": "Run or test session not found"}, status_code=404)

    scored = compute_risk_scores(rows, session["results"])
    decisions = get_all_decisions(run_id)
    result = build_clusters(rows, session["results"], scored, decisions, group_by=group_by)
    emergent = detect_anomalies(rows, scored, decisions)
    clusters = result["rule_clusters"] + emergent
    cluster = next((c for c in clusters if c["cluster_id"] == cluster_id), None)
    if not cluster:
        return JSONResponse({"error": "Cluster not found for current grouping"}, status_code=404)

    cache_key = f"investigation::{result['signature']}::{cluster_id}"
    cached = get_ai_cache(run_id, cache_key)
    if cached:
        cached["cached"] = True
        return cached

    prior_rows = [
        get_rows(r["run_id"])
        for r in list_runs(limit=6, user_id=sess["user_id"])
        if r["run_id"] != run_id
    ][:5]
    facts = build_investigation_facts(cluster, clusters, rows, decisions, prior_rows)
    brief = polish_investigation(facts) or deterministic_investigation(facts)
    brief["cached"] = False
    set_ai_cache(run_id, cache_key, brief)
    return brief


@app.post("/api/runs/{run_id}/finding-report")
def finding_report(run_id: str, sess=Depends(require_auth)):
    session = get_last_test_session(run_id)
    rows = get_rows(run_id)
    if not session or not rows:
        return JSONResponse({"error": "Run or test session not found"}, status_code=404)

    scored = compute_risk_scores(rows, session["results"])
    decisions = get_all_decisions(run_id)
    facts = build_finding_facts(rows, scored, decisions)
    if not facts:
        return JSONResponse(
            {"error": "Confirm at least one invoice before generating a finding."},
            status_code=400,
        )

    cache_key = f"finding::{finding_signature(scored, decisions)}"
    cached = get_ai_cache(run_id, cache_key)
    if cached:
        cached["cached"] = True
        return cached

    report = polish_finding(facts) or deterministic_finding(facts)
    report["plain_text"] = render_plain_text(report)
    report["cached"] = False
    set_ai_cache(run_id, cache_key, report)
    return report


# ── Grounded chat copilot ────────────────────────────────────────────────────
# The LLM only parses intent; every number/filter/evidence below comes from the
# same deterministic functions the rest of the app uses. See chat_agent.py.
@app.post("/api/runs/{run_id}/chat")
async def chat(run_id: str, request: Request, sess=Depends(require_auth)):
    body = await request.json()
    message = (body.get("message") or "").strip()
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)

    session = get_last_test_session(run_id)
    rows = get_rows(run_id)
    if not session or not rows:
        return JSONResponse({"error": "Run or test session not found"}, status_code=404)

    scored = compute_risk_scores(rows, session["results"])
    decisions = get_all_decisions(run_id)

    prior_run_summary = None
    try:
        other_runs = [r for r in list_runs(limit=10, user_id=sess["user_id"]) if r["run_id"] != run_id]
        if other_runs:
            prior = other_runs[0]
            prior_session = get_last_test_session(prior["run_id"])
            prior_rows = get_rows(prior["run_id"])
            if prior_session and prior_rows:
                prior_scored = compute_risk_scores(prior_rows, prior_session["results"])
                prior_run_summary = {
                    "run_id": prior["run_id"],
                    "filename": prior.get("filename") or prior["run_id"],
                    "total_hit_rows": len(prior_scored),
                    "total_amount": round(sum(rs.amount for rs in prior_scored), 2),
                }
    except Exception:
        prior_run_summary = None

    result = answer_chat(
        message, rows, scored, decisions, evidence_for,
        test_results=session["results"], prior_run_summary=prior_run_summary,
        agent_cases=get_agent_review_cases(run_id, sess["user_id"]),
    )
    return result


@app.post("/api/compare")
async def compare(
    file_new: UploadFile = File(...),
    run_id_old: str = Form(...),
    sess=Depends(require_auth),
):
    old_rows = get_rows(run_id_old)
    if not old_rows:
        return JSONResponse({"error": "Previous run not found"}, status_code=404)

    suffix = Path(file_new.filename or "upload.csv").suffix or ".csv"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        shutil.copyfileobj(file_new.file, tmp)
        temp_path = tmp.name

    ingest = ingest_csv(temp_path)
    result = compare_runs(old_rows, ingest.rows)
    result["new_file"] = file_new.filename
    result["old_run_id"] = run_id_old
    return result


# ── Saved test library ───────────────────────────────────────────────────────

@app.get("/api/tests/library")
def get_library(sess=Depends(require_auth)):
    return list_saved_tests(user_id=sess["user_id"])


@app.post("/api/tests/library")
async def add_to_library(request: Request, sess=Depends(require_auth)):
    body = await request.json()
    test_id = save_test_to_library(body, user_id=sess["user_id"])
    return {"id": test_id, "ok": True}


@app.delete("/api/tests/library/{test_id}")
def remove_from_library(test_id: int, sess=Depends(require_auth)):
    delete_saved_test(test_id, user_id=sess["user_id"])
    return {"ok": True}


# ── Vendor drill-down ────────────────────────────────────────────────────────

@app.get("/api/runs/{run_id}/vendor/{vendor_number}")
def vendor_detail(run_id: str, vendor_number: str, sess=Depends(require_auth)):
    rows = get_rows(run_id)
    if not rows:
        return JSONResponse({"error": "Run not found"}, status_code=404)

    vendor_rows = [
        r for r in rows
        if str(r.get("vendor_number") or "").strip().lower() == vendor_number.strip().lower()
        or str(r.get("vendor_name") or "").strip().lower() == vendor_number.strip().lower()
    ]

    sess = get_last_test_session(run_id)
    flagged_indices = set()
    if sess:
        for tr in sess["results"]:
            for idx in tr.get("all_hit_indices", []):
                flagged_indices.add(idx)

    total_amount = sum(
        float(str(r.get("amount_excl_lc") or 0).replace(",", ""))
        for r in vendor_rows
        if r.get("amount_excl_lc")
    )

    enriched = []
    for r in vendor_rows:
        idx = rows.index(r)
        enriched.append({**r, "row_idx": idx, "flagged": idx in flagged_indices})

    return {
        "vendor_number": vendor_number,
        "vendor_name": vendor_rows[0].get("vendor_name") if vendor_rows else vendor_number,
        "invoice_count": len(vendor_rows),
        "total_amount": round(total_amount, 2),
        "flagged_count": sum(1 for r in enriched if r["flagged"]),
        "invoices": enriched[:200],
    }


# ── Export confirmed findings ────────────────────────────────────────────────

@app.get("/api/runs/{run_id}/export")
def export_findings(run_id: str, status: str = "confirmed", sess=Depends(require_auth)):
    sess = get_last_test_session(run_id)
    if not sess:
        return JSONResponse({"error": "No test session"}, status_code=404)

    rows = get_rows(run_id)
    scored = compute_risk_scores(rows, sess["results"])
    decisions = get_all_decisions(run_id)

    export_rows = []
    for rs in scored:
        dec = decisions.get(rs.row_idx, {"status": "pending", "note": ""})
        if status != "all" and dec["status"] != status:
            continue
        row = rows[rs.row_idx]
        export_rows.append({
            "Risk Score":        rs.risk_score,
            "Risk Band":         rs.band_label,
            "Review Status":     dec["status"],
            "Reviewer Note":     dec["note"],
            "Flagged By":        " | ".join(f["test_name"] for f in rs.flagged_by),
            "Tests Count":       rs.overlap_count,
            "Vendor Number":     row.get("vendor_number",""),
            "Vendor Name":       row.get("vendor_name",""),
            "Invoice No":        row.get("physical_invoice_no") or row.get("system_invoice_no",""),
            "Date Authorized":   row.get("date_authorized",""),
            "Date Captured":     row.get("date_captured",""),
            "Scheduled Pay":     row.get("scheduled_pay_date",""),
            "Amount (Excl.)":    row.get("amount_excl_lc",""),
            "Amount (Incl.)":    row.get("amount_incl_lc",""),
            "Currency":          row.get("reference_currency",""),
            "Description":       row.get("invoice_description",""),
            "Company Code":      row.get("company_code",""),
            "Captured By":       row.get("captured_by",""),
            "Authorized By":     row.get("authorized_by",""),
            "Payment Type":      row.get("payment_type",""),
            "Fiscal Year":       row.get("fiscal_year",""),
        })

    output = io.StringIO()
    if export_rows:
        writer = csv.DictWriter(output, fieldnames=list(export_rows[0].keys()))
        writer.writeheader()
        writer.writerows(export_rows)
    else:
        output.write("No findings match the selected status\n")

    output.seek(0)
    fname = f"dia_findings_{run_id}_{status}.csv"
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode("utf-8-sig")),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/results")
def results_page():
    return FileResponse(static_dir / "results.html")


@app.get("/api/runs/{run_id}/tests/last")
def last_test_session(run_id: str, sess=Depends(require_auth)):
    sess = get_last_test_session(run_id)
    if not sess:
        return JSONResponse({"error": "No test sessions found"}, status_code=404)
    rows = get_rows(run_id)
    scored = compute_risk_scores(rows, sess["results"])
    sess["risk_summary"] = {
        "total_hit_rows": len(scored),
        "critical": sum(1 for r in scored if r.band_css == "critical"),
        "high": sum(1 for r in scored if r.band_css == "high"),
        "medium": sum(1 for r in scored if r.band_css == "medium"),
        "low": sum(1 for r in scored if r.band_css == "low"),
    }
    return sess
