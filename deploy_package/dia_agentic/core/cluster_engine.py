"""Rule-based clustering of test hits for the review page.

Primary grouping is by the *anomaly signature* - the combination of tests that
fired on each invoice. This yields a small number of meaningful clusters
("Duplicate + Round Dollar", "Round Dollar only", ...) instead of hundreds of
per-vendor fragments.

Other groupings (vendor / value band / weekend / approver) are available via the
`group_by` argument so a reviewer can toggle the lens.

Large single-signature clusters are auto-split by their strongest secondary
trait when that split is meaningful.

Ranking: most tests fired first, then amount at risk.
"""
import hashlib
from collections import defaultdict

from dia_agentic.core.evidence import (
    evidence_for, parse_amount, parse_date, is_weekend,
)

MAX_SAMPLES = 6
MAX_CLUSTERS = 60
SPLIT_THRESHOLD = 40        # single-signature clusters bigger than this may be split
SPLIT_MIN_SUBGROUP = 6      # a sub-group must have at least this many rows to count


def _amount(row):
    return parse_amount(row.get("amount_excl_lc"))


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


def _value_band(amt):
    if amt >= 100000:
        return ">100k"
    if amt >= 10000:
        return "10k-100k"
    if amt >= 1000:
        return "1k-10k"
    return "<1k"


# ── grouping dimensions (for toggle + splitting) ──────────────────────────────
def _key_vendor(r):
    return (str(r.get("vendor_number") or "").strip()
            or str(r.get("vendor_name") or "").strip() or "(no vendor)")


def _key_value(r):
    return _value_band(_amount(r))


def _key_temporal(r):
    for f in ("date_authorized", "date_captured", "date_documented"):
        d = parse_date(r.get(f))
        if d:
            return "weekend" if d.weekday() >= 5 else "weekday"
    return "no date"


def _key_person(r):
    return (str(r.get("authorized_by") or "").strip()
            or str(r.get("captured_by") or "").strip() or "(unknown approver)")


DIMENSIONS = {
    "vendor":   {"label": "Vendor",            "key": _key_vendor},
    "value":    {"label": "Value band",        "key": _key_value},
    "temporal": {"label": "Weekend / weekday", "key": _key_temporal},
    "person":   {"label": "Approver",          "key": _key_person},
}


def _dominant_band(scored_map, indices):
    counts = defaultdict(int)
    for i in indices:
        rs = scored_map.get(i)
        if rs:
            counts[rs.band_css] += 1
    order = ["critical", "high", "medium", "low"]
    return max(order, key=lambda b: (counts.get(b, 0), -order.index(b))) if counts else "low"


def _evidence_chips(indices, rows, test_ids):
    chips = []
    amts = [_amount(rows[i]) for i in indices]
    amts = [a for a in amts if a > 0]
    if amts:
        chips.append(f"avg {_fmt_amt(sum(amts)/len(amts))}")
    weekend = sum(1 for i in indices if any(
        is_weekend(rows[i].get(f)) for f in ("date_authorized", "date_captured", "date_documented")))
    if weekend and weekend >= len(indices) * 0.5:
        chips.append(f"{round(weekend/len(indices)*100)}% weekend")
    vendors = {str(rows[i].get("vendor_number") or "").strip() for i in indices}
    vendors.discard("")
    if vendors:
        chips.append(f"{len(vendors)} vendor{'s' if len(vendors) != 1 else ''}")
    if "keyword_suspicious" in test_ids:
        from dia_agentic.core.evidence import KEYWORDS
        kw_counts = defaultdict(int)
        for i in indices:
            desc = str(rows[i].get("invoice_description") or "").lower()
            for k in KEYWORDS:
                if k in desc:
                    kw_counts[k] += 1
                    break
        if kw_counts:
            chips.append(f"top: \u201c{max(kw_counts, key=kw_counts.get)}\u201d")
    return chips[:4]


