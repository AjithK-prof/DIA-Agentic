"""
Data quality analyser — runs after ingest, before tests.
Computes null rates, date format consistency, currency mix, amount stats.
"""
from collections import Counter
import re

DATE_FORMATS = [
    (r'^\d{2}-\d{2}-\d{4}$', 'DD-MM-YYYY'),
    (r'^\d{4}-\d{2}-\d{2}$', 'YYYY-MM-DD'),
    (r'^\d{2}/\d{2}/\d{4}$', 'DD/MM/YYYY or MM/DD/YYYY'),
    (r'^\d{8}$',              'YYYYMMDD'),
]

DATE_FIELDS   = ['date_documented','date_captured','date_authorized',
                 'scheduled_pay_date','payment_date','due_date','actual_pay_date']
AMOUNT_FIELDS = ['amount_excl_lc','amount_incl_lc','amount_vat_lc']


def _detect_date_format(val: str) -> str | None:
    v = str(val).strip()
    for pattern, label in DATE_FORMATS:
        if re.match(pattern, v):
            return label
    return 'unknown'


def _safe_float(val) -> float | None:
    try:
        return float(str(val).replace(',', '').strip())
    except Exception:
        return None


def analyse(rows: list[dict]) -> dict:
    if not rows:
        return {}

    total = len(rows)
    fields = list(rows[0].keys())
    field_stats = {}

    for field in fields:
        values     = [r.get(field) for r in rows]
        non_null   = [v for v in values if v is not None and str(v).strip() not in ('', 'None', 'null', 'nan', 'N/A')]
        null_count = total - len(non_null)
        null_pct   = round(null_count / total * 100, 1)

        stat: dict = {
            "null_count": null_count,
            "null_pct":   null_pct,
            "filled_pct": round(100 - null_pct, 1),
            "quality":    "good" if null_pct < 5 else "warn" if null_pct < 30 else "bad",
        }

        # Date fields — check format consistency
        if field in DATE_FIELDS and non_null:
            fmt_counts: Counter = Counter()
            sentinel = {'01-01-1900','1900-01-01','01/01/1900'}
            real = [v for v in non_null if str(v).strip() not in sentinel]
            for v in real:
                fmt_counts[_detect_date_format(str(v))] += 1
            stat["date_formats"]       = dict(fmt_counts.most_common(5))
            stat["dominant_format"]    = fmt_counts.most_common(1)[0][0] if fmt_counts else None
            stat["format_consistent"]  = len(fmt_counts) <= 1
            stat["sentinel_count"]     = len(non_null) - len(real)

        # Amount fields — basic stats
        if field in AMOUNT_FIELDS and non_null:
            nums = [f for v in non_null if (f := _safe_float(v)) is not None]
            if nums:
                stat["min"]  = round(min(nums), 2)
                stat["max"]  = round(max(nums), 2)
                stat["mean"] = round(sum(nums) / len(nums), 2)
                stat["zero_count"] = sum(1 for n in nums if n == 0)
                stat["negative_count"] = sum(1 for n in nums if n < 0)

        field_stats[field] = stat

    # Currency mix
    currencies: Counter = Counter()
    for r in rows:
        c = r.get('reference_currency')
        if c:
            currencies[str(c).strip().upper()] += 1
    currency_mix = dict(currencies.most_common())

    # Overall score
    filled_pcts = [s["filled_pct"] for s in field_stats.values()]
    overall_completeness = round(sum(filled_pcts) / len(filled_pcts), 1) if filled_pcts else 0

    bad_fields  = [f for f, s in field_stats.items() if s["quality"] == "bad"]
    warn_fields = [f for f, s in field_stats.items() if s["quality"] == "warn"]

    return {
        "total_rows":            total,
        "total_fields":          len(fields),
        "overall_completeness":  overall_completeness,
        "bad_fields":            bad_fields,
        "warn_fields":           warn_fields,
        "currency_mix":          currency_mix,
        "field_stats":           field_stats,
    }


def compare_runs(old_rows: list[dict], new_rows: list[dict]) -> dict:
    """Compare two datasets — returns new, changed, removed invoices + vendor changes."""

    def key(r: dict) -> str:
        vendor = str(r.get('vendor_number') or '').strip().lower()
        inv    = str(r.get('physical_invoice_no') or r.get('system_invoice_no') or '').strip().lower()
        return f"{vendor}|{inv}"

    old_map = {key(r): r for r in old_rows if key(r) != '|'}
    new_map = {key(r): r for r in new_rows if key(r) != '|'}

    old_keys = set(old_map)
    new_keys = set(new_map)

    added   = new_keys - old_keys
    removed = old_keys - new_keys
    common  = old_keys & new_keys

    # Changed = same key but amount or date differs
    changed = []
    for k in common:
        o, n = old_map[k], new_map[k]
        diffs = []
        for field in ('amount_excl_lc', 'date_authorized', 'scheduled_pay_date', 'authorized_by'):
            ov, nv = str(o.get(field) or ''), str(n.get(field) or '')
            if ov != nv:
                diffs.append({"field": field, "old": ov, "new": nv})
        if diffs:
            changed.append({"key": k, "diffs": diffs, "row": n})

    # Vendor-level amount comparison
    def vendor_totals(rows):
        totals: dict[str, float] = {}
        for r in rows:
            v = str(r.get('vendor_number') or r.get('vendor_name') or '').strip()
            try:
                amt = float(str(r.get('amount_excl_lc') or 0).replace(',', ''))
            except Exception:
                amt = 0.0
            totals[v] = totals.get(v, 0.0) + amt
        return totals

    old_totals = vendor_totals(old_rows)
    new_totals = vendor_totals(new_rows)
    all_vendors = set(old_totals) | set(new_totals)

    vendor_changes = []
    for v in all_vendors:
        o = old_totals.get(v, 0.0)
        n = new_totals.get(v, 0.0)
        if o == 0 and n == 0:
            continue
        pct = ((n - o) / o * 100) if o else 100.0
        if abs(pct) >= 20 or (o == 0 and n > 0) or (n == 0 and o > 0):
            vendor_changes.append({
                "vendor":    v,
                "old_total": round(o, 2),
                "new_total": round(n, 2),
                "change_pct": round(pct, 1),
                "direction": "new" if o == 0 else ("removed" if n == 0 else ("up" if pct > 0 else "down")),
            })
    vendor_changes.sort(key=lambda x: abs(x["change_pct"]), reverse=True)

    def sample(keys, src_map, n=50):
        return [src_map[k] for k in list(keys)[:n]]

    return {
        "added_count":   len(added),
        "removed_count": len(removed),
        "changed_count": len(changed),
        "vendor_changes": vendor_changes[:30],
        "added_sample":   sample(added, new_map),
        "removed_sample": sample(removed, old_map),
        "changed_sample": changed[:50],
        "old_total_rows": len(old_rows),
        "new_total_rows": len(new_rows),
    }
