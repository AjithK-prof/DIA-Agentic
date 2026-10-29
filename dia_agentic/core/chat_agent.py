"""
Grounded audit copilot for the results page.

Design contract (do not weaken):
  - Stage 1  LLM (llama-3.1-8b): converts natural language → structured intent JSON.
             Fast, cheap, deterministic whitelist validation follows.
  - Stage 2  Python (deterministic): executes the intent against pre-computed risk
             scores, test results, and decisions. No LLM touches numbers.
  - Stage 3  LLM synthesis (llama-3.3-70b, optional): generates a human-readable
             narrative for explain_vendor / explain_test responses ONLY.
             The model never sees raw data it could fabricate — it only receives
             structured summaries built by the deterministic stage.

  Three-model routing:
    llama-3.1-8b   → intent parsing      (fast, ~0.5s, cheap)
    llama-3.3-70b  → rich synthesis      (smart, ~0.7s, used selectively)
    qwen-32b-coder → test code generation (separate path in test_builder.py)
"""
import json
import math
import os
import re

from dia_agentic.core.llm_provider import (
    get_client as _get_llm_client,
    get_synthesis_client as _get_synth_client,
)
from dia_agentic.core.cluster_engine import build_clusters
from dia_agentic.core.anomaly_detect import detect_anomalies
from dia_agentic.core.ai_review import generate_triage
from dia_agentic.core.date_utils import actual_payment_date, parse_invoice_date

# ── Whitelisted query surface ─────────────────────────────────────────────────
ALLOWED_BANDS      = {"critical", "high", "medium", "low"}
ALLOWED_STATUSES   = {"pending", "confirmed", "false_positive", "needs_info"}
ALLOWED_GROUP_BY   = {"vendor", "test", "band", "status", None}
ALLOWED_METRICS    = {"count", "amount", "avg_risk", "max_risk", "min_risk"}
ALLOWED_SORT       = {"asc", "desc"}
ALLOWED_ROW_SORTS  = {"risk_desc", "risk_asc", "amount_desc", "amount_asc"}
ALLOWED_INTENTS    = {
    "filter", "aggregate", "explain_row", "summarize", "compare",
    "clarify", "unsupported",
    # ── new intents ───────────────────────────────────────────────────────────
    "list_tests",     # "what are our tests / what rules are running"
    "explain_test",   # "what is the Round Dollar test / explain test X"
    "count_clusters", # "how many clusters / patterns are there"
    "explain_vendor", # "tell me about vendor X / what is going on with LCA"
    "run_stats", "review_overview", "review_case", "explain_field", "explain_risk", "invoice_field",
}
ALLOWED_STAT_FIELDS = {"all_invoices", "actual_payment_date", "date_documented", "date_captured"}
ALLOWED_STAT_CONDITIONS = {"total", "missing", "present"}
FIELD_HELP = {
    "actual_payment_date": "Executed payment date: a valid payment_date, falling back to actual_pay_date. Scheduled payment and 1900 placeholders do not count.",
    "payment_date": "Executed payment date in the primary ERP field. Blank, invalid, and 1900 placeholder values are not real dates.",
    "actual_pay_date": "Alternative ERP field for the executed payment date. Used when payment_date is not valid.",
    "scheduled_pay_date": "Planned payment date; it does not prove that payment occurred.",
    "date_documented": "Invoice documentation date used by payment-before-invoice tests.",
    "date_captured": "Date the invoice was entered or captured.",
    "risk_score": "A deterministic review-priority score for flagged invoices, not a fraud probability. It combines average test weight, overlap, and invoice amount relative to the largest flagged amount.",
    "review_status": "The recorded reviewer outcome: pending, confirmed, false_positive, or needs_info. An agent recommendation is separate from this decision.",
}
ALLOWED_PROJECTIONS = {
    "vendor_number", "vendor_name", "amount_excl_lc", "risk_score",
    "risk_band", "physical_invoice_no", "flagged_by_names", "review_status",
}

MAX_ROWS_RETURNED = 50
DEFAULT_TOP_N     = 5
MAX_TOP_N         = 20

