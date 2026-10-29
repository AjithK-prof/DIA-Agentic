"""Create a concise audit finding from confirmed review decisions."""
import hashlib
import json
from collections import Counter


def finding_signature(scored, decisions) -> str:
    reviewed = [
        (rs.row_idx, decisions.get(rs.row_idx, {}).get("note", ""))
        for rs in scored
        if decisions.get(rs.row_idx, {}).get("status") == "confirmed"
    ]
    return hashlib.md5(json.dumps(reviewed, sort_keys=True).encode()).hexdigest()[:16]


def build_finding_facts(rows, scored, decisions) -> dict | None:
    confirmed = [
        rs for rs in scored
        if decisions.get(rs.row_idx, {}).get("status") == "confirmed"
    ]
    if not confirmed:
        return None

    tests = Counter()
    vendors = Counter()
    notes = []
    for rs in confirmed:
        row = rows[rs.row_idx]
        vendor = str(row.get("vendor_name") or row.get("vendor_number") or "Unknown vendor").strip()
        vendors[vendor] += 1
        for test in rs.flagged_by:
            tests[test.get("test_name") or test.get("test_id") or "Unknown test"] += 1
        note = decisions.get(rs.row_idx, {}).get("note", "").strip()
        if note:
            notes.append(note)

    amount = round(sum(rs.amount for rs in confirmed), 2)
    return {
        "confirmed_count": len(confirmed),
        "amount_at_risk": amount,
        "top_patterns": [{"name": name, "count": count} for name, count in tests.most_common(5)],
        "top_vendors": [{"name": name, "count": count} for name, count in vendors.most_common(5)],
        "risk_bands": dict(Counter(rs.band_label for rs in confirmed)),
        "reviewer_notes": notes[:8],
    }


def deterministic_finding(facts: dict) -> dict:
    top_pattern = facts["top_patterns"][0]["name"] if facts["top_patterns"] else "invoice anomalies"
    top_vendor = facts["top_vendors"][0]["name"] if facts["top_vendors"] else "the reviewed vendors"
    title = f"Confirmed {top_pattern}"
    report = {
        "title": title,
        "condition": (
            f"The review confirmed {facts['confirmed_count']:,} invoices totalling "
            f"${facts['amount_at_risk']:,.2f}. The most common pattern was {top_pattern}, "
            f"and the largest invoice concentration involved {top_vendor}."
        ),
        "cause": (
            "The available invoice data indicates a control exception. Management should validate "
            "the supporting documents and approval history before assigning a final root cause."
        ),
        "risk": (
            "The exceptions may expose the organisation to duplicate, unsupported, improperly "
            "approved, or otherwise non-compliant payments."
        ),
        "recommendation": (
            "Investigate the confirmed invoices, recover any invalid payments, document the outcome, "
            "and strengthen preventive controls around the dominant test patterns."
        ),
        "facts": facts,
        "source": "deterministic",
    }
    report["plain_text"] = render_plain_text(report)
    return report


def render_plain_text(report: dict) -> str:
    return "\n\n".join([
        report.get("title", "Audit Finding"),
        f"CONDITION\n{report.get('condition', '')}",
        f"CAUSE\n{report.get('cause', '')}",
        f"RISK\n{report.get('risk', '')}",
        f"RECOMMENDATION\n{report.get('recommendation', '')}",
    ])
