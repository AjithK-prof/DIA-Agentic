"""Emergent (data-driven) anomaly detection.

Unlike the preset/generated tests which find *what the reviewer told them to
look for*, these detectors profile the whole dataset statistically and surface
patterns that no test flagged. They run in one pass, in pure Python, no LLM.

Each detector returns zero or more "emergent clusters":
    {
      "detector_id": str,
      "title": str,
      "description": str,          # plain-English why-it-matters
      "severity": "critical|high|medium|low",
      "row_indices": [int, ...],
      "evidence_chips": [str, ...],
      "sample_rows": [ {...}, ... ],
      "suggested_test": {"name": str, "prompt": str} | None,
    }

A single invoice may appear in several emergent clusters (different anomaly
types) - that is expected and useful.
"""
import math
import re
from collections import defaultdict

from dia_agentic.core.evidence import parse_amount, parse_date, day_of_week, is_weekend

MAX_SAMPLES = 6
MAX_ROWS_PER_CLUSTER = 400
SEVERITY_SCORE = {"critical": 90, "high": 72, "medium": 55, "low": 35}
ROUND_LIMITS = [1_000_000, 500_000, 250_000, 100_000, 50_000, 25_000, 10_000, 5_000]


# ── shared helpers ────────────────────────────────────────────────────────────
def _amt(row):
    return parse_amount(row.get("amount_excl_lc"))


def _vendor(row):
    return (str(row.get("vendor_number") or "").strip()
            or str(row.get("vendor_name") or "").strip())


def _best_date(row):
    for f in ("date_authorized", "date_captured", "date_documented"):
        d = parse_date(row.get(f))
        if d:
            return d
    return None


def _fmt_amt(n):
    if not n:
        return "$0"
    a = abs(n)
    if a >= 1e9:
        return f"${n/1e9:.1f}B"
    if a >= 1e6:
        return f"${n/1e6:.1f}M"
    if a >= 1e3:
        return f"${n/1e3:.1f}K"
    return f"${n:.0f}"


