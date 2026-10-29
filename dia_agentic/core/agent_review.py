"""Bounded Level-1 investigation: one optional tool call, then a recommendation.

The model never writes a review decision. Only the separately approved,
deterministic low-risk policy can auto-clear an invoice.
"""
import json
import math
from statistics import median

from .evidence import SENTINELS, evidence_for, parse_amount
from .llm_provider import get_client

SYSTEM = """You are a cautious Level-1 invoice review agent. Use only supplied facts.
Return a JSON object and nothing else. First choose exactly one next step:
{"next":"evidence"|"related"|"conclude","reason":"short reason"}
After a tool result you must conclude:
{"recommendation":"confirm"|"dismiss"|"request_evidence",
 "reason":"brief explanation citing actual values or test evidence",
 "missing_fields":["names from missing_required_fields only"]}
Do not invent evidence, documents, missing fields, or scores. Blank review
fields are supplied explicitly; do not infer that a field is missing merely
because you would like it. Scheduled payment is not actual payment. A test hit
is a reason to investigate, not proof of fraud or a reason to dismiss. Only
suggest dismissal if an independent verified counter-explanation supports it.
A recommendation is not a final decision.
If facts are ambiguous but no required field is missing, explain the uncertainty
without asserting a missing field. Vendor comparisons use only same-vendor,
same-currency invoices in this run; fewer than three peers cannot establish a
baseline. A round-number test by itself does not establish fraud.
You have at most one tool call per invoice."""

# Deliberately bounded source fields, not the calculated risk score or band.
SOURCE_FIELDS = (
    "date_documented", "date_captured", "date_authorized",
    "scheduled_pay_date", "payment_date", "actual_pay_date",
    "captured_by", "authorized_by", "invoice_description",
    "po_non_po", "payment_type", "company_code",
)


def _present(value) -> bool:
    return value is not None and str(value).strip().lower() not in (
        SENTINELS | {"01/01/1900"}
    )


def _evidence(hit: dict, row: dict) -> list[dict]:
    return [
        {"field": str(item["field"])[:60],
         "value": str(item["value"])[:150],
         "note": str(item.get("note") or "")[:100]}
        for item in evidence_for(hit.get("test_id", ""), row)
    ][:6]


def _vendor_context(row: dict, risk, rows: list[dict]) -> dict:
    """Comparable invoice amounts only: same vendor and currency, excluding self."""
    vendor_id = str(row.get("vendor_number") or "").strip().casefold()
    vendor_name = str(row.get("vendor_name") or "").strip().casefold()
    currency = str(row.get("reference_currency") or "").strip().upper()
    peers = []
    comparable = bool(currency and (vendor_id or vendor_name))
    for idx, other in enumerate(rows):
        if idx == risk.row_idx or not comparable:
            continue
        if vendor_id:
            same_vendor = str(other.get("vendor_number") or "").strip().casefold() == vendor_id
        else:
            same_vendor = bool(vendor_name) and (
                str(other.get("vendor_name") or "").strip().casefold() == vendor_name
            )
        if not same_vendor or str(other.get("reference_currency") or "").strip().upper() != currency:
            continue
        amount = parse_amount(other.get("amount_excl_lc"))
        if math.isfinite(amount) and amount > 0:
            peers.append(amount)
    amount = parse_amount(row.get("amount_excl_lc"))
    peer_median = median(peers) if len(peers) >= 3 else None
    return {
        "peer_count": len(peers),
        "same_amount_peers": sum(1 for other in peers if other == amount),
        "median_peer_amount": round(peer_median, 2) if peer_median is not None else None,
        "amount_vs_median": (
            round(amount / peer_median, 2)
            if peer_median and math.isfinite(amount) and amount > 0 else None
        ),
        "basis": "other invoices for this vendor, same currency, in this run",
        "baseline_available": peer_median is not None and math.isfinite(amount) and amount > 0,
    }


def _suggested_check(risk, context: dict) -> str:
    tests = [hit.get("test_name") or hit.get("test_id") for hit in risk.flagged_by]
    if len(tests) > 1:
        return "Check source invoices and approvals for each independent test: " + ", ".join(tests[:3])
    if any(hit.get("test_id") == "round_dollar" for hit in risk.flagged_by):
        if not context["baseline_available"]:
            return "Compare the invoice to its contract or purchase order and approval; too few vendor peers for an amount baseline."
        if context["amount_vs_median"] >= 3:
            return "Check contract, invoice and authorization for an amount at least 3× this vendor's peer median."
        return "Check contract, invoice and approval; round amounts alone do not establish an exception."
    return "Inspect the source invoice and approval evidence for the triggered test."