# ── Stage-1 system prompt (intent parsing only) ───────────────────────────────
SYSTEM_PROMPT = """You are an intent parser for an invoice-audit results page.
Your ONLY job is to convert the user's message into a JSON object.
You do NOT answer the question, compute anything, or add information not in the message.

Return ONLY a JSON object — no markdown fences, no explanation, no extra keys.

Schema:
{
  "intent": "filter"|"aggregate"|"explain_row"|"summarize"|"compare"|"clarify"|"unsupported"|"list_tests"|"explain_test"|"count_clusters"|"explain_vendor"|"run_stats"|"review_overview"|"review_case"|"explain_field"|"explain_risk"|"invoice_field",
  "filters": {
    "risk_band": "critical"|"high"|"medium"|"low"|null,
    "min_amount": number|null,
    "max_amount": number|null,
    "vendor_number": string|null,
    "vendor_name": string|null,
    "test_id": string|null,
    "review_status": "pending"|"confirmed"|"false_positive"|"needs_info"|null
  },
  "group_by": "vendor"|"test"|"band"|"status"|null,
  "metric": "count"|"amount"|"avg_risk"|"max_risk"|"min_risk"|null,
  "top_n": integer|null,
  "sort": "asc"|"desc"|null,
  "row_sort": "risk_desc"|"risk_asc"|"amount_desc"|"amount_asc"|null,
  "row_limit": integer|null,
  "row_ref": string|null,
  "projection": ["vendor_number"|"vendor_name"|"amount_excl_lc"|"risk_score"|"risk_band"|"physical_invoice_no"|"flagged_by_names"|"review_status"]|null,
  "clarify_question": string|null,
  "new_test_prompt": string|null,
  "stat_field": "all_invoices"|"actual_payment_date"|"date_documented"|"date_captured"|null,
  "stat_condition": "total"|"missing"|"present"|null,
  "field_name": "actual_payment_date"|"payment_date"|"actual_pay_date"|"scheduled_pay_date"|"date_documented"|"date_captured"|"risk_score"|"review_status"|null
}

## CRITICAL RULE — NEVER ADD PHANTOM FILTERS
Set a filter field ONLY when the user EXPLICITLY states that value.
If the user does not mention a band, amount, vendor, test, or status — set ALL filter fields to null.

WRONG: "top vendors by risk" → risk_band:"critical"
RIGHT: "top vendors by risk" → all filters null

WRONG: "top vendors by highest risk" → risk_band:"high"
RIGHT: "top vendors by highest risk" → all filters null, metric:"max_risk"
NOTE: "highest risk" and "highest risk score" refer to the METRIC (max_risk), NOT a band filter.
      risk_band filter should ONLY be set when the user says "high-risk invoices", "only critical", "show me the high band", etc.

WRONG: "most flagged invoices" → review_status:"false_positive"
RIGHT: "most flagged invoices" → all filters null
NOTE: ALL rows shown are "flagged" (detected by audit tests). "Flagged" ≠ false_positive.
      Only add review_status when user says "confirmed", "dismissed", "false positive", "pending review", "needs info".

WRONG: "top 5 by amount" → min_amount: 50000
RIGHT: "top 5 by amount" → min_amount: null

## Projection rule
Set projection ONLY when the user asks for one specific field.
"what is the vendor number of high risk vendors" → projection: ["vendor_number"]
"show me the amounts" → projection: ["amount_excl_lc"]
"what tests flagged these invoices" → projection: ["flagged_by_names"]
"show invoices" → projection: null

## Intent definitions

1. "filter" — user wants to see rows matching criteria or ranked individual rows.
   Set ONLY the filters explicitly mentioned. Use row_sort and row_limit when user asks for top/worst N individual invoices.
   "show critical invoices" → risk_band:"critical"
   "invoices from Acme" → vendor_name:"Acme"
   "top 10 riskiest invoices" → no filters, row_sort:"risk_desc", row_limit:10
   "top 5 highest amount invoices" → no filters, row_sort:"amount_desc", row_limit:5
   "what is the highest risk score" → no filters, row_sort:"risk_desc", row_limit:1
   "what is the highest invoice amount" → no filters, row_sort:"amount_desc", row_limit:1
   "show me the riskiest rows" → no filters, row_sort:"risk_desc", row_limit:10
   "lowest risk flagged invoices" → no filters, row_sort:"risk_asc", row_limit:10
   NOTE: Use "filter" (not "aggregate") when user asks for individual rows/invoices, not grouped totals.
   NOTE: Use "aggregate" when user asks about vendors/tests as groups (top vendors, which test has most hits).

2. "aggregate" — user wants a ranked/grouped answer (top N groups, most, highest average per group).
   "top 5 vendors by risk" → group_by:"vendor", metric:"max_risk", top_n:5, sort:"desc", ALL filters null
   "which vendor has the most invoices" → group_by:"vendor", metric:"count", ALL filters null
   "top vendors by amount among critical" → group_by:"vendor", metric:"amount", risk_band:"critical"
   "top 3 by highest risk score" → group_by:"vendor" (default when entity not specified), metric:"max_risk", top_n:3
   "top risk vendor" / "riskiest vendor" → group_by:"vendor", metric:"max_risk", top_n:1
   DEFAULT: when no group_by entity is mentioned in a top-N group query, use group_by:"vendor".

3. "explain_row" — user asks about one SPECIFIC invoice by its actual ID/number (e.g. INV-123, AMC-B/03, 200000476).
   "what is wrong with invoice INV-123" → row_ref:"INV-123"
   IMPORTANT: "explain about top risk vendor", "explain the riskiest vendor", "explain about high risk invoices"
   do NOT belong here — these are aggregate or explain_vendor, because no specific invoice ID was given.
   Only use explain_row when the user provides an actual alphanumeric invoice reference.

4. "summarize" — user wants an overall run summary.
   "summarize", "overview", "what should I look at"

5. "compare" — user wants this run vs a previous run.
   "how does this compare to last run"

6. "clarify" — request is genuinely ambiguous (missing a specific value).
   "show large invoices" → clarify: "What amount threshold defines 'large'?"
   DO NOT clarify for tests, clusters, or vendor questions — use the specific intent below.

7. "unsupported" — user wants a NEW detection rule that requires scanning ALL rows for a NEW pattern
   (field comparisons, text patterns, near-duplicates). Put description in new_test_prompt.
   "find invoices where captured_by equals authorized_by" → unsupported
   NOTE: If the user describes something that SOUNDS like an existing test name (round dollar, same day,
   suspicious keyword), use "explain_test" instead of "unsupported".

8. "list_tests" — user asks what tests/rules are active or running.
   "what are our current tests", "show all tests", "what rules detect fraud", "what patterns do we have"

9. "explain_test" — user asks about a specific named test (its definition, what it detects, why it matters).
   Put the test name in row_ref.
   "what is the Round Dollar test", "explain Same Day Capture", "how does keyword test work"
   "round dollar invoices ?" → explain_test, row_ref:"Round Dollar"
   "why are invoices flagged for same day" → explain_test, row_ref:"Same Day"

   WRONG — no specific test name, use aggregate(group_by="test") instead:
   "what is the riskiest test" → aggregate, group_by:"test", metric:"max_risk", top_n:1
   "which test has the most hits" → aggregate, group_by:"test", metric:"count", top_n:1
   "highest risk test" / "critical test" / "top test" → aggregate, group_by:"test", metric:"max_risk", top_n:5
   "what test detected the most invoices" → aggregate, group_by:"test", metric:"count", top_n:1
   Only use explain_test when a SPECIFIC test name is present in the question.

10. "count_clusters" — user asks how many clusters/patterns/anomaly groups exist.
    "how many clusters are there", "how many patterns", "how many anomaly groups"

11. "explain_vendor" — user wants a profile/narrative about ONE SPECIFIC named vendor.
    REQUIRED: a real vendor name or vendor number must appear in the message.
    "tell me about LCA Instruments" → explain_vendor, row_ref:"LCA Instruments"  ← specific name given
    "what is going on with KENNY CARLTON" → explain_vendor, row_ref:"KENNY CARLTON"
    "why is vendor 12345 suspicious" → explain_vendor, row_ref:"12345"

    WRONG — no specific vendor named, use aggregate instead:
    "explain about top risk vendor" → aggregate, group_by:"vendor", metric:"max_risk", top_n:1
    "explain the riskiest vendors" → aggregate, group_by:"vendor", metric:"max_risk", top_n:5
    "explain the top 3 vendors by risk" → aggregate, group_by:"vendor", metric:"max_risk", top_n:3

    WRONG — user wants a field, use filter+projection instead:
    "vendor number of KENNY CARLTON" → filter, vendor_name:"KENNY CARLTON", projection:["vendor_number"]
    "what is the risk score of ACME" → filter, vendor_name:"ACME", projection:["risk_score"]

12. "run_stats" — count ALL uploaded invoices, including those not flagged. Set stat_field
    and stat_condition. "How many invoices in this run?" → all_invoices,total.
    "How many invoices have no actual payment date?" → actual_payment_date,missing.
    "How many have valid documented dates?" → date_documented,present.
    For actual payment, invalid payment_date must fall back to valid actual_pay_date;
    scheduled_pay_date never counts as actual payment.
    For counts of FLAGGED invoices or review statuses, use filter or review_overview instead.

13. "review_overview" — counts of recorded decisions and pending flagged invoices.
    "How many are pending review?" / "How many were confirmed or need info?"

14. "review_case" — status, Agent Review recommendation, missing evidence, or next check
    for ONE invoice with an explicit invoice ID. Put its ID in row_ref.
    "Why is invoice INV-123 pending?" / "What did Agent Review recommend for INV-123?"
    Do not use for a vendor, or a question without an invoice ID.

15. "explain_field" — meaning of a named source field. Put the EXACT name in field_name.
    "What does actual_pay_date mean?" / "Is scheduled_pay_date actual payment?"

16. "explain_risk" — how the risk score is calculated; optional invoice ID in row_ref.
    "How is risk score calculated?" / "Why is invoice INV-123 scored 56?"

17. "invoice_field" — value of a named field for ONE invoice. Require explicit
    invoice ID in row_ref AND exact allowed field in field_name.
    "What is actual_pay_date for invoice INV-123?" / "What is the scheduled_pay_date for invoice INV-123?"
    Do not confuse a source field value with a definition of the field.

## Other rules
- group_by must be vendor/test/band/status. For any other grouping, use "unsupported".
- Never invent numbers for vague words ("large", "small", "recent") — use "clarify".
- review_status: "pending"=not reviewed, "confirmed"=confirmed finding, "false_positive"=dismissed, "needs_info"=more info needed.
- Output JSON only. No markdown, no text before or after.
"""


