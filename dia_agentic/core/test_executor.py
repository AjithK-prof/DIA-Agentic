SAFE_BUILTINS = {
    '__builtins__': {
        'len': len, 'range': range, 'enumerate': enumerate,
        'zip': zip, 'list': list, 'dict': dict, 'set': set,
        'tuple': tuple, 'str': str, 'int': int, 'float': float,
        'bool': bool, 'abs': abs, 'round': round,
        'min': min, 'max': max, 'sum': sum,
        'sorted': sorted, 'reversed': reversed,
        'isinstance': isinstance, 'type': type,
        'any': any, 'all': all, 'filter': filter, 'map': map,
        'print': print, 'repr': repr,
        'ValueError': ValueError, 'TypeError': TypeError,
        'KeyError': KeyError, 'IndexError': IndexError,
        'AttributeError': AttributeError, 'StopIteration': StopIteration,
        '__import__': __import__,
        'None': None, 'True': True, 'False': False,
    }
}


from dia_agentic.core.evidence import evidence_for, parse_date
from dia_agentic.core.date_utils import actual_payment_date, parse_invoice_date


def _parse_amount(val) -> float:
    try:
        return abs(float(str(val or 0).replace(',', '').strip()))
    except (ValueError, TypeError):
        return 0.0


def execute_test(test: dict, rows: list[dict]) -> dict:
    code = test.get("code", "")
    test_name = test.get("test_name", "Unknown Test")

    if not rows:
        return _empty_result(test, "No rows to test")

    skipped_missing_dates = 0
    if test.get("id") == "same_day_actual_payment":
        skipped_missing_dates = sum(
            1 for row in rows
            if not parse_date(row.get("date_captured"))
            or not (parse_date(row.get("payment_date"))
                    or parse_date(row.get("actual_pay_date")))
        )
        if skipped_missing_dates == len(rows):
            result = _empty_result(test, "No rows have both a valid capture and actual payment date")
            result["skipped_missing_dates"] = skipped_missing_dates
            return result

    if test.get("type") == "generated":
        from dia_agentic.core.test_builder import payment_timing_errors
        errors = payment_timing_errors(code, test.get("prompt") or test_name)
        if errors:
            return _empty_result(
                test, "Payment-date test needs regeneration: " + "; ".join(errors)
            )

    try:
        namespace = {**SAFE_BUILTINS,
                     "parse_invoice_date": parse_invoice_date,
                     "actual_payment_date": actual_payment_date}
        compile(code, "<test>", "exec")
        exec(code, namespace)

        if 'run_test' not in namespace:
            return _empty_result(test, "Generated code missing run_test function")

        hit_indices = namespace['run_test'](rows)

        if not isinstance(hit_indices, list):
            return _empty_result(test, "run_test must return a list")

        hit_indices = [i for i in hit_indices if isinstance(i, int) and 0 <= i < len(rows)]

    except Exception as e:
        return _empty_result(test, f"Execution error: {e}")

    total = len(rows)
    hits = len(hit_indices)
    hit_amount = sum(_parse_amount(rows[i].get('amount_excl_lc')) for i in hit_indices)

    test_id = test.get("id", test_name.lower().replace(" ", "_"))
    sample = []
    for i in sorted(set(hit_indices))[:200]:
        r = rows[i]
        inv_no = r.get('physical_invoice_no') or r.get('system_invoice_no') or '—'
        vendor = r.get('vendor_number') or ''
        inv_id = f"{vendor} / {inv_no}" if vendor and inv_no != '—' else (inv_no if inv_no != '—' else f"Row {i+2}")
        sample.append({
            "row": i + 2,
            "inv_id": inv_id,
            "vendor_number": r.get('vendor_number'),
            "vendor_name": r.get('vendor_name'),
            "physical_invoice_no": inv_no,
            # All date fields — frontend uses these for timeline and evidence display
            "date_documented":   r.get('date_documented'),
            "date_captured":     r.get('date_captured'),
            "date_authorized":   r.get('date_authorized') or r.get('date_documented'),
            "scheduled_pay_date": r.get('scheduled_pay_date'),
            "payment_date":      r.get('payment_date'),       # actual payment (preferred)
            "actual_pay_date":   r.get('actual_pay_date'),    # actual payment (alternate field — same meaning)
            "amount_excl_lc": r.get('amount_excl_lc'),
            "amount_incl_lc": r.get('amount_incl_lc'),
            "invoice_description": str(r.get('invoice_description') or '')[:120],
            "reference_currency": r.get('reference_currency'),
            "captured_by":   r.get('captured_by'),
            "authorized_by": r.get('authorized_by'),
            "company_code":  r.get('company_code'),
            "fiscal_year":   r.get('fiscal_year'),
            "payment_type":  r.get('payment_type'),
            "evidence": evidence_for(test_id, r),
        })

    return {
        "test_id": test.get("id", test_name.lower().replace(" ", "_")),
        "test_name": test_name,
        "type": test.get("type", "generated"),
        "applicable": True,
        "hit_count": hits,
        "hit_pct": round(hits / total * 100, 1) if total else 0.0,
        "hit_amount": round(hit_amount, 2),
        "total_rows": total,
        "severity": test.get("severity", "warning"),
        "weightage": test.get("weightage", 7),
        "all_hit_indices": sorted(set(hit_indices)),
        "sample_hits": sample,
        "explanation": test.get("explanation", ""),
        "description": test.get("description", ""),
        "code": code,
        "prompt": test.get("prompt", ""),
        "required_fields": test.get("required_fields", []),
        "skipped_missing_dates": skipped_missing_dates,
    }


def _empty_result(test: dict, reason: str) -> dict:
    test_name = test.get("test_name", "Unknown Test")
    return {
        "test_id": test.get("id", test_name.lower().replace(" ", "_")),
        "test_name": test_name,
        "type": test.get("type", "preset"),
        "applicable": False,
        "not_applicable_reason": reason,
        "hit_count": 0, "hit_pct": 0, "hit_amount": 0,
        "sample_hits": [], "all_hit_indices": [],
        "severity": test.get("severity", "warning"),
        "weightage": test.get("weightage", 7),
        "explanation": test.get("explanation", ""),
        "description": test.get("description", ""),
        "required_fields": test.get("required_fields", []),
    }
