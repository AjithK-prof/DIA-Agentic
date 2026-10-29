"""Per-hit evidence extraction for built-in tests.

Given a test_id and a row, returns a list of evidence items that tell the
reviewer exactly *why* the row was flagged, so they don't have to work it out
themselves (e.g. which date is a weekend, which amount is round).

Each evidence item: {"field": <udm_field>, "value": <raw value>, "note": <label>}
"""
from .date_utils import (
    DATE_FORMATS, SENTINELS, actual_payment_date, parse_invoice_date as parse_date,
)

KEYWORDS = [
    "bonus", "gift", "personal", "cash", "loan", "advance",
    "entertainment", "alcohol", "drinks", "travel", "holiday",
    "vacation", "reward", "incentive", "tip", "gratuity",
    "donation", "contribution", "sponsorship", "refund",
    "reimbursement", "settlement", "legal", "penalty",
    "fine", "commission", "kickback", "rebate", "courtesy",
]

ROUND_THRESHOLDS = [1000000, 500000, 100000, 50000, 10000, 5000, 1000]


def day_of_week(val):
    """Return short day name (Mon..Sun) or None."""
    d = parse_date(val)
    return d.strftime("%a") if d else None


def is_weekend(val):
    d = parse_date(val)
    return d.weekday() >= 5 if d else False


def parse_amount(val) -> float:
    try:
        return abs(float(str(val or 0).replace(",", "").strip()))
    except (ValueError, TypeError):
        return 0.0


def _date_note(val, prefix=""):
    dow = day_of_week(val)
    if not dow:
        return prefix.strip() or "date"
    weekend = is_weekend(val)
    tag = f"{dow}"
    if weekend:
        tag += " (weekend)"
    return f"{prefix}{tag}".strip()


def evidence_for(test_id: str, row: dict) -> list[dict]:
    ev: list[dict] = []

    if test_id == "round_dollar":
        raw = row.get("amount_excl_lc")
        amt = parse_amount(raw)
        div = next((t for t in ROUND_THRESHOLDS if amt and amt % t == 0), None)
        note = f"round \u00f7 {div:,}" if div else "round amount"
        ev.append({"field": "amount_excl_lc", "value": raw, "note": note})

    elif test_id == "duplicate_invoice":
        ev.append({"field": "vendor_number", "value": row.get("vendor_number"), "note": "same vendor"})
        ev.append({"field": "physical_invoice_no",
                   "value": row.get("physical_invoice_no") or row.get("system_invoice_no"),
                   "note": "same invoice no"})
        ev.append({"field": "amount_excl_lc", "value": row.get("amount_excl_lc"), "note": "same amount"})

    elif test_id == "keyword_suspicious":
        raw = row.get("invoice_description")
        desc = str(raw or "").lower()
        kw = next((k for k in KEYWORDS if k in desc), None)
        note = f"keyword: {kw}" if kw else "suspicious keyword"
        ev.append({"field": "invoice_description", "value": raw, "note": note})

    elif test_id == "same_day_actual_payment":
        cap = row.get("date_captured")
        payment_field = "payment_date" if parse_date(row.get("payment_date")) else "actual_pay_date"
        actual = row.get(payment_field)
        ev.append({"field": "date_captured", "value": cap, "note": _date_note(cap, "captured ")})
        ev.append({"field": payment_field, "value": actual,
                   "note": _date_note(actual, "actually paid ") + " \u00b7 same day"})

    elif test_id == "same_day_payment":
        # Historical test sessions used scheduled payment, not actual payment.
        # Preserve their evidence without relabeling past findings as actual payments.
        cap = row.get("date_captured")
        sch = row.get("scheduled_pay_date")
        ev.append({"field": "date_captured", "value": cap, "note": _date_note(cap, "captured ")})
        ev.append({"field": "scheduled_pay_date", "value": sch,
                   "note": _date_note(sch, "scheduled ") + " \u00b7 same day (legacy test)"})

    else:
        # ── Generic evidence for AI-generated tests ──────────────────────────
        # Surfaces the actual values of key fields the test is likely to have
        # compared, so reviewers can see what the code evaluated.
        ev.extend(_generic_evidence(test_id, row))

    return [e for e in ev if e.get("value") not in (None, "", "None")]


# ── Generic evidence for generated tests ────────────────────────────────────────

# Map of signal words in test_id → fields to expose
_GEN_EVIDENCE_MAP = [
    # Payment timing — check both payment_date and actual_pay_date
    ({"payment", "before", "invoice", "date", "paid"},
     [("payment_date",       "actual payment date"),
      ("actual_pay_date",    "actual payment date (alt field)"),
      ("date_documented",    "invoice issued date")]),
    # Weekend / off-hours
    ({"weekend", "offhour", "off_hour", "holiday", "after_hour"},
     [("date_authorized",    None),   # note built dynamically
      ("date_captured",      None)]),
    # SoD / same person
    ({"sod", "segregation", "duties", "captured_by", "authorized_by",
      "same_person", "self_approve"},
     [("captured_by",        "captured by"),
      ("authorized_by",      "authorized by")]),
    # Near-duplicate / window
    ({"duplicate", "near", "similar", "window", "7day", "seven"},
     [("vendor_number",      "vendor"),
      ("amount_excl_lc",     "amount"),
      ("physical_invoice_no","invoice no"),
      ("date_documented",    "invoice date")]),
    # Amount outlier / threshold
    ({"amount", "threshold", "limit", "outlier", "spike", "high_value"},
     [("amount_excl_lc",     "amount flagged"),
      ("vendor_number",      "vendor")]),
    # Missing PO
    ({"po", "purchase_order", "non_po", "missing_po"},
     [("po_non_po",          "PO status"),
      ("amount_excl_lc",     "amount")]),
]


def _generic_evidence(test_id: str, row: dict) -> list[dict]:
    """Build evidence for a generated test based on signal words in the test_id.

    For payment-timing tests: if both payment_date and actual_pay_date are blank,
    return no evidence at all — the test code should not have flagged this row.
    """
    ev: list[dict] = []
    tid = test_id.lower()
    seen_fields: set[str] = set()

    # A nonblank placeholder is not evidence of an executed payment.
    if any(kw in tid for kw in ("payment", "paid")) and actual_payment_date(row) is None:
        return []

    for keywords, fields in _GEN_EVIDENCE_MAP:
        # match if any keyword appears as a substring of the test_id
        if not any(kw in tid for kw in keywords):
            continue
        for field, base_note in fields:
            if field in seen_fields:
                continue
            seen_fields.add(field)
            val = row.get(field)
            if val in (None, "", "None", "none", "null"):
                # Field is blank — do NOT add any evidence chip.
                # The timeline warning in results.js handles the "both payment fields blank"
                # message separately; we must not add a "(blank)" item here because it
                # would be displayed as if the test positively matched the field.
                continue
            if field in ("payment_date", "actual_pay_date", "date_documented",
                         "date_authorized", "date_captured", "scheduled_pay_date") \
                    and parse_date(val) is None:
                continue
            # Build note
            if base_note is None:
                # date field — add day-of-week
                note = _date_note(val, "")
            elif field in ("date_documented", "date_authorized", "date_captured",
                           "scheduled_pay_date", "payment_date"):
                dow = day_of_week(val)
                note = f"{base_note} · {dow}" if dow else base_note
            else:
                note = base_note
            ev.append({"field": field, "value": val, "note": note})

    return ev