# ── LLM helpers ───────────────────────────────────────────────────────────────

def _call_llm(client, model: str, system: str, user: str, max_tokens: int = 600, temp: float = 0.0) -> str:
    """Single LLM call; returns raw text content. Raises on hard failure."""
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
        temperature=temp,
        max_tokens=max_tokens,
    )
    raw = (resp.choices[0].message.content or "").strip()
    return re.sub(r"<think>[\s\S]*?</think>", "", raw).strip()


def _parse_json_from_text(text: str) -> dict:
    """Extract and parse a JSON object from LLM output text."""
    if "```" in text:
        for part in text.split("```"):
            part = part.strip().lstrip("json").strip()
            if part.startswith("{"):
                text = part
                break
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        text = m.group(0)
    return json.loads(text)


def _parse_intent_llm(message: str) -> dict:
    """Stage 1: call llama-3.1-8b to turn the message into a structured intent."""
    try:
        client, model = _get_llm_client()
    except RuntimeError as e:
        return {"error": str(e)}

    last_error = ""
    for attempt in range(2):
        try:
            raw = _call_llm(client, model, SYSTEM_PROMPT, message)
            result = _parse_json_from_text(raw)
            if not isinstance(result, dict):
                raise ValueError("not a JSON object")
            return result
        except json.JSONDecodeError:
            if attempt == 0:
                continue
            last_error = "model did not return valid JSON"
            break
        except Exception as e:
            last_error = str(e)
            break

    if "401" in last_error or "invalid_api_key" in last_error.lower():
        return {"error": "API key invalid or expired. Update CLOUDFLARE_AUTH_TOKEN in .env."}
    if last_error.startswith("<") or "<!doctype" in last_error.lower():
        return {"error": "A network proxy intercepted the API request. Ask IT to whitelist api.cloudflare.com."}
    if "RemoteDisconnected" in last_error:
        return {"error": "Connection to AI API was closed by a network proxy."}
    return {"error": f"Could not parse question: {last_error or 'unknown error'}"}


def _local_intent(message: str) -> dict | None:
    """Handle precise frequent questions even if the intent model is unavailable."""
    text = message.strip().lower()
    invoice = re.search(r"\binvoice\s+#?([a-z0-9][a-z0-9_./-]*)", text)
    ref = invoice.group(1) if invoice else None
    if ref in {"id", "number", "status", "risk", "date"}:
        ref = None
    fields = [key for key in FIELD_HELP if re.search(r"\b" + re.escape(key) + r"\b", text)]
    if ref and len(fields) == 1 and re.search(r"\b(what|show|value|is)\b", text) \
            and not re.search(r"\b(why|mean|explain|calculat\w*)\b", text):
        return {"intent": "invoice_field", "row_ref": ref, "field_name": fields[0]}
    if ref and re.search(r"\b(review|recommend|pending|decision|evidence|status)\b", text):
        return {"intent": "review_case", "row_ref": ref}
    if re.search(r"\b(risk score|risk scoring)\b", text) and re.search(
        r"\b(calculat\w*|comput\w*|work\w*|determined|why)\b", text
    ):
        return {"intent": "explain_risk", "row_ref": ref}
    if re.search(r"\b(how many|number of|count of)\b", text):
        if re.search(r"\b(for vendor|from vendor|vendor |only |among |over |under |above |below |in 20\d\d)\b", text):
            return None
        if re.search(r"\b(pending|confirmed|dismissed|false positive|needs info)\b", text) \
                and re.search(r"\b(review|invoice|finding|case|decision)\b", text):
            return {"intent": "review_overview"}
        # Do not turn a request for a new date comparison into a date-quality count.
        if re.search(r"\b(invoice|invoices|rows)\b", text) and not re.search(
            r"\b(before|after|earlier|later|same day)\b", text
        ):
            condition = ("missing" if re.search(r"\b(missing|no|without|blank|invalid)\b", text)
                         else "present" if re.search(r"\b(valid|present|with|have)\b", text)
                         else None)
            field = ("actual_payment_date" if re.search(r"\b(actual|executed|paid|payment)\b.{0,30}\bdate\b|\bdate\b.{0,30}\b(actual|executed|paid|payment)\b", text)
                     else "date_documented" if re.search(r"\b(documented|invoice date|issued date)\b", text)
                     else "date_captured" if re.search(r"\b(captured|capture date)\b", text)
                     else None)
            if field and condition:
                return {"intent": "run_stats", "stat_field": field, "stat_condition": condition}
            if not field and re.search(r"\b(in this run|in the run|total invoices|uploaded invoices)\b", text):
                return {"intent": "run_stats", "stat_field": "all_invoices", "stat_condition": "total"}
    if re.search(r"\b(what|explain|mean)\b", text) and "difference between" not in text \
            and not re.search(r"\b(invoice|blank|missing|value|vendor)\b", text):
        if len(fields) == 1:
            return {"intent": "explain_field", "field_name": fields[0]}
    return None


def _synthesize(system_prompt: str, user_content: str, max_tokens: int = 250) -> str:
    """Stage 3: call llama-3.3-70b for a rich narrative. Returns '' on failure."""
    try:
        client, model = _get_synth_client()
        return _call_llm(client, model, system_prompt, user_content, max_tokens=max_tokens, temp=0.2)
    except Exception:
        return ""


# ── Intent validation ─────────────────────────────────────────────────────────

