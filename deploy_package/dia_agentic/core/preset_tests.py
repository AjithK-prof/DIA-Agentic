PRESET_TESTS = [
    {
        "id": "round_dollar",
        "test_name": "Round Dollar Invoices",
        "type": "preset",
        "prompt": "Find all invoices where the amount is a suspiciously round number — divisible by 1000, 5000, 10000, 50000, or 100000.",
        "severity": "warning",
        "weightage": 8,
        "required_fields": ["amount_excl_lc"],
        "explanation": "Flags invoices with round amounts (divisible by 1,000+). Fraudulent invoices are often filed in exact round numbers to avoid scrutiny. Legitimate invoices typically have cents and specific amounts.",
        "code": """def run_test(rows):
    THRESHOLDS = [1000000, 500000, 100000, 50000, 10000, 5000, 1000]
    hits = []
    for i, row in enumerate(rows):
        try:
            amt = abs(float(str(row.get('amount_excl_lc') or 0).replace(',', '')))
            if amt == 0:
                continue
            for t in THRESHOLDS:
                if amt % t == 0:
                    hits.append(i)
                    break
        except (ValueError, TypeError):
            pass
    return hits
""",
    },
    {
        "id": "duplicate_invoice",
        "test_name": "Duplicate Invoice Detection",
        "type": "preset",
        "prompt": "Find invoices that appear to be duplicates — same vendor, same amount, and same invoice number submitted more than once.",
        "severity": "error",
        "weightage": 10,
        "required_fields": ["vendor_number", "amount_excl_lc"],
        "explanation": "Groups invoices by vendor + amount + invoice number. Any combination appearing more than once is a potential duplicate payment — one of the most common P2P frauds.",
        "code": """def run_test(rows):
    from collections import defaultdict
    seen = defaultdict(list)
    for i, row in enumerate(rows):
        vendor = str(row.get('vendor_number') or '').strip().lower()
        inv_no = str(row.get('physical_invoice_no') or row.get('system_invoice_no') or '').strip().lower()
        try:
            amt = round(abs(float(str(row.get('amount_excl_lc') or 0).replace(',', ''))), 2)
        except (ValueError, TypeError):
            amt = 0
        if vendor and amt > 0:
            key = (vendor, amt, inv_no)
            seen[key].append(i)
    hits = []
    for indices in seen.values():
        if len(indices) > 1:
            hits.extend(indices)
    return hits
""",
    },
    {
        "id": "keyword_suspicious",
        "test_name": "Suspicious Description Keywords",
        "type": "preset",
        "prompt": "Find invoices whose description contains suspicious words commonly associated with fraud or policy violations.",
        "severity": "warning",
        "weightage": 9,
        "required_fields": ["invoice_description"],
        "explanation": "Scans invoice descriptions for keywords commonly associated with fraudulent or non-compliant spend: personal expenses, gifts, cash advances, entertainment. Even one match warrants a closer look.",
        "code": """def run_test(rows):
    KEYWORDS = [
        'bonus', 'gift', 'personal', 'cash', 'loan', 'advance',
        'entertainment', 'alcohol', 'drinks', 'travel', 'holiday',
        'vacation', 'reward', 'incentive', 'tip', 'gratuity',
        'donation', 'contribution', 'sponsorship', 'refund',
        'reimbursement', 'settlement', 'legal', 'penalty',
        'fine', 'commission', 'kickback', 'rebate', 'courtesy'
    ]
    hits = []
    for i, row in enumerate(rows):
        desc = str(row.get('invoice_description') or '').lower()
        if not desc or desc in ('none', 'null', 'nan', 'n/a', ''):
            continue
        for kw in KEYWORDS:
            if kw in desc:
                hits.append(i)
                break
    return hits
""",
    },
    {
        "id": "same_day_actual_payment",
        "test_name": "Same Day Capture and Actual Payment",
        "type": "preset",
        "prompt": "Find invoices captured on the same date payment was actually executed. Use payment_date or actual_pay_date, never scheduled_pay_date. Skip rows with missing or 1900 placeholder dates.",
        "severity": "error",
        "weightage": 9,
        "required_fields": ["date_captured"],
        "explanation": "Invoice capture and actual payment happened on the same date. Review authorization and payment records to determine whether controls were bypassed; matching dates alone are not proof of fraud. Rows without a valid capture or actual payment date are skipped.",
        "code": """def run_test(rows):
    hits = []
    for i, row in enumerate(rows):
        captured = parse_invoice_date(row.get('date_captured'))
        actual = actual_payment_date(row)
        if captured and actual and captured == actual:
            try:
                amt = float(str(row.get('amount_excl_lc') or 0).replace(',',''))
                if amt != 0:
                    hits.append(i)
            except (ValueError, TypeError):
                hits.append(i)
    return hits
""",
    },
]