def _sample_rows(indices, rows, scored_map, test_ids, decisions):
    ranked = sorted(indices, key=lambda i: (scored_map[i].risk_score if scored_map.get(i) else 0),
                    reverse=True)
    out = []
    for i in ranked[:MAX_SAMPLES]:
        r = rows[i]
        rs = scored_map.get(i)
        ev = []
        for tid in test_ids:
            ev.extend(evidence_for(tid, r))
        out.append({
            "row_idx": i,
            "row_key": str(i),
            "risk_score": rs.risk_score if rs else 0,
            "band_css": rs.band_css if rs else "low",
            "vendor_number": r.get("vendor_number"),
            "vendor_name": r.get("vendor_name"),
            "physical_invoice_no": r.get("physical_invoice_no") or r.get("system_invoice_no"),
            "date_authorized": r.get("date_authorized") or r.get("date_documented"),
            "date_captured": r.get("date_captured"),
            "scheduled_pay_date": r.get("scheduled_pay_date"),
            "amount_excl_lc": r.get("amount_excl_lc"),
            "reference_currency": r.get("reference_currency"),
            "invoice_description": str(r.get("invoice_description") or "")[:160],
            "evidence": ev,
            "review_status": decisions.get(i, {}).get("status", "pending"),
        })
    return out


def _best_split(indices, rows):
    """For a large single-signature cluster, try to split by a secondary trait.
    Returns (dim_name, {subkey: [idx,...]}) or (None, None) if no meaningful split."""
    best = (None, None, -1.0)
    for name in ("temporal", "value", "vendor"):
        keyfn = DIMENSIONS[name]["key"]
        groups = defaultdict(list)
        for i in indices:
            groups[keyfn(rows[i])].append(i)
        big = {k: v for k, v in groups.items() if len(v) >= SPLIT_MIN_SUBGROUP}
        if len(big) < 2 or len(big) > 6:
            continue
        covered = sum(len(v) for v in big.values())
        coverage = covered / len(indices)
        spread = 1.0 - (max(len(v) for v in big.values()) / len(indices))
        score = coverage * 0.6 + spread * 0.4
        if score > best[2] and coverage >= 0.6:
            best = (name, dict(big), score)
    return best[0], best[1]


def build_clusters(rows, test_results, scored, decisions, group_by="signature"):
    """Returns {test_overview, rule_clusters, group_by, signature}."""
    scored_map = {rs.row_idx: rs for rs in scored}
    flagged_by_row = {rs.row_idx: rs.flagged_by for rs in scored}
    id_to_name = {}
    applicable = []
    for t in test_results:
        if t.get("applicable") and t.get("hit_count", 0) > 0:
            applicable.append(t)
            id_to_name[t["test_id"]] = t["test_name"]

    # ── per-test overview strip (unchanged concept) ──
    test_overview = []
    for t in applicable:
        idxs = [i for i in t.get("all_hit_indices", []) if i in scored_map]
        if not idxs:
            continue
        test_overview.append({
            "test_id": t["test_id"],
            "test_name": t["test_name"],
            "type": t.get("type", "preset"),
            "severity": t.get("severity", "warning"),
            "hit_count": len(idxs),
            "hit_pct": t.get("hit_pct", 0),
            "amount_at_risk": round(sum(_amount(rows[i]) for i in idxs), 2),
        })

    clusters = _group(rows, scored_map, flagged_by_row, id_to_name, decisions, group_by)

    # rank: most tests fired, then amount at risk
    def rank_key(c):
        return (-c.get("test_count", 1), -c["amount_at_risk"])
    clusters.sort(key=rank_key)
    clusters = clusters[:MAX_CLUSTERS]
    for rank, c in enumerate(clusters, 1):
        c["rank"] = rank

    signature = hashlib.md5(
        (group_by + "|" + "|".join(f"{c['cluster_id']}:{c['size']}" for c in clusters)).encode()
    ).hexdigest()[:16]

    return {"test_overview": test_overview, "rule_clusters": clusters,
            "group_by": group_by, "signature": signature}