def _validate_intent(raw: dict) -> dict:
    """Clamp every field to the whitelist. Unknown/invalid values become null."""
    intent = raw.get("intent")
    if intent not in ALLOWED_INTENTS:
        intent = "clarify"

    f = raw.get("filters") or {}
    if not isinstance(f, dict):
        f = {}
    band   = f.get("risk_band");    band   = band   if band   in ALLOWED_BANDS    else None
    status = f.get("review_status"); status = status if status in ALLOWED_STATUSES else None

    def _num(v):
        try:   return float(v) if v is not None else None
        except: return None

    filters = {
        "risk_band":      band,
        "min_amount":     _num(f.get("min_amount")),
        "max_amount":     _num(f.get("max_amount")),
        "vendor_number":  (str(f.get("vendor_number")).strip() or None) if f.get("vendor_number") else None,
        "vendor_name":    (str(f.get("vendor_name")).strip() or None)   if f.get("vendor_name")   else None,
        "test_id":        (str(f.get("test_id")).strip() or None)       if f.get("test_id")       else None,
        "review_status":  status,
    }

    group_by = raw.get("group_by");  group_by = group_by if group_by in ALLOWED_GROUP_BY else None
    metric   = raw.get("metric");    metric   = metric   if metric   in ALLOWED_METRICS  else None
    sort     = raw.get("sort");      sort     = sort     if sort     in ALLOWED_SORT     else None

    try:   top_n = int(raw.get("top_n")) if raw.get("top_n") is not None else None
    except: top_n = None
    if top_n is not None:
        top_n = max(1, min(top_n, MAX_TOP_N))

    if intent == "aggregate" and (not group_by or not metric):
        intent = "clarify"

    # Validate projection — only keep whitelisted field names
    raw_proj = raw.get("projection")
    projection = None
    if isinstance(raw_proj, list):
        projection = [p for p in raw_proj if p in ALLOWED_PROJECTIONS] or None

    row_sort = raw.get("row_sort")
    row_sort = row_sort if row_sort in ALLOWED_ROW_SORTS else None

    try:   row_limit = int(raw.get("row_limit")) if raw.get("row_limit") is not None else None
    except: row_limit = None
    if row_limit is not None:
        row_limit = max(1, min(row_limit, MAX_ROWS_RETURNED))

    return {
        "intent":           intent,
        "filters":          filters,
        "group_by":         group_by,
        "metric":           metric,
        "top_n":            top_n,
        "sort":             sort,
        "row_sort":         row_sort,
        "row_limit":        row_limit,
        "row_ref":          (str(raw.get("row_ref")).strip() or None) if raw.get("row_ref") else None,
        "projection":       projection,
        "clarify_question": raw.get("clarify_question") or None,
        "new_test_prompt":  raw.get("new_test_prompt")  or None,
        "stat_field": raw.get("stat_field") if raw.get("stat_field") in ALLOWED_STAT_FIELDS else None,
        "stat_condition": raw.get("stat_condition") if raw.get("stat_condition") in ALLOWED_STAT_CONDITIONS else None,
        "field_name": raw.get("field_name") if raw.get("field_name") in FIELD_HELP else None,
    }


# ── Deterministic data helpers ────────────────────────────────────────────────

def _build_chat_rows(rows: list[dict], scored: list, decisions: dict) -> list[dict]:
    out = []
    for rs in scored:
        row = rows[rs.row_idx]
        dec = decisions.get(rs.row_idx, {"status": "pending", "note": ""})
        out.append({
            "row_idx":            rs.row_idx,
            "vendor_number":      row.get("vendor_number"),
            "vendor_name":        row.get("vendor_name"),
            "physical_invoice_no": row.get("physical_invoice_no") or row.get("system_invoice_no"),
            "amount_excl_lc":     rs.amount,
            "risk_score":         rs.risk_score,
            "risk_band":          rs.band_css,
            "flagged_by":         [t["test_id"]   for t in rs.flagged_by],
            "flagged_by_names":   [t["test_name"] for t in rs.flagged_by],
            "review_status":      dec["status"],
        })
    return out


def _apply_filters(chat_rows: list[dict], filters: dict) -> list[dict]:
    out = chat_rows
    if filters.get("risk_band"):
        out = [r for r in out if r["risk_band"] == filters["risk_band"]]
    if filters.get("min_amount") is not None:
        out = [r for r in out if r["amount_excl_lc"] >= filters["min_amount"]]
    if filters.get("max_amount") is not None:
        out = [r for r in out if r["amount_excl_lc"] <= filters["max_amount"]]
    if filters.get("vendor_number"):
        v = filters["vendor_number"].lower()
        out = [r for r in out if v in str(r.get("vendor_number") or "").lower()]
    if filters.get("vendor_name"):
        v = filters["vendor_name"].lower()
        out = [r for r in out if v in str(r.get("vendor_name") or "").lower()]
    if filters.get("test_id"):
        t = filters["test_id"].lower()
        out = [r for r in out if any(t in fid.lower() for fid in r["flagged_by"])]
    if filters.get("review_status"):
        out = [r for r in out if r["review_status"] == filters["review_status"]]
    return out


def _group_key(row: dict, group_by: str) -> str:
    if group_by == "vendor": return row.get("vendor_name") or row.get("vendor_number") or "Unknown"
    if group_by == "test":   return ", ".join(row["flagged_by_names"]) or "Unknown"
    if group_by == "band":   return row["risk_band"]
    if group_by == "status": return row["review_status"]
    return "all"


def _group_by_key(rows: list[dict], group_by: str) -> dict:
    from collections import defaultdict
    groups = defaultdict(list)
    for r in rows:
        groups[_group_key(r, group_by)].append(r)
    return groups


def _group_rows(matched: list[dict], group_by: str | None) -> list[dict]:
    if not group_by:
        return []
    groups = _group_by_key(matched, group_by)
    out = [{"key": k, "row_count": len(v), "amount": round(sum(x["amount_excl_lc"] for x in v), 2)}
           for k, v in groups.items()]
    out.sort(key=lambda g: g["amount"], reverse=True)
    return out


def _aggregate_rows(matched: list[dict], group_by: str, metric: str, top_n, sort) -> list[dict]:
    groups = _group_by_key(matched, group_by)
    entries = []
    for k, v in groups.items():
        count  = len(v)
        amount = round(sum(x["amount_excl_lc"] for x in v), 2)
        scores = [x["risk_score"] for x in v]
        entries.append({
            "key":       k,
            "row_count": count,
            "amount":    amount,
            "avg_risk":  round(sum(scores) / count, 1) if count else 0,
            "max_risk":  max(scores) if scores else 0,
            "min_risk":  min(scores) if scores else 0,
        })
    metric_key = {"count": "row_count", "amount": "amount", "avg_risk": "avg_risk",
                  "max_risk": "max_risk", "min_risk": "min_risk"}[metric]
    entries.sort(key=lambda e: e[metric_key], reverse=((sort or "desc") == "desc"))
    return entries[: (top_n or DEFAULT_TOP_N)]