def _percentile(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def _num_from_invoice(row):
    """Extract the trailing integer from an invoice number for sequence checks."""
    s = str(row.get("physical_invoice_no") or row.get("system_invoice_no") or "").strip()
    m = re.search(r"(\d{2,})\s*$", s)
    return int(m.group(1)) if m else None


def _make_cluster(detector_id, title, description, severity, indices,
                  rows, scored_map, decisions, chips, evidence_fn, suggested_test=None):
    indices = list(dict.fromkeys(indices))[:MAX_ROWS_PER_CLUSTER]
    if not indices:
        return None
    amt = sum(_amt(rows[i]) for i in indices)
    reviewed = sum(1 for i in indices
                   if decisions.get(i, {}).get("status", "pending") != "pending")
    ranked = sorted(indices, key=lambda i: _amt(rows[i]), reverse=True)
    samples = []
    for i in ranked[:MAX_SAMPLES]:
        r = rows[i]
        rs = scored_map.get(i)
        samples.append({
            "row_idx": i,
            "row_key": str(i),
            "risk_score": rs.risk_score if rs else SEVERITY_SCORE[severity],
            "band_css": rs.band_css if rs else severity,
            "vendor_number": r.get("vendor_number"),
            "vendor_name": r.get("vendor_name"),
            "physical_invoice_no": r.get("physical_invoice_no") or r.get("system_invoice_no"),
            "date_authorized": r.get("date_authorized") or r.get("date_documented"),
            "date_captured": r.get("date_captured"),
            "scheduled_pay_date": r.get("scheduled_pay_date"),
            "amount_excl_lc": r.get("amount_excl_lc"),
            "reference_currency": r.get("reference_currency"),
            "invoice_description": str(r.get("invoice_description") or "")[:160],
            "evidence": evidence_fn(r) if evidence_fn else [],
            "review_status": decisions.get(i, {}).get("status", "pending"),
        })
    return {
        "detector_id": detector_id,
        "cluster_id": f"emg::{detector_id}",
        "source": "emergent",
        "title": title,
        "description": description,
        "severity": severity,
        "dominant_band": severity,
        "size": len(indices),
        "amount_at_risk": round(amt, 2),
        "amount_display": _fmt_amt(amt),
        "reviewed": reviewed,
        "reviewed_pct": round(reviewed / len(indices) * 100) if indices else 0,
        "evidence_chips": [c for c in chips if c][:4],
        "row_indices": indices,
        "sample_rows": samples,
        "suggested_test": suggested_test,
        "ai_summary": None,
    }


# ── detectors ──────────────────────────────────────────────────────────────────
def _d_amount_outlier(rows, scored_map, decisions):
    by_vendor = defaultdict(list)
    for i, r in enumerate(rows):
        v = _vendor(r)
        if v:
            by_vendor[v].append(i)
    flagged = []
    for v, idxs in by_vendor.items():
        amts = [_amt(rows[i]) for i in idxs]
        vals = [a for a in amts if a > 0]
        if len(vals) < 4:
            continue
        mean = sum(vals) / len(vals)
        var = sum((a - mean) ** 2 for a in vals) / len(vals)
        std = math.sqrt(var)
        if std <= 0:
            continue
        for i in idxs:
            a = _amt(rows[i])
            z = (a - mean) / std
            if z >= 3 and a >= mean * 3:
                flagged.append((i, z, mean))
    if len(flagged) < 2:
        return []
    idxs = [i for i, _, _ in flagged]

    def ev(r):
        return [{"field": "amount_excl_lc", "value": r.get("amount_excl_lc"),
                 "note": "far above vendor norm"}]
    return [_make_cluster(
        "amount_outlier", "Amounts far above vendor's norm",
        "Invoices dramatically larger than what this vendor usually bills (>3 std dev).",
        "high", idxs, rows, scored_map, decisions,
        [f"{len(idxs)} outliers", "vs vendor avg"], ev,
        {"name": "Vendor amount outlier",
         "prompt": "Flag invoices whose amount_excl_lc is more than 3x the average amount for the same vendor_number."})]


def _d_threshold_hugging(rows, scored_map, decisions):
    idxs = []
    hit_limit = defaultdict(int)
    for i, r in enumerate(rows):
        a = _amt(r)
        if a <= 0:
            continue
        for T in ROUND_LIMITS:
            if 0.9 * T <= a < T:
                idxs.append(i)
                hit_limit[T] += 1
                break
    if len(idxs) < 3:
        return []
    top_T = max(hit_limit, key=hit_limit.get)

    def ev(r):
        return [{"field": "amount_excl_lc", "value": r.get("amount_excl_lc"),
                 "note": "just below limit"}]
    return [_make_cluster(
        "threshold_hugging", "Amounts hugging approval limits",
        "Invoices priced just under round approval thresholds - possible structuring to avoid sign-off.",
        "high", idxs, rows, scored_map, decisions,
        [f"{len(idxs)} near-limit", f"top: <{_fmt_amt(top_T)}"], ev,
        {"name": "Threshold hugging",
         "prompt": "Flag invoices where amount_excl_lc is between 90% and 100% of a round approval limit such as 10000, 50000, 100000, 1000000."})]


def _d_benford(rows, scored_map, decisions):
    lead = defaultdict(list)
    total = 0
    for i, r in enumerate(rows):
        a = _amt(r)
        if a >= 10:
            d = int(str(int(a))[0])
            if 1 <= d <= 9:
                lead[d].append(i)
                total += 1
    if total < 200:
        return []
    expected = {d: math.log10(1 + 1 / d) for d in range(1, 10)}
    chi = sum(((len(lead[d]) / total) - expected[d]) ** 2 / expected[d] for d in range(1, 10))
    worst = max(range(1, 10), key=lambda d: (len(lead[d]) / total) - expected[d])
    over = (len(lead[worst]) / total) - expected[worst]
    if over < 0.04:
        return []
    idxs = lead[worst]

    def ev(r):
        return [{"field": "amount_excl_lc", "value": r.get("amount_excl_lc"),
                 "note": f"leading digit {worst}"}]
    return [_make_cluster(
        "benford", f"Benford deviation (leading digit {worst})",
        "The distribution of leading digits deviates from Benford's Law, a classic sign of fabricated figures.",
        "medium", idxs, rows, scored_map, decisions,
        [f"digit {worst} over-represented", f"+{over*100:.0f}% vs expected"], ev,
        {"name": f"Leading-digit {worst} concentration",
         "prompt": f"Flag invoices where the first digit of amount_excl_lc is {worst}."})]


def _d_segregation_of_duties(rows, scored_map, decisions):
    idxs = []
    for i, r in enumerate(rows):
        cap = str(r.get("captured_by") or "").strip().lower()
        auth = str(r.get("authorized_by") or "").strip().lower()
        if cap and auth and cap == auth:
            idxs.append(i)
    if not idxs:
        return []

    def ev(r):
        return [{"field": "captured_by", "value": r.get("captured_by"), "note": "same person"},
                {"field": "authorized_by", "value": r.get("authorized_by"), "note": "captured = authorized"}]
    return [_make_cluster(
        "sod", "Same person captured and authorized",
        "Segregation-of-duties failure: the invoice was entered and approved by the same user.",
        "critical", idxs, rows, scored_map, decisions,
        [f"{len(idxs)} SoD breaches"], ev,
        {"name": "Segregation of duties",
         "prompt": "Flag invoices where captured_by equals authorized_by."})]


def _d_sequential_invoices(rows, scored_map, decisions):
    by_vendor = defaultdict(list)
    for i, r in enumerate(rows):
        v = _vendor(r)
        n = _num_from_invoice(r)
        if v and n is not None:
            by_vendor[v].append((n, i))
    idxs = []
    for v, pairs in by_vendor.items():
        pairs.sort()
        run = [pairs[0]]
        for k in range(1, len(pairs)):
            if pairs[k][0] == pairs[k - 1][0] + 1:
                run.append(pairs[k])
            else:
                if len(run) >= 3:
                    idxs.extend(i for _, i in run)
                run = [pairs[k]]
        if len(run) >= 3:
            idxs.extend(i for _, i in run)
    if not idxs:
        return []

    def ev(r):
        return [{"field": "physical_invoice_no",
                 "value": r.get("physical_invoice_no") or r.get("system_invoice_no"),
                 "note": "consecutive number"}]
    return [_make_cluster(
        "sequential_invoice", "Consecutive invoice numbers",
        "Runs of sequentially-numbered invoices from one vendor - a signature of fabricated invoice books.",
        "high", idxs, rows, scored_map, decisions,
        [f"{len(idxs)} in sequences"], ev,
        {"name": "Sequential invoice numbers",
         "prompt": "Flag invoices from the same vendor_number whose physical_invoice_no values are consecutive integers."})]


def _d_fuzzy_duplicates(rows, scored_map, decisions):
    by_key = defaultdict(list)
    for i, r in enumerate(rows):
        v = _vendor(r)
        a = _amt(r)
        if v and a > 0:
            by_key[(v, round(a, 2))].append(i)
    idxs = []
    for (v, a), group in by_key.items():
        invs = {str(rows[i].get("physical_invoice_no") or rows[i].get("system_invoice_no") or "").strip()
                for i in group}
        if len(group) >= 2 and len(invs) >= 2:
            idxs.extend(group)
    if not idxs:
        return []

    def ev(r):
        return [{"field": "amount_excl_lc", "value": r.get("amount_excl_lc"), "note": "same amount"},
                {"field": "vendor_number", "value": r.get("vendor_number"), "note": "same vendor"}]
    return [_make_cluster(
        "fuzzy_duplicate", "Near-duplicate invoices",
        "Same vendor and identical amount but different invoice numbers - duplicates the exact-match test misses.",
        "high", idxs, rows, scored_map, decisions,
        [f"{len(idxs)} near-dupes"], ev,
        {"name": "Fuzzy duplicate",
         "prompt": "Flag invoices with the same vendor_number and same amount_excl_lc but different physical_invoice_no."})]


def _d_velocity_spike(rows, scored_map, decisions):
    by_vendor = defaultdict(list)
    for i, r in enumerate(rows):
        v = _vendor(r)
        d = _best_date(r)
        if v and d:
            by_vendor[v].append((d, i))
    idxs = []
    for v, pairs in by_vendor.items():
        if len(pairs) < 5:
            continue
        pairs.sort()
        window = []
        for d, i in pairs:
            window.append((d, i))
            window = [(wd, wi) for wd, wi in window if (d - wd).days <= 3]
            if len(window) >= 5:
                idxs.extend(wi for _, wi in window)
    if not idxs:
        return []

    def ev(r):
        return [{"field": "date_captured", "value": r.get("date_captured") or r.get("date_authorized"),
                 "note": "burst window"}]
    return [_make_cluster(
        "velocity_spike", "Vendor billing bursts",
        "Vendors that submitted an unusual burst of invoices within a few days.",
        "medium", idxs, rows, scored_map, decisions,
        [f"{len(idxs)} in bursts", "5+ in 3 days"], ev,
        {"name": "Velocity spike",
         "prompt": "Flag invoices where the same vendor_number has 5 or more invoices within a 3-day window."})]


def _d_new_vendor_high_value(rows, scored_map, decisions):
    by_vendor = defaultdict(list)
    for i, r in enumerate(rows):
        v = _vendor(r)
        if v:
            by_vendor[v].append(i)
    amts = sorted(a for a in (_amt(r) for r in rows) if a > 0)
    if not amts:
        return []
    p90 = _percentile(amts, 0.90)
    idxs = []
    for v, group in by_vendor.items():
        if len(group) == 1:
            i = group[0]
            if _amt(rows[i]) >= p90:
                idxs.append(i)
    if len(idxs) < 2:
        return []

    def ev(r):
        return [{"field": "amount_excl_lc", "value": r.get("amount_excl_lc"), "note": "high value"},
                {"field": "vendor_number", "value": r.get("vendor_number"), "note": "only invoice"}]
    return [_make_cluster(
        "new_vendor_high_value", "One-off high-value vendors",
        "Vendors with a single, large invoice (top 10% by amount) - possible shell-vendor risk.",
        "high", idxs, rows, scored_map, decisions,
        [f"{len(idxs)} one-off vendors", f">={_fmt_amt(p90)}"], ev,
        {"name": "New vendor high value",
         "prompt": "Flag invoices from vendors that appear only once in the file where amount_excl_lc is in the top 10% of all amounts."})]


def _d_approver_concentration(rows, scored_map, decisions):
    by_appr = defaultdict(list)
    total = 0.0
    for i, r in enumerate(rows):
        auth = str(r.get("authorized_by") or "").strip()
        a = _amt(r)
        if auth:
            by_appr[auth].append(i)
            total += a
    if not by_appr or total <= 0 or len(by_appr) < 3:
        return []
    appr_amt = {a: sum(_amt(rows[i]) for i in g) for a, g in by_appr.items()}
    top = max(appr_amt, key=appr_amt.get)
    share = appr_amt[top] / total
    if share < 0.4:
        return []
    idxs = by_appr[top]

    def ev(r):
        return [{"field": "authorized_by", "value": r.get("authorized_by"),
                 "note": f"{share*100:.0f}% of value"}]
    return [_make_cluster(
        "approver_concentration", "Approver value concentration",
        f"A single approver authorised {share*100:.0f}% of total invoice value - concentration risk.",
        "medium", idxs, rows, scored_map, decisions,
        [f"approver {top}", f"{share*100:.0f}% of value"], ev,
        {"name": "Approver concentration",
         "prompt": "Flag invoices authorised by an approver who accounts for more than 40% of total invoice value."})]


def _d_dormant_reactivation(rows, scored_map, decisions):
    by_vendor = defaultdict(list)
    for i, r in enumerate(rows):
        v = _vendor(r)
        d = _best_date(r)
        if v and d:
            by_vendor[v].append((d, i))
    idxs = []
    for v, pairs in by_vendor.items():
        if len(pairs) < 2:
            continue
        pairs.sort()
        for k in range(1, len(pairs)):
            gap = (pairs[k][0] - pairs[k - 1][0]).days
            if gap >= 180:
                idxs.append(pairs[k][1])
    if not idxs:
        return []

    def ev(r):
        return [{"field": "date_authorized", "value": r.get("date_authorized") or r.get("date_captured"),
                 "note": "after long dormancy"}]
    return [_make_cluster(
        "dormant_reactivation", "Dormant vendor reactivation",
        "Vendors that were inactive for 6+ months then suddenly billed again.",
        "medium", idxs, rows, scored_map, decisions,
        [f"{len(idxs)} reactivations", "180+ day gap"], ev,
        {"name": "Dormant vendor reactivation",
         "prompt": "Flag invoices from a vendor_number that had no invoices for at least 180 days before this one."})]


DETECTORS = [
    _d_segregation_of_duties,
    _d_amount_outlier,
    _d_threshold_hugging,
    _d_fuzzy_duplicates,
    _d_sequential_invoices,
    _d_new_vendor_high_value,
    _d_velocity_spike,
    _d_approver_concentration,
    _d_dormant_reactivation,
    _d_benford,
]

_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def detect_anomalies(rows, scored, decisions) -> list[dict]:
    scored_map = {rs.row_idx: rs for rs in scored}
    out = []
    for fn in DETECTORS:
        try:
            for cluster in fn(rows, scored_map, decisions):
                if cluster:
                    out.append(cluster)
        except Exception:
            continue
    out.sort(key=lambda c: (_SEVERITY_RANK.get(c["severity"], 9), -c["amount_at_risk"]))
    for rank, c in enumerate(out, 1):
        c["rank"] = rank
    return out