def _make_cluster(cluster_id, title, subtitle, test_ids, id_to_name, indices,
                  rows, scored_map, decisions, test_count):
    amt = sum(_amount(rows[i]) for i in indices)
    reviewed = sum(1 for i in indices
                   if decisions.get(i, {}).get("status", "pending") != "pending")
    return {
        "cluster_id": cluster_id,
        "source": "rule",
        "title": title,
        "subtitle": subtitle,
        "test_ids": test_ids,
        "test_names": [id_to_name.get(t, t) for t in test_ids],
        "test_count": test_count,
        "size": len(indices),
        "amount_at_risk": round(amt, 2),
        "amount_display": _fmt_amt(amt),
        "dominant_band": _dominant_band(scored_map, indices),
        "reviewed": reviewed,
        "reviewed_pct": round(reviewed / len(indices) * 100) if indices else 0,
        "evidence_chips": _evidence_chips(indices, rows, test_ids),
        "row_indices": indices,
        "sample_rows": _sample_rows(indices, rows, scored_map, test_ids, decisions),
        "ai_summary": None,
    }


def _group(rows, scored_map, flagged_by_row, id_to_name, decisions, group_by):
    clusters = []

    if group_by == "signature":
        # group rows by the set of tests that flagged them
        sig_groups = defaultdict(list)
        for idx, tests in flagged_by_row.items():
            tids = tuple(sorted(t["test_id"] for t in tests))
            if not tids:
                continue
            sig_groups[tids].append(idx)
        for tids, idxs in sig_groups.items():
            names = [id_to_name.get(t, t) for t in tids]
            title = " + ".join(names)
            base_id = "rule::" + hashlib.md5("+".join(tids).encode()).hexdigest()[:8]
            tc = len(tids)

            # auto-split only single-test, large clusters
            if tc == 1 and len(idxs) > SPLIT_THRESHOLD:
                dim, sub = _best_split(idxs, rows)
                if sub:
                    dlabel = DIMENSIONS[dim]["label"]
                    assigned = set()
                    for subkey, subidxs in sorted(sub.items(), key=lambda kv: -sum(_amount(rows[i]) for i in kv[1])):
                        cid = f"{base_id}::{dim}={hashlib.md5(str(subkey).encode()).hexdigest()[:6]}"
                        clusters.append(_make_cluster(
                            cid, f"{title} · {subkey}", f"{tc} test · split by {dlabel}",
                            list(tids), id_to_name, subidxs, rows, scored_map, decisions, tc))
                        assigned.update(subidxs)
                    rest = [i for i in idxs if i not in assigned]
                    if len(rest) >= SPLIT_MIN_SUBGROUP:
                        cid = f"{base_id}::{dim}=other"
                        clusters.append(_make_cluster(
                            cid, f"{title} · other", f"{tc} test · split by {dlabel}",
                            list(tids), id_to_name, rest, rows, scored_map, decisions, tc))
                    continue

            subtitle = f"{tc} tests fired" if tc > 1 else "1 test"
            clusters.append(_make_cluster(
                base_id, title, subtitle, list(tids), id_to_name, idxs,
                rows, scored_map, decisions, tc))
        return clusters

    # other groupings: single lens across all flagged rows
    keyfn = DIMENSIONS.get(group_by, DIMENSIONS["vendor"])["key"]
    dlabel = DIMENSIONS.get(group_by, DIMENSIONS["vendor"])["label"]
    groups = defaultdict(list)
    for idx in flagged_by_row:
        groups[keyfn(rows[idx])].append(idx)
    for key, idxs in groups.items():
        tids = sorted({t["test_id"] for i in idxs for t in flagged_by_row.get(i, [])})
        title = str(key)
        if group_by == "vendor":
            names = {str(rows[i].get("vendor_name") or "").strip() for i in idxs}
            names.discard("")
            if names and key != "(no vendor)":
                title = f"{next(iter(names))} ({key})"
        cid = f"rule::{group_by}::{hashlib.md5(str(key).encode()).hexdigest()[:8]}"
        clusters.append(_make_cluster(
            cid, title, f"grouped by {dlabel}", tids, id_to_name, idxs,
            rows, scored_map, decisions, len(tids)))
    return clusters