def _fuzzy_find_test(test_results: list[dict], ref: str) -> dict | None:
    """Find a test by partial name match. Returns the test dict or None."""
    if not ref or not test_results:
        return None
    ref_l = ref.strip().lower()
    # Exact name match
    for t in test_results:
        if (t.get("test_name") or "").lower() == ref_l:
            return t
    # Substring match
    for t in test_results:
        name = (t.get("test_name") or "").lower()
        if ref_l in name or name in ref_l:
            return t
    # Word-level match (e.g. "Round Dollar" → "Round Dollar Invoices")
    ref_words = set(ref_l.split())
    best, best_score = None, 0
    for t in test_results:
        name_words = set((t.get("test_name") or "").lower().split())
        score = len(ref_words & name_words)
        if score > best_score:
            best, best_score = t, score
    return best if best_score > 0 else None


def _build_test_catalog(test_results: list[dict], scored: list) -> list[dict]:
    """Build a clean test catalog with hit counts from this run's scored rows."""
    catalog = []
    for t in (test_results or []):
        if not t.get("applicable", True):
            continue
        tid = t.get("test_id") or ""
        hit_count = sum(1 for rs in scored if any(x["test_id"] == tid for x in rs.flagged_by))
        catalog.append({
            "test_id":     tid,
            "test_name":   t.get("test_name") or tid,
            "hit_count":   hit_count,
            "description": t.get("description") or t.get("explanation") or "",
            "type":        t.get("type", "preset"),
        })
    catalog.sort(key=lambda t: t["hit_count"], reverse=True)
    return catalog


def _exact_invoice(rows: list[dict], ref: str):
    """Never guess which invoice a review decision belongs to."""
    if not ref:
        return None, "Please provide an invoice number."
    keys = ("physical_invoice_no", "system_invoice_no", "original_invoice_no")
    matches = [
        idx for idx, row in enumerate(rows)
        if any(str(row.get(key) or "").strip().casefold() == ref.strip().casefold()
               for key in keys if row.get(key))
    ]
    if len(matches) != 1:
        return None, ("More than one invoice has that number; provide a unique invoice ID."
                      if matches else f"No invoice with ID '{ref}' was found in this run.")
    return matches[0], None


# ── Main entry point ──────────────────────────────────────────────────────────