def _context_summary(context: dict) -> str:
    if context["baseline_available"]:
        return (
            f"{context['peer_count']} same-vendor peers in this run; median "
            f"{context['median_peer_amount']:,.2f}; invoice is "
            f"{context['amount_vs_median']}× the peer median; "
            f"{context['same_amount_peers']} peers have the exact amount."
        )
    return (
        f"Only {context['peer_count']} comparable same-vendor invoices in this run; "
        "there is no reliable vendor amount baseline."
    )


def _json_response(client, model: str, messages: list[dict], tokens: int) -> dict:
    response = client.chat.completions.create(
        model=model, messages=messages, temperature=0, max_tokens=tokens,
        timeout=20,
    )
    text = (response.choices[0].message.content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Agent returned an invalid response")
    return value


def investigate(row: dict, risk, rows: list[dict], model_client=None) -> dict:
    """Investigate one risk with at most two compact LLM calls."""
    client, model = model_client or get_client()
    vendor = str(row.get("vendor_number") or row.get("vendor_name") or "")
    hits = risk.flagged_by[:5]
    required = {
        field for hit in hits for field in (hit.get("required_fields") or [])
        if isinstance(field, str) and field
    }
    missing_required = sorted(
        field for field in required if field in row and not _present(row[field])
    )[:10]
    unavailable_required = sorted(field for field in required if field not in row)[:10]
    same_day = any(hit.get("test_id") == "same_day_payment" for hit in hits)
    actual_same_day = any(hit.get("test_id") == "same_day_actual_payment" for hit in hits)
    actual_date = next(
        (field for field in ("actual_pay_date", "payment_date")
         if field in row and _present(row[field])), None
    )
    payment_gap = None
    if same_day and not actual_date:
        payment_gap = next(
            (field for field in ("actual_pay_date", "payment_date") if field in row), None
        )
        if payment_gap and payment_gap not in missing_required:
            missing_required.append(payment_gap)
    tests = [
        {"id": hit.get("test_id"), "name": hit.get("test_name"),
         "severity": hit.get("severity"), "matched_evidence": _evidence(hit, row)}
        for hit in hits
    ]
    context = _vendor_context(row, risk, rows)
    suggested_check = _suggested_check(risk, context)
    facts = {
        "row_idx": risk.row_idx,
        "invoice": row.get("physical_invoice_no") or row.get("system_invoice_no"),
        "vendor": vendor[:90],
        "amount": risk.amount,
        "currency": row.get("reference_currency"),
        "source_fields": {
            field: str(row[field])[:150]
            for field in SOURCE_FIELDS if field in row and _present(row[field])
        },
        "missing_required_fields": missing_required,
        "unavailable_required_fields": unavailable_required,
        "review_check": (
            ("Historical test: capture equals SCHEDULED payment date. This does "
             "not establish actual payment. Re-run the revised preset test on a "
             "new analysis for actual-payment findings. "
             + (f"Actual payment evidence is blank ({payment_gap})." if payment_gap
                else "Check actual payment evidence before concluding."))
            if same_day else
            ("Capture equals ACTUAL payment date, not just scheduled payment. "
             "Verify authorization and payment records; matching dates alone "
             "do not prove a control failure.")
            if actual_same_day else ""
        ),
        "tests": tests,
        "vendor_comparison": context,
        "other_triggered_tests": [
            hit.get("test_name") or hit.get("test_id")
            for hit in hits if hit.get("test_id") != "round_dollar"
        ],
        "suggested_check": suggested_check,
    }
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": json.dumps(facts, default=str)},
    ]
    trace = [
        {"step": "selected", "detail": "Selected by DIA priority; score and band withheld from model"},
        {"step": "facts", "detail": f"{len(tests)} test(s), {len(facts['source_fields'])} source fields",
         "result": facts},
    ]
    choice = _json_response(client, model, messages, 170)
    next_step = choice.get("next")
    if next_step not in ("evidence", "related", "conclude"):
        raise ValueError("Agent requested an unsupported tool")
    trace.append({"step": "planned", "detail": next_step,
                  "reason": str(choice.get("reason") or "")[:180]})
    if next_step == "evidence":
        tool = [
            {"test": hit.get("test_name"), **item}
            for hit in hits for item in _evidence(hit, row)
        ][:12]
    elif next_step == "related":
        matches = [
            other for i, other in enumerate(rows)
            if i != risk.row_idx and vendor and
            str(other.get("vendor_number") or other.get("vendor_name") or "") == vendor
        ]
        tool = {
            "related_count": len(matches),
            "sample": [
                {"invoice": other.get("physical_invoice_no") or other.get("system_invoice_no"),
                 "amount": other.get("amount_excl_lc")}
                for other in matches[:5]
            ],
        }
    else:
        tool = {"message": "No further tool requested; use the provided invoice facts."}
    trace.append({"step": "tool_result", "detail": next_step, "result": tool})
    messages.append({"role": "assistant", "content": json.dumps(choice)})
    messages.append({"role": "user", "content": "Tool result: " + json.dumps(tool, default=str)
                     + "\nNow return the final recommendation JSON."})
    verdict = _json_response(client, model, messages, 280)
    recommendation = verdict.get("recommendation")
    if recommendation not in ("confirm", "dismiss", "request_evidence"):
        raise ValueError("Agent returned an unsupported recommendation")
    reason = str(verdict.get("reason") or "").strip()[:600]
    if not reason:
        raise ValueError("Agent supplied no reason")
    claimed = verdict.get("missing_fields")
    verified_missing = [
        field for field in missing_required
        if isinstance(claimed, list) and field in claimed
    ]
    single_round = len(risk.flagged_by) == 1 and risk.flagged_by[0].get("test_id") == "round_dollar"
    if payment_gap:
        verified_missing = sorted(set(verified_missing + [payment_gap]))
        trace.append({"step": "validation", "detail":
                      "Scheduled payment is not actual payment; actual payment evidence is blank"})
        recommendation = "request_evidence"
        reason = (
            f"Capture and scheduled payment dates match, but {payment_gap} is blank "
            "(1900 is a placeholder). Obtain actual payment evidence before deciding."
        )
    elif same_day and not actual_date:
        trace.append({"step": "validation", "detail":
                      "Actual payment date is unavailable in the source dataset"})
        recommendation = "manual_review"
        reason = "Capture matches scheduled payment, but the dataset has no actual payment date. Inspect external payment evidence before deciding."
    elif recommendation == "dismiss":
        trace.append({"step": "validation", "detail":
                      "Dismissal held: no independently verified counter-evidence",
                      "reason": reason})
        recommendation = "manual_review"
        reason = "Dismissal lacks verified counter-evidence. " + _context_summary(context) + " " + suggested_check
    elif recommendation == "confirm" and single_round:
        trace.append({"step": "validation", "detail":
                      "Confirmation held: round amount alone is not proof of wrongdoing",
                      "reason": reason})
        recommendation = "manual_review"
        reason = _context_summary(context) + " " + suggested_check
    elif recommendation == "request_evidence" and not verified_missing:
        trace.append({"step": "validation", "detail":
                      "Evidence request held: no claimed missing field matched a blank required source field"})
        recommendation = "manual_review"
        reason = "No required source field is missing. " + _context_summary(context) + " " + suggested_check
    review_action = (
        "investigate_vendor_outlier"
        if recommendation == "manual_review" and single_round
        and context["baseline_available"] and context["amount_vs_median"] >= 3
        else "verify_documents"
        if recommendation == "manual_review" and single_round
        else "inspect_source"
        if recommendation == "manual_review"
        else "collect_missing_evidence"
        if recommendation == "request_evidence"
        else "confirm"
        if recommendation == "confirm"
        else "inspect_source"
    )
    trace.append({"step": "recommended", "detail": recommendation,
                  "reason": reason})
    return {
        "schema_version": 5,
        "recommendation": recommendation,
        "review_action": review_action,
        "reason": reason,
        "missing_fields": verified_missing,
        "vendor_comparison": context,
        "suggested_check": suggested_check,
        "model": model,
        "trace": trace,
        "risk_score": risk.risk_score,
        "risk_band": risk.band_label,
        "vendor": str(row.get("vendor_name") or vendor)[:90],
        "invoice": str(facts["invoice"] or risk.row_idx),
        "amount": risk.amount,
        "currency": str(facts["currency"] or ""),
    }
