"""Build a compact, evidence-only investigation brief for an anomaly cluster."""
from collections import Counter
from datetime import date

from dia_agentic.core.evidence import parse_amount, parse_date


def _top_entities(rows, indices, field, fallback=None, limit=3):
    counts = Counter()
    amounts = Counter()
    for idx in indices:
        value = str(rows[idx].get(field) or (rows[idx].get(fallback) if fallback else "") or "").strip()
        if not value:
            continue
        counts[value] += 1
        amounts[value] += parse_amount(rows[idx].get("amount_excl_lc"))
    return [
        {"name": name, "invoice_count": count, "amount": round(amounts[name], 2)}
        for name, count in counts.most_common(limit)
    ]


def _date_range(rows, indices):
    dates: list[date] = []
    for idx in indices:
        row = rows[idx]
        for field in ("date_authorized", "date_captured", "date_documented"):
            parsed = parse_date(row.get(field))
            if parsed:
                dates.append(parsed)
                break
    return {
        "first_date": min(dates).isoformat() if dates else None,
        "last_date": max(dates).isoformat() if dates else None,
    }


def build_investigation_facts(
    cluster: dict,
    all_clusters: list[dict],
    rows: list[dict],
    decisions: dict[int, dict],
    historical_rows: list[list[dict]] | None = None,
) -> dict:
    indices = sorted(set(cluster.get("row_indices", [])))
    vendor_keys = {
        str(rows[i].get("vendor_number") or rows[i].get("vendor_name") or "").strip().lower()
        for i in indices
    }
    vendor_keys.discard("")

    related = []
    current = set(indices)
    for other in all_clusters:
        if other["cluster_id"] == cluster["cluster_id"]:
            continue
        overlap = current.intersection(other.get("row_indices", []))
        if overlap:
            related.append({
                "cluster_id": other["cluster_id"],
                "title": other["title"],
                "shared_invoices": len(overlap),
            })
    related.sort(key=lambda x: -x["shared_invoices"])

    prior_count = 0
    prior_amount = 0.0
    for prior_rows in historical_rows or []:
        for row in prior_rows:
            vendor = str(row.get("vendor_number") or row.get("vendor_name") or "").strip().lower()
            if vendor and vendor in vendor_keys:
                prior_count += 1
                prior_amount += parse_amount(row.get("amount_excl_lc"))

    statuses = Counter(decisions.get(i, {}).get("status", "pending") for i in indices)
    facts = {
        "cluster_id": cluster["cluster_id"],
        "title": cluster["title"],
        "source": cluster.get("source", "rule"),
        "invoice_count": len(indices),
        "amount_at_risk": round(sum(parse_amount(rows[i].get("amount_excl_lc")) for i in indices), 2),
        "test_names": cluster.get("test_names", []),
        "evidence_signals": cluster.get("evidence_chips", []),
        "top_vendors": _top_entities(rows, indices, "vendor_name", "vendor_number"),
        "top_approvers": _top_entities(rows, indices, "authorized_by"),
        "related_patterns": related[:5],
        "confirmed": statuses["confirmed"],
        "false_positive": statuses["false_positive"],
        "needs_info": statuses["needs_info"],
        "pending": statuses["pending"],
        "prior_vendor_invoice_count": prior_count,
        "prior_vendor_amount": round(prior_amount, 2),
        **_date_range(rows, indices),
    }
    return facts


def deterministic_investigation(facts: dict) -> dict:
    vendor = facts["top_vendors"][0] if facts["top_vendors"] else None
    approver = facts["top_approvers"][0] if facts["top_approvers"] else None
    parts = [
        f"{facts['invoice_count']:,} invoices in this pattern represent "
        f"${facts['amount_at_risk']:,.2f} of exposure."
    ]
    if vendor:
        parts.append(
            f"The largest concentration is {vendor['name']} with "
            f"{vendor['invoice_count']:,} invoices (${vendor['amount']:,.2f})."
        )
    if approver:
        parts.append(
            f"{approver['name']} is the most frequent approver "
            f"({approver['invoice_count']:,} invoices)."
        )
    if facts["related_patterns"]:
        parts.append(
            f"The same invoices also appear in {len(facts['related_patterns'])} other anomaly "
            f"pattern{'s' if len(facts['related_patterns']) != 1 else ''}."
        )
    if facts["prior_vendor_invoice_count"]:
        parts.append(
            f"The involved vendors have {facts['prior_vendor_invoice_count']:,} invoices worth "
            f"${facts['prior_vendor_amount']:,.2f} in earlier stored runs."
        )

    actions = [
        "Verify the invoice documents and purchase support for the highest-value items.",
        "Review the approval path and confirm the approver was authorized for the amounts.",
    ]
    if facts["related_patterns"]:
        actions.insert(0, "Review the connected anomaly patterns together before closing this cluster.")
    if facts["pending"]:
        actions.append(f"Resolve the {facts['pending']:,} pending review decisions and record evidence.")

    return {
        "summary": " ".join(parts),
        "next_actions": actions[:4],
        "facts": facts,
        "source": "deterministic",
    }