def answer_chat(
    message: str,
    rows: list[dict],
    scored: list,
    decisions: dict,
    evidence_for_fn,
    test_results: list[dict] | None = None,
    prior_run_summary: dict | None = None,
    agent_cases: dict[int, dict] | None = None,
) -> dict:
    """
    Routes a natural-language question to a deterministic data handler.
    LLM is used in two bounded places only:
      1. Intent parsing (llama-3.1-8b)          — always, fast
      2. Narrative synthesis (llama-3.3-70b)    — only for explain_vendor / explain_test
    """
    raw_intent = _local_intent(message) or _parse_intent_llm(message)
    if "error" in raw_intent:
        return {"type": "error", "message": raw_intent["error"]}

    intent    = _validate_intent(raw_intent)
    chat_rows = _build_chat_rows(rows, scored, decisions)

    if intent["intent"] == "run_stats":
        if re.search(r"\b(before|after|earlier|later|same day)\b", message.lower()):
            return {"type": "clarify", "message": "Date-comparison counts require a matching audit test; these run-wide counts only check whether dates are present."}
        field = intent["stat_field"]
        condition = intent["stat_condition"]
        if not field or not condition or (field == "all_invoices" and condition != "total"):
            return {"type": "clarify", "message": "Which count do you need: all invoices, or missing/present actual payment, documented, or capture dates?"}
        if any(value is not None for value in intent["filters"].values()) or re.search(
            r"\b(for vendor|from vendor|vendor |only |among |over |under |above |below |in 20\d\d)\b",
            message.lower(),
        ):
            return {"type": "clarify", "message": "This count covers the full run. Ask without a vendor, risk, or status filter."}
        if field == "all_invoices":
            matching = list(range(len(rows)))
        else:
            get_date = (actual_payment_date if field == "actual_payment_date"
                        else lambda row: parse_invoice_date(row.get(field)))
            matching = [i for i, row in enumerate(rows)
                        if (get_date(row) is None) == (condition == "missing")]
        label = {
            "all_invoices": "invoices in this run",
            "actual_payment_date": "invoices with a valid actual payment date",
            "date_documented": "invoices with a valid documented invoice date",
            "date_captured": "invoices with a valid capture date",
        }[field]
        if condition == "missing":
            label = label.replace("with a valid", "without a valid")
        return {
            "type": "run_stats_result", "count": len(matching), "total": len(rows),
            "message": f"{len(matching):,} of {len(rows):,} {label}.",
            "sample_invoices": [
                {"row_idx": i, "invoice": rows[i].get("physical_invoice_no")
                 or rows[i].get("system_invoice_no") or f"row {i + 1}"}
                for i in matching[:5]
            ],
            "sources": ["Uploaded invoice rows", "Shared date parser" if field != "all_invoices" else "Run row count"],
        }

    if intent["intent"] == "review_overview":
        if any(value is not None for value in intent["filters"].values()) or re.search(
            r"\b(for vendor|from vendor|vendor |only |among |over |under |above |below |in 20\d\d)\b",
            message.lower(),
        ):
            return {"type": "clarify", "message": "This overview covers all flagged invoices; ask without filters."}
        counts = {status: 0 for status in ("pending", "confirmed", "false_positive", "needs_info")}
        for row in chat_rows:
            status = row["review_status"]
            if status in counts:
                counts[status] += 1
        return {
            "type": "review_overview_result", "counts": counts,
            "message": (f"Of {len(chat_rows):,} flagged invoices: {counts['pending']:,} pending, "
                        f"{counts['confirmed']:,} confirmed, {counts['false_positive']:,} dismissed, "
                        f"and {counts['needs_info']:,} need information."),
            "sources": ["Latest test results", "Recorded review decisions"],
        }

    if intent["intent"] == "review_case":
        idx, error = _exact_invoice(rows, intent["row_ref"] or "")
        if error:
            return {"type": "clarify" if not intent["row_ref"] else "not_found",
                    "message": error}
        dec = decisions.get(idx, {"status": "pending", "note": ""})
        case = (agent_cases or {}).get(idx)
        invoice = rows[idx].get("physical_invoice_no") or rows[idx].get("system_invoice_no") or intent["row_ref"]
        recommendation = case.get("recommendation") if case else None
        status = dec.get("status", "pending")
        message = f"Invoice {invoice}: recorded status is {status}."
        if dec.get("note"):
            message += f" Decision note: {dec['note']}"
        if case:
            message += f" Agent Review recommended {recommendation}: {case.get('reason', '')}"
            if case.get("suggested_check"):
                message += f" Next check: {case['suggested_check']}"
            if status != "pending":
                message += " This recommendation is separate from the recorded decision."
        else:
            message += " No Agent Review recommendation is saved for this reviewer."
        return {
            "type": "review_case_result", "message": message, "row_idx": idx,
            "invoice": invoice, "status": status, "recommendation": recommendation,
            "missing_fields": case.get("missing_fields", []) if case else [],
            "sources": ["Uploaded invoice row", "Recorded review decision"]
                       + (["Saved Agent Review case"] if case else []),
        }

    if intent["intent"] == "explain_field":
        name = intent["field_name"]
        if not name:
            return {"type": "clarify", "message": "Which field would you like explained? For example, actual_pay_date or scheduled_pay_date."}
        return {"type": "field_help_result", "message": f"{name}: {FIELD_HELP[name]}",
                "sources": ["Application field definitions"]}

    if intent["intent"] == "invoice_field":
        name = intent["field_name"]
        if not name:
            return {"type": "clarify", "message": "Name the source field you want to check."}
        idx, error = _exact_invoice(rows, intent["row_ref"] or "")
        if error:
            return {"type": "clarify" if not intent["row_ref"] else "not_found",
                    "message": error}
        row = rows[idx]
        if name == "actual_payment_date":
            value = actual_payment_date(row)
            display = str(value) if value else "not available (neither actual payment field has a valid date)"
        elif name == "risk_score":
            risk = next((r for r in scored if r.row_idx == idx), None)
            display = str(risk.risk_score) if risk else "not scored (no applicable test hit)"
        elif name == "review_status":
            display = decisions.get(idx, {}).get("status", "pending")
        else:
            value = row.get(name)
            display = str(value).strip() if value is not None and str(value).strip() else "blank"
            if name in ("payment_date", "actual_pay_date", "scheduled_pay_date",
                        "date_documented", "date_captured") and value is not None \
                    and parse_invoice_date(value) is None and display != "blank":
                display += " (invalid or placeholder date)"
        return {
            "type": "invoice_field_result",
            "message": f"Invoice {intent['row_ref']}: {name} = {display}.",
            "sources": ["Uploaded invoice row"] + (
                ["Latest test results"] if name == "risk_score" else
                ["Recorded review decisions"] if name == "review_status" else []),
        }

    if intent["intent"] == "explain_risk":
        ref = intent["row_ref"]
        base = ("Score = round(average test weight × overlap multiplier × amount factor × 100 / 150), "
                "capped at 100. Overlap is 1, 1.2, 1.35, or 1.5 for 1, 2, 3, or 4+ tests. "
                "Amount factor is 0–10 relative to the largest flagged invoice in this run. "
                "Bands: Low 0–24, Medium 25–49, High 50–74, Critical 75–100. "
                "This prioritizes review; it is not a fraud probability.")
        if not ref:
            return {"type": "risk_help_result", "message": base,
                    "sources": ["Risk scoring rules"]}
        idx, error = _exact_invoice(rows, ref)
        if error:
            return {"type": "not_found", "message": error}
        risk = next((item for item in scored if item.row_idx == idx), None)
        if not risk:
            return {"type": "risk_help_result",
                    "message": f"Invoice {ref} was not flagged by an applicable test, so it has no risk score.",
                    "sources": ["Latest test results", "Risk scoring rules"]}
        mean = sum(t["weightage"] for t in risk.flagged_by) / len(risk.flagged_by)
        overlap = {1: 1.0, 2: 1.2, 3: 1.35}.get(len(risk.flagged_by), 1.5)
        max_amount = max((item.amount for item in scored), default=0)
        amount_factor = (min(math.log10(risk.amount + 1) / math.log10(max_amount + 1), 1) * 10
                         if max_amount > 0 else 0)
        message = (f"Invoice {ref} scores {risk.risk_score} ({risk.band_label}). "
                   f"{len(risk.flagged_by)} test(s), average weight {mean:.2f}, "
                   f"overlap {overlap:g}, amount factor {amount_factor:.2f}. " + base)
        return {"type": "risk_help_result", "message": message,
                "sources": ["Latest test results", "Invoice amount", "Risk scoring rules"]}

    # ── summarize ─────────────────────────────────────────────────────────────
    if intent["intent"] == "summarize":
        if not test_results:
            return {"type": "error", "message": "No test results available to summarize."}
        result   = build_clusters(rows, test_results, scored, decisions, group_by="signature")
        emergent = detect_anomalies(rows, scored, decisions)
        triage   = generate_triage(result["rule_clusters"], emergent)
        parts    = [triage.get("executive_summary", "")]
        priorities = triage.get("priorities") or []
        if priorities:
            names = []
            for p in priorities[:3]:
                c = next((x for x in result["rule_clusters"] + emergent
                           if x["cluster_id"] == p.get("cluster_id")), None)
                names.append(c["title"] if c else p.get("cluster_id", ""))
            parts.append("Review first: " + "; ".join(n for n in names if n))
        return {"type": "summary_result", "message": " ".join(p for p in parts if p), "priorities": priorities}

    # ── compare ───────────────────────────────────────────────────────────────
    if intent["intent"] == "compare":
        if not prior_run_summary:
            return {"type": "not_found", "message": "No previous run available to compare against yet."}
        cur_total  = len(chat_rows)
        cur_amount = round(sum(r["amount_excl_lc"] for r in chat_rows), 2)
        prev_total  = prior_run_summary.get("total_hit_rows", 0)
        prev_amount = prior_run_summary.get("total_amount", 0)
        row_delta   = cur_total - prev_total
        amt_delta   = round(cur_amount - prev_amount, 2)
        direction     = "more" if row_delta > 0 else ("fewer" if row_delta < 0 else "the same number of")
        amt_direction = "more" if amt_delta > 0 else ("less" if amt_delta < 0 else "the same")
        msg = (
            f"This run flagged {cur_total:,} rows ({cur_amount:,.2f} at risk) vs "
            f"{prev_total:,} rows ({prev_amount:,.2f}) in the previous run "
            f"({prior_run_summary.get('filename', 'prior run')}) — "
            f"{abs(row_delta):,} {direction} rows, {abs(amt_delta):,.2f} {amt_direction} amount at risk."
        )
        return {"type": "compare_result", "message": msg,
                "current": {"row_count": cur_total, "amount": cur_amount}, "previous": prior_run_summary}

    # ── list_tests ────────────────────────────────────────────────────────────
    if intent["intent"] == "list_tests":
        catalog = _build_test_catalog(test_results or [], scored)
        if not catalog:
            return {"type": "list_tests_result",
                    "message": "No tests have been run on this dataset yet.",
                    "tests": []}
        active = [t for t in catalog if t["hit_count"] > 0]
        zero   = [t for t in catalog if t["hit_count"] == 0]
        msg = (f"{len(active)} active test(s) with hits this run"
               + (f", {len(zero)} test(s) with no hits." if zero else "."))
        return {"type": "list_tests_result", "message": msg, "tests": catalog}

    # ── count_clusters ────────────────────────────────────────────────────────
    if intent["intent"] == "count_clusters":
        if not test_results:
            return {"type": "error", "message": "No test results available."}
        result   = build_clusters(rows, test_results, scored, decisions, group_by="signature")
        emergent = detect_anomalies(rows, scored, decisions)
        rule_count     = len(result["rule_clusters"])
        emergent_count = len(emergent)
        multi_test     = sum(1 for c in result["rule_clusters"] if c.get("test_count", 1) > 1)
        msg = (f"{rule_count} rule-based cluster(s) ({multi_test} involving multiple tests simultaneously) "
               f"and {emergent_count} data-driven anomaly group(s) — "
               f"{rule_count + emergent_count} total patterns detected.")
        return {
            "type":            "count_clusters_result",
            "message":         msg,
            "rule_clusters":   rule_count,
            "emergent":        emergent_count,
            "multi_test":      multi_test,
        }

    # ── explain_test ──────────────────────────────────────────────────────────
    if intent["intent"] == "explain_test":
        ref   = intent.get("row_ref") or message  # fallback: search by full message
        catalog  = _build_test_catalog(test_results or [], scored)
        matched  = _fuzzy_find_test(test_results or [], ref)
        if not matched:
            # Try using message words against catalog
            matched = _fuzzy_find_test(test_results or [], message)
        if not matched:
            names = [t["test_name"] for t in catalog[:8]]
            return {
                "type":    "explain_test_result",
                "message": f"Couldn't find a test matching '{ref}'. Active tests: {', '.join(names)}.",
                "test":    None,
                "rows":    [],
            }

        tid       = matched.get("test_id", "")
        test_rows = [r for r in chat_rows if tid in r.get("flagged_by", [])]
        hit_count = len(test_rows)
        total_amt = round(sum(r["amount_excl_lc"] for r in test_rows), 2)
        description = matched.get("description") or matched.get("explanation") or ""

        # Stage-3 synthesis: if no stored description, ask llama-3.3-70b
        if not description:
            ctx = (
                f"Test name: {matched.get('test_name')}\n"
                f"Hits in this run: {hit_count} invoices, ${total_amt:,.0f} total\n"
                f"Sample vendor names: {', '.join(set(r.get('vendor_name','') for r in test_rows[:5] if r.get('vendor_name')))}\n"
            )
            description = _synthesize(
                "You are a P2P audit analyst. In 2-3 sentences explain what the named audit test detects "
                "and why it matters for fraud/error detection. Be concise and plain-English. "
                "Do not invent data — use only what is provided.",
                ctx
            ) or f"Flags invoices based on '{matched.get('test_name')}' pattern."

        msg = (
            f"**{matched.get('test_name')}** — {description} "
            f"Hit {hit_count} invoice(s) in this run (${total_amt:,.0f} at risk)."
        )
        return {
            "type":        "explain_test_result",
            "message":     msg,
            "test":        {**matched, "description": description, "hit_count": hit_count, "total_amount": total_amt},
            "rows":        test_rows[:10],
        }

    # ── explain_vendor ────────────────────────────────────────────────────────
    if intent["intent"] == "explain_vendor":
        # LLM may put vendor name in row_ref OR in filters.vendor_name — check both
        ref = (
            intent.get("row_ref")
            or (intent.get("filters") or {}).get("vendor_name")
            or ""
        )
        ref_l = ref.strip().lower()

        # If "ref" is a vague descriptor (not a real name), redirect to top-vendor aggregate.
        # NEVER redirect if ref contains any digit — that's a real vendor number or ID.
        _vague_words = {"top", "risk", "riskiest", "risky", "highest", "vendor",
                        "high", "critical", "most", "suspicious", "flagged", "worst"}
        _ref_words = set(ref_l.split())
        _has_id = bool(re.search(r"\d", ref_l))  # any digit = real identifier
        if not ref_l or (not _has_id and _ref_words and _ref_words.issubset(_vague_words)):
            entries = _aggregate_rows(chat_rows, "vendor", "max_risk", 5, "desc")
            msg = "No specific vendor name given — here are the top vendors by risk score."
            return {
                "type":           "aggregate_result",
                "message":        msg,
                "group_by":       "vendor",
                "metric":         "max_risk",
                "entries":        entries,
                "active_filters": {},
            }

        vendor_rows = [r for r in chat_rows
                       if ref_l and (
                           ref_l in str(r.get("vendor_name") or "").lower()
                           or ref_l in str(r.get("vendor_number") or "").lower()
                       )]
        if not vendor_rows and ref_l:
            # broader search: any word from ref matches
            words = [w for w in ref_l.split() if len(w) > 2]
            for r in chat_rows:
                name = str(r.get("vendor_name") or "").lower()
                if any(w in name for w in words):
                    vendor_rows.append(r)

        if not vendor_rows:
            return {"type": "not_found",
                    "message": f"No flagged rows found for vendor '{ref}' in this run."}

        vendor_name = vendor_rows[0].get("vendor_name") or vendor_rows[0].get("vendor_number") or ref
        total_amt   = round(sum(r["amount_excl_lc"] for r in vendor_rows), 2)
        max_risk    = max(r["risk_score"] for r in vendor_rows)
        top_band    = vendor_rows[0]["risk_band"]
        test_names  = list(dict.fromkeys(name for r in vendor_rows for name in r["flagged_by_names"]))

        # Collect evidence for top invoice
        top_row   = max(vendor_rows, key=lambda r: r["risk_score"])
        raw_row   = rows[top_row["row_idx"]]
        evidence  = []
        for tid in top_row["flagged_by"]:
            evidence.extend(evidence_for_fn(tid, raw_row))

        # Stage-3 synthesis with llama-3.3-70b
        ctx = (
            f"Vendor: {vendor_name}\n"
            f"Flagged invoices: {len(vendor_rows)}\n"
            f"Total at risk: ${total_amt:,.0f}\n"
            f"Highest risk score: {max_risk} ({top_band})\n"
            f"Tests triggered: {', '.join(test_names)}\n"
            f"Top invoice evidence: {json.dumps([{'field': e['field'], 'value': str(e['value']), 'note': e['note']} for e in evidence[:4]], default=str)}"
        )
        narrative = _synthesize(
            "You are a P2P audit analyst. Write a 3-sentence risk narrative for this vendor. "
            "Reference specific test names and amounts. Be direct and actionable. "
            "Do not invent facts — use only the data provided.",
            ctx
        )
        if not narrative:
            narrative = (
                f"{vendor_name} triggered {len(test_names)} test(s): "
                f"{', '.join(test_names[:3])}. "
                f"{len(vendor_rows)} invoice(s) flagged totalling ${total_amt:,.0f}."
            )

        return {
            "type":        "explain_vendor_result",
            "message":     narrative,
            "vendor_name": vendor_name,
            "row_count":   len(vendor_rows),
            "total_amount": total_amt,
            "max_risk":    max_risk,
            "risk_band":   top_band,
            "test_names":  test_names,
            "rows":        vendor_rows[:8],
            "evidence":    evidence[:6],
        }

    # ── aggregate ─────────────────────────────────────────────────────────────
    if intent["intent"] == "aggregate":
        matched = _apply_filters(chat_rows, intent["filters"])
        entries = _aggregate_rows(matched, intent["group_by"], intent["metric"], intent["top_n"], intent["sort"])
        metric_label = {
            "count": "number of flagged rows", "amount": "amount at risk",
            "avg_risk": "average risk score",  "max_risk": "highest risk score",
            "min_risk": "lowest risk score",
        }[intent["metric"]]
        if entries:
            msg = f"Top {len(entries)} {intent['group_by']}(s) by {metric_label}."
        else:
            active_filters = {k: v for k, v in (intent["filters"] or {}).items() if v is not None}
            if active_filters:
                fd = ", ".join(f"{k}={v}" for k, v in active_filters.items())
                msg = f"No rows matched the filters ({fd}). Try without the filter."
            elif not chat_rows:
                msg = "No flagged rows found — make sure tests have been executed first."
            else:
                msg = f"No {intent['group_by']} groups found."
        return {
            "type":           "aggregate_result",
            "message":        msg,
            "group_by":       intent["group_by"],
            "metric":         intent["metric"],
            "entries":        entries,
            "active_filters": {k: v for k, v in (intent["filters"] or {}).items() if v is not None},
        }

    # ── clarify ───────────────────────────────────────────────────────────────
    if intent["intent"] == "clarify":
        q = intent["clarify_question"] or "Could you clarify? (e.g. a specific amount threshold or vendor name)"
        return {"type": "clarify", "message": q}

    # ── unsupported ───────────────────────────────────────────────────────────
    if intent["intent"] == "unsupported":
        desc = intent["new_test_prompt"] or message
        return {
            "type":         "unsupported",
            "message":      (
                "That's not one of the current tests, so I can't filter for it directly. "
                "I can draft it as a new test for your approval instead."
            ),
            "draft_prompt": desc,
        }

    # ── explain_row ───────────────────────────────────────────────────────────
    if intent["intent"] == "explain_row":
        ref = intent["row_ref"] or ""
        # If ref looks like a vague description rather than an invoice ID, redirect.
        # NEVER redirect if ref contains any digit — that's a real invoice ID.
        _vague = {"top", "risk", "riskiest", "highest", "vendor", "invoice",
                  "high", "critical", "most", "suspicious", "flagged", "worst", "row"}
        _ref_words = set(ref.strip().lower().split())
        _has_id = bool(re.search(r"\d", ref))
        if not ref or (not _has_id and _ref_words and _ref_words.issubset(_vague)):
            entries = _aggregate_rows(chat_rows, "vendor", "max_risk", 5, "desc")
            return {
                "type":           "aggregate_result",
                "message":        "No specific invoice ID given — here are the top vendors by risk score.",
                "group_by":       "vendor",
                "metric":         "max_risk",
                "entries":        entries,
                "active_filters": {},
            }
        idx, error = _exact_invoice(rows, ref)
        if error:
            return {"type": "not_found", "message": error}
        match = next((r for r in chat_rows if r["row_idx"] == idx), None)
        if not match:
            return {
                "type": "row_detail", "evidence": [],
                "message": (f"Invoice {ref} is in this run, but no applicable test "
                            "flagged it in the latest test session. It has no risk score."),
                "sources": ["Uploaded invoice row", "Latest test results"],
            }
        raw_row  = rows[match["row_idx"]]
        evidence = []
        for tid in match["flagged_by"]:
            evidence.extend(evidence_for_fn(tid, raw_row))
        return {
            "type":     "row_detail",
            "row":      match,
            "evidence": evidence,
            "message":  (
                f"{match.get('vendor_name') or match.get('vendor_number') or 'This invoice'} "
                f"(#{match.get('physical_invoice_no') or match['row_idx']}) "
                f"has risk score {match['risk_score']} ({match['risk_band']}), "
                f"flagged by: {', '.join(match['flagged_by_names']) or 'none'}."
            ),
        }

    # ── filter (default) ──────────────────────────────────────────────────────
    matched      = _apply_filters(chat_rows, intent["filters"])
    total_amount = round(sum(r["amount_excl_lc"] for r in matched), 2)
    groups       = _group_rows(matched, intent["group_by"])

    # Apply row_sort (for "top N riskiest invoices" etc.)
    row_sort  = intent.get("row_sort")
    row_limit = intent.get("row_limit")
    if row_sort == "risk_desc":
        matched = sorted(matched, key=lambda r: r["risk_score"], reverse=True)
    elif row_sort == "risk_asc":
        matched = sorted(matched, key=lambda r: r["risk_score"])
    elif row_sort == "amount_desc":
        matched = sorted(matched, key=lambda r: r["amount_excl_lc"], reverse=True)
    elif row_sort == "amount_asc":
        matched = sorted(matched, key=lambda r: r["amount_excl_lc"])

    display_limit = row_limit or MAX_ROWS_RETURNED
    sample = matched[:display_limit]

    filt_desc = []
    f = intent["filters"]
    if f["risk_band"]:      filt_desc.append(f"risk band = {f['risk_band']}")
    if f["min_amount"] is not None: filt_desc.append(f"amount >= {f['min_amount']:,.0f}")
    if f["max_amount"] is not None: filt_desc.append(f"amount <= {f['max_amount']:,.0f}")
    if f["vendor_number"]:  filt_desc.append(f"vendor number contains '{f['vendor_number']}'")
    if f["vendor_name"]:    filt_desc.append(f"vendor name contains '{f['vendor_name']}'")
    if f["test_id"]:        filt_desc.append(f"test matches '{f['test_id']}'")
    if f["review_status"]:  filt_desc.append(f"review status = {f['review_status']}")

    sort_label = {
        "risk_desc": "highest risk first", "risk_asc": "lowest risk first",
        "amount_desc": "highest amount first", "amount_asc": "lowest amount first",
    }.get(row_sort or "", "")

    desc       = " and ".join(filt_desc) if filt_desc else "all flagged rows"
    sort_note  = f", sorted by {sort_label}" if sort_label else ""
    limit_note = f" (top {row_limit})" if row_limit else ""
    group_note = f", grouped by {intent['group_by']}" if intent["group_by"] else ""
    msg        = f"{len(sample) if row_limit else len(matched)} row(s){limit_note} matching {desc}{sort_note}{group_note}. Total: {total_amount:,.2f}."

    return {
        "type":         "filter_result",
        "message":      msg,
        "row_count":    len(matched),
        "total_amount": total_amount,
        "group_by":     intent["group_by"],
        "groups":       groups,
        "rows":         sample,
        "projection":   intent.get("projection"),
        "row_sort":     row_sort,
        "truncated":    not row_limit and len(matched) > MAX_ROWS_RETURNED,
    }
