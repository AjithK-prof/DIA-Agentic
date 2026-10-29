import json
import os
import re
import ast
from datetime import datetime, date

# ════════════════════════════════════════════════════════════════════════════════
#  LLM PROVIDER TOGGLE
#  Active: GROQ
#  To switch to OpenRouter: comment the Groq block, uncomment the OpenRouter block
# ════════════════════════════════════════════════════════════════════════════════

# ── GROQ (commented — replaced by llm_provider) ──────────────────────────────
# from groq import Groq
# GROQ_MODEL = "qwen/qwen3.8-27b"

# ── LLM PROVIDER (Foundry Local or Groq, chosen via .env) ────────────────────
from dia_agentic.core.llm_provider import get_client as _get_llm_client, get_cloudflare_code_client as _get_code_client, is_foundry_active

# ── OPENROUTER (commented) ───────────────────────────────────────────────────────
# from openai import OpenAI
# OR_MODEL          = "nvidia/nemotron-3-ultra-550b-a55b:free"
# OR_MODEL_FALLBACK = "google/gemma-4-31b-it:free"

# ════════════════════════════════════════════════════════════════════════════════

UDM_FIELD_DESCRIPTIONS = """
vendor_number      : Vendor identifier code (string)
vendor_name        : Vendor full name (string)
company_code       : Company or entity code (string)
physical_invoice_no: Physical invoice number from the vendor (string)
system_invoice_no  : System-assigned invoice number (string)
original_invoice_no: Original invoice reference number (string)
date_documented    : Date the invoice was created / entered (DD-MM-YYYY string)
date_captured      : Date the invoice was captured in the ERP system (DD-MM-YYYY string)
date_authorized    : Date the invoice was approved / authorized (DD-MM-YYYY string)
scheduled_pay_date : Planned payment date — NOT the actual payment date (DD-MM-YYYY string)
payment_date       : Actual date the payment was executed / cleared (DD-MM-YYYY string)
actual_pay_date    : Alternative actual payment date field — some ERP exports use this instead of payment_date
amount_excl_lc     : Invoice amount EXCLUDING tax, in local currency (numeric string)
amount_incl_lc     : Invoice amount INCLUDING tax, in local currency (numeric string)
amount_vat_lc      : VAT / tax amount in local currency (numeric string)
reference_currency : Currency code e.g. USD EUR ZAR (string)
invoice_description: Free-text description / narration on the invoice (string)
invoice_type       : Type — standard, credit, debit memo, etc. (string)
captured_by        : Person who entered the invoice in the system (string)
authorized_by      : Person who approved / authorized the invoice (string)
payment_type       : Method of payment (string)
fiscal_year        : Financial year (string)
block_group        : Grouping or blocking classification (string)
vendor_country     : Vendor ISO country code (string)
po_non_po          : Whether the invoice is against a PO — PO or Non-PO (string)
"""

# ── Critical rules injected as concrete anti-pattern examples ───────────────────
SYSTEM_PROMPT = """You are a senior P2P (Procure-to-Pay) fraud analyst and Python developer.
You write *accurate*, *executable* Python functions that detect invoice fraud and control failures.

=== INVOICE DATA MODEL ===
Each row is a Python dict. All values are strings or None.
{field_descriptions}

=== ACTUAL DATASET SAMPLE ===
{sample_rows}

=== FUNCTION REQUIREMENTS ===
def run_test(rows):
    ...
    return hits  # list[int] of 0-based row indices that match the pattern

Rules:
1. Signature must be exactly: def run_test(rows):
2. Return a list[int] of 0-based indices. Never return None.
3. Handle None / missing: use row.get('field') or '', never assume a field exists.
4. Parse amounts: float(str(val or 0).replace(',','').strip()) inside try/except.
5. Date helpers are supplied by the test runner: parse_invoice_date(value)
   returns a date or None for blank, invalid, and 1900 placeholders.
   actual_payment_date(row) parses payment_date first and falls back to
   actual_pay_date when the first is invalid. DO NOT define, import,
   or override these helpers yourself.
6. Imports (datetime, collections, re, etc.) must go INSIDE the function body.
7. Avoid O(n²) — use dicts/sets for grouping.
8. Sentinel values to skip: '', None, 'none', 'null', 'nan', 'n/a', '01-01-1900', '1900-01-01'.

=== DATE FIELD CONTRACT — VERY IMPORTANT ===
- date_documented    : when the invoice was ISSUED / CREATED
- date_captured      : when it was ENTERED into the ERP
- date_authorized    : when it was APPROVED
- scheduled_pay_date : the PLANNED payment date (may not match when it was actually paid)
- payment_date       : the ACTUAL payment execution date (preferred field)
- actual_pay_date    : ALTERNATIVE actual payment date — some ERP exports use this INSTEAD OF payment_date

PAYMENT-TIMING TESTS must call the supplied helpers directly:
  pay_date = actual_payment_date(row)
  invoice_date = parse_invoice_date(row.get('date_documented'))
  if pay_date is None or invoice_date is None:
      continue   # skip this test for this row, not the other tests

If you need "payment before invoice", the condition is:
  pay_date < invoice_date
Same-day payment must NEVER be flagged — use strict <, not <=.
Never use 'scheduled_pay_date' as a payment date proxy.

=== SEGREGATION-OF-DUTIES TESTS ===
A SoD violation requires the SAME non-blank person in two incompatible roles.
Correct:
  c = str(row.get('captured_by') or '').strip().lower()
  a = str(row.get('authorized_by') or '').strip().lower()
  if c and a and c == a:   # same person — flag
Wrong (do NOT do this):
  if captured_by == authorized_by:   # fails if either is None

=== DUPLICATE / NEAR-DUPLICATE TESTS ===
- Always strip() and lower() string keys before grouping.
- Round amounts to 2 decimal places before comparing.
- Flag ALL copies when a duplicate is found (extend hits with all indices in the group).
- When using a date window (e.g. 7-day), compute absolute difference in days:
  abs((d1 - d2).days) <= 7

=== AMOUNT THRESHOLD TESTS ===
- Do NOT hard-code currency-specific thresholds unless the prompt specifies a currency.
- Use a relative approach (e.g. "> 3 standard deviations from vendor mean") or
  make the threshold a clearly named constant with a comment.
- Zero and negative amounts must always be excluded before thresholding.

=== VENDOR PATTERN TESTS ===
- Use vendor_number as the primary group key (more stable than vendor_name).
- Include vendor_name in output evidence but group by vendor_number.

=== SELF-CHECK BEFORE RETURNING ===
Make sure your code:
✓ Returns a plain list, not a generator or set.
✓ Never mutates the input rows list.
✓ Would return [] on an empty input.
✓ Would return [] if all required fields are blank.

=== OUTPUT FORMAT ===
Return ONLY this JSON object — no markdown, no explanation outside it:
{{
  "test_name": "3-5 word descriptive name",
  "description": "One sentence: what exact pattern this detects and why it indicates a control failure.",
  "required_fields": ["field_name_1", "field_name_2"],
  "severity": "error or warning or info",
  "code": "def run_test(rows):\\n    ...\\n    return hits",
  "explanation": "2-3 plain-English sentences explaining the logic to a non-technical auditor, including what a legitimate vs suspicious case looks like."
}}"""


# ── Post-generation semantic validator ──────────────────────────────────────────

# Synthetic fixture rows used to sanity-check common test categories.
# Build_test checks the generated code against these rows and warns if
# the result contradicts an expected outcome.

_DATE_SENTINELS = {"none", "null", "nan", "n/a", "", "01-01-1900", "1900-01-01"}

_FIXTURE_ROWS = [
    # IDX 0 — clean invoice: must not be flagged by any basic test
    {
        "vendor_number": "V001", "vendor_name": "Acme Supplies",
        "physical_invoice_no": "INV-1001", "system_invoice_no": "SYS-1001",
        "date_documented": "10-01-2024", "date_captured": "11-01-2024",
        "date_authorized": "13-01-2024", "scheduled_pay_date": "25-01-2024",
        "payment_date": "24-01-2024", "actual_pay_date": None,
        "amount_excl_lc": "3750.50", "amount_incl_lc": "4312.50",
        "reference_currency": "USD", "invoice_description": "Office supplies - quarterly",
        "captured_by": "alice.smith", "authorized_by": "bob.jones",
        "company_code": "CC01", "invoice_type": "standard",
        "po_non_po": "PO", "fiscal_year": "2024",
    },
    # IDX 1 — DUPLICATE of row 0: same vendor + amount + invoice_no
    {
        "vendor_number": "V001", "vendor_name": "Acme Supplies",
        "physical_invoice_no": "INV-1001", "system_invoice_no": "SYS-1002",
        "date_documented": "10-01-2024", "date_captured": "12-01-2024",
        "date_authorized": "14-01-2024", "scheduled_pay_date": "25-01-2024",
        "payment_date": "25-01-2024", "actual_pay_date": None,
        "amount_excl_lc": "3750.50", "amount_incl_lc": "4312.50",
        "reference_currency": "USD", "invoice_description": "Office supplies - quarterly",
        "captured_by": "alice.smith", "authorized_by": "bob.jones",
        "company_code": "CC01", "invoice_type": "standard",
        "po_non_po": "PO", "fiscal_year": "2024",
    },
    # IDX 2 — PAYMENT BEFORE INVOICE via payment_date: 05-Jan < 10-Jan
    {
        "vendor_number": "V002", "vendor_name": "Beta Corp",
        "physical_invoice_no": "INV-2001", "system_invoice_no": "SYS-2001",
        "date_documented": "10-01-2024", "date_captured": "12-01-2024",
        "date_authorized": "13-01-2024", "scheduled_pay_date": "25-01-2024",
        "payment_date": "05-01-2024", "actual_pay_date": None,
        "amount_excl_lc": "8200.00", "amount_incl_lc": "9430.00",
        "reference_currency": "USD", "invoice_description": "Consulting services",
        "captured_by": "alice.smith", "authorized_by": "bob.jones",
        "company_code": "CC01", "invoice_type": "standard",
        "po_non_po": "Non-PO", "fiscal_year": "2024",
    },
    # IDX 3 — SAME-DAY payment: payment_date == date_documented — must NOT flag
    {
        "vendor_number": "V003", "vendor_name": "Gamma Ltd",
        "physical_invoice_no": "INV-3001", "system_invoice_no": "SYS-3001",
        "date_documented": "15-01-2024", "date_captured": "15-01-2024",
        "date_authorized": "15-01-2024", "scheduled_pay_date": "15-01-2024",
        "payment_date": "15-01-2024", "actual_pay_date": None,
        "amount_excl_lc": "5000.00", "amount_incl_lc": "5750.00",
        "reference_currency": "USD", "invoice_description": "Emergency repair",
        "captured_by": "carol.white", "authorized_by": "carol.white",
        "company_code": "CC02", "invoice_type": "standard",
        "po_non_po": "Non-PO", "fiscal_year": "2024",
    },
    # IDX 4 — PAYMENT BEFORE INVOICE via actual_pay_date only (payment_date blank)
    {
        "vendor_number": "V008", "vendor_name": "Theta Exports",
        "physical_invoice_no": "INV-8001", "system_invoice_no": "SYS-8001",
        "date_documented": "20-03-2024", "date_captured": "22-03-2024",
        "date_authorized": "23-03-2024", "scheduled_pay_date": "10-04-2024",
        "payment_date": "",                  # blank — ERP uses actual_pay_date
        "actual_pay_date": "15-03-2024",     # 5 days before invoice
        "amount_excl_lc": "6100.00", "amount_incl_lc": "7015.00",
        "reference_currency": "USD", "invoice_description": "Logistics",
        "captured_by": "greg.tan", "authorized_by": "hana.wu",
        "company_code": "CC04", "invoice_type": "standard",
        "po_non_po": "Non-PO", "fiscal_year": "2024",
    },
    # IDX 5 — SoD VIOLATION: same person captured and authorized
    {
        "vendor_number": "V004", "vendor_name": "Delta Inc",
        "physical_invoice_no": "INV-4001", "system_invoice_no": "SYS-4001",
        "date_documented": "20-01-2024", "date_captured": "21-01-2024",
        "date_authorized": "21-01-2024", "scheduled_pay_date": "05-02-2024",
        "payment_date": "04-02-2024", "actual_pay_date": None,
        "amount_excl_lc": "12500.00", "amount_incl_lc": "14375.00",
        "reference_currency": "USD", "invoice_description": "IT equipment",
        "captured_by": "dave.green", "authorized_by": "dave.green",
        "company_code": "CC01", "invoice_type": "standard",
        "po_non_po": "PO", "fiscal_year": "2024",
    },
    # IDX 6 — ROUND AMOUNT: 50,000 exactly
    {
        "vendor_number": "V005", "vendor_name": "Epsilon Services",
        "physical_invoice_no": "INV-5001", "system_invoice_no": "SYS-5001",
        "date_documented": "22-01-2024", "date_captured": "23-01-2024",
        "date_authorized": "25-01-2024", "scheduled_pay_date": "10-02-2024",
        "payment_date": "09-02-2024", "actual_pay_date": None,
        "amount_excl_lc": "50000.00", "amount_incl_lc": "57500.00",
        "reference_currency": "USD", "invoice_description": "Marketing campaign",
        "captured_by": "eve.brown", "authorized_by": "frank.black",
        "company_code": "CC03", "invoice_type": "standard",
        "po_non_po": "Non-PO", "fiscal_year": "2024",
    },
    # IDX 7 — WEEKEND AUTHORIZATION: date_authorized = Saturday 27-Jan-2024
    {
        "vendor_number": "V006", "vendor_name": "Zeta Partners",
        "physical_invoice_no": "INV-6001", "system_invoice_no": "SYS-6001",
        "date_documented": "25-01-2024", "date_captured": "26-01-2024",
        "date_authorized": "27-01-2024",   # Saturday
        "scheduled_pay_date": "15-02-2024",
        "payment_date": "14-02-2024", "actual_pay_date": None,
        "amount_excl_lc": "7800.00", "amount_incl_lc": "8970.00",
        "reference_currency": "USD", "invoice_description": "Legal services",
        "captured_by": "alice.smith", "authorized_by": "bob.jones",
        "company_code": "CC01", "invoice_type": "standard",
        "po_non_po": "Non-PO", "fiscal_year": "2024",
    },
    # IDX 8 — BOTH payment fields blank: must NOT be flagged for payment-before-invoice
    {
        "vendor_number": "V007", "vendor_name": "Eta Logistics",
        "physical_invoice_no": "INV-7001", "system_invoice_no": "SYS-7001",
        "date_documented": "28-01-2024", "date_captured": "29-01-2024",
        "date_authorized": "30-01-2024", "scheduled_pay_date": "15-02-2024",
        "payment_date": "", "actual_pay_date": None,    # both blank — must skip
        "amount_excl_lc": "3100.00", "amount_incl_lc": "3565.00",
        "reference_currency": "USD", "invoice_description": "Freight charges",
        "captured_by": "alice.smith", "authorized_by": "bob.jones",
        "company_code": "CC01", "invoice_type": "standard",
        "po_non_po": "PO", "fiscal_year": "2024",
    },
]

# Prompt-pattern → expected fixture outcomes.
# Each entry: (keyword_set, must_include_indices, must_exclude_indices, note)
_SEMANTIC_RULES: list[tuple] = [
    (
        {"payment", "before", "invoice", "date"},
        [2, 4],    # idx 2: via payment_date. idx 4: via actual_pay_date. MUST flag
        [3, 8],    # idx 3: same-day. idx 8: both payment fields blank. MUST NOT flag
        "payment-before-invoice",
    ),
    (
        {"duplicate", "same", "vendor", "amount"},
        [0, 1],    # idx 0 and 1 are duplicates — both MUST flag
        [],
        "duplicate-invoice",
    ),
    (
        {"segregation", "duties", "sod", "capture", "authorize",
         "captured", "authorized", "same", "person"},
        [3, 5],    # idx 3: carol.white==carol.white. idx 5: dave.green==dave.green. MUST flag
        [0],       # idx 0: alice != bob. MUST NOT flag
        "segregation-of-duties",
    ),
    (
        {"round", "amount", "number"},
        [6],       # idx 6: 50,000 exactly — MUST flag
        [0],       # idx 0: 3,750.50 — MUST NOT flag
        "round-amount",
    ),
    (
        {"weekend", "authorization", "authorized", "approval", "approved"},
        [7],       # idx 7: authorized Saturday 27-Jan-2024 — MUST flag
        [0],       # idx 0: authorized Mon 13-Jan — MUST NOT flag
        "weekend-authorization",
    ),
]


def _classify_prompt(prompt: str) -> list[tuple]:
    """Return all semantic rules whose keyword sets overlap the prompt."""
    words = set(re.sub(r"[^a-z0-9 ]", " ", prompt.lower()).split())
    matched = []
    for kws, must, must_not, label in _SEMANTIC_RULES:
        if len(kws & words) >= 2:
            matched.append((must, must_not, label))
    return matched


def _validate_generated_code(code: str, prompt: str) -> list[str]:
    """
    Smoke-test the generated code against fixture rows.
    Returns a list of warning strings (empty = passed).
    Warnings do NOT block saving — they are included in the result so the UI
    can surface them to the reviewer.
    """
    warnings: list[str] = []
    rules = _classify_prompt(prompt)
    if not rules:
        return []  # no applicable semantic check

    try:
        from dia_agentic.core.date_utils import actual_payment_date, parse_invoice_date
        from dia_agentic.core.test_executor import SAFE_BUILTINS
        namespace = {**SAFE_BUILTINS, "actual_payment_date": actual_payment_date,
                     "parse_invoice_date": parse_invoice_date}
        exec(compile(code, "<validator>", "exec"), namespace)
        fn = namespace.get("run_test")
        if not callable(fn):
            return ["Generated function not callable — skipping semantic check"]
        hits: list[int] = fn(_FIXTURE_ROWS)
        hit_set = set(hits)
    except Exception as exc:
        return [f"Semantic validator execution error: {exc}"]

    for must_include, must_exclude, label in rules:
        for idx in must_include:
            if idx not in hit_set:
                warnings.append(
                    f"[{label}] Expected fixture row {idx} to be flagged but it was not. "
                    "The test may be using the wrong date field or incorrect comparison."
                )
        for idx in must_exclude:
            if idx in hit_set:
                warnings.append(
                    f"[{label}] Fixture row {idx} should NOT be flagged but was. "
                    "Check for same-day / missing-field false positives."
                )
    return warnings


def _is_payment_timing_prompt(prompt: str) -> bool:
    words = set(re.findall(r"[a-z]+", prompt.lower()))
    return bool(words & {"payment", "payments", "paid", "pay"}) and bool(
        words & {"before", "after", "earlier", "later", "same", "timing", "premature"}
    )


def payment_timing_errors(code: str, prompt: str) -> list[str]:
    """Reject unsafe generated timing tests, including previously saved code.

    This runs at generation AND execution. Validation uses synthetic rows only;
    it does not rewrite code or change any stored audit result.
    """
    if not _is_payment_timing_prompt(prompt):
        return []
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"Invalid Python: {exc.msg}"]
    helper_names = {"actual_payment_date", "parse_invoice_date"}
    called = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    errors = []
    missing = helper_names - called
    if missing:
        errors.append("Use the shared " + ", ".join(sorted(missing)) + " helper(s)")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in helper_names:
            errors.append("Do not redefine the shared date helpers")
            break
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id in helper_names
                   for target in targets):
                errors.append("Do not replace the shared date helpers")
                break
    if errors:
        return errors

    from dia_agentic.core.date_utils import actual_payment_date, parse_invoice_date
    from dia_agentic.core.test_executor import SAFE_BUILTINS

    rows = [dict(row) for row in _FIXTURE_ROWS]
    fallback_idx = len(rows)
    rows.append({**_FIXTURE_ROWS[2], "payment_date": "01-01-1900",
                 "actual_pay_date": "05-01-2024"})
    missing_payment_idx = len(rows)
    rows.append({**_FIXTURE_ROWS[2], "payment_date": "1900-01-01",
                 "actual_pay_date": "01/01/1900", "scheduled_pay_date": "05-01-2024"})
    missing_invoice_idx = len(rows)
    rows.append({**_FIXTURE_ROWS[2], "date_documented": "01-01-1900"})
    missing_capture_idx = len(rows)
    rows.append({**_FIXTURE_ROWS[3], "date_captured": "01-01-1900"})
    try:
        namespace = {**SAFE_BUILTINS, "actual_payment_date": actual_payment_date,
                     "parse_invoice_date": parse_invoice_date}
        exec(compile(tree, "<payment-validator>", "exec"), namespace)
        hits = namespace["run_test"](rows)
        if not isinstance(hits, list) or any(
            type(idx) is not int or idx < 0 or idx >= len(rows) for idx in hits
        ):
            return ["run_test must return valid 0-based row indices"]
        hit_set = set(hits)
    except Exception as exc:
        return [f"Payment-date fixture execution failed: {type(exc).__name__}"]
    if missing_payment_idx in hit_set:
        errors.append("Missing or placeholder actual payment dates must not produce hits")
    words = set(re.findall(r"[a-z]+", prompt.lower()))
    if "before" in words or "premature" in words:
        if missing_invoice_idx in hit_set:
            errors.append("Missing invoice dates must not produce payment-before-invoice hits")
        if not {2, 4, fallback_idx} <= hit_set or {3, 8} & hit_set:
            errors.append("Payment-before-invoice must flag real early payments, "
                          "including valid fallback dates, but not same-day or missing dates")
    if "same" in words and "capture" in words and missing_capture_idx in hit_set:
        errors.append("Missing capture dates must not produce same-day payment hits")
    return errors


# ── Input guard: reject nonsense prompts before calling the API ─────────────────

P2P_KEYWORDS = {
    "invoice", "invoices", "vendor", "vendors", "payment", "payments", "pay",
    "amount", "amounts", "duplicate", "duplicates", "approval", "approvals",
    "authorized", "authorize", "capture", "captured", "date", "dates",
    "round", "weekend", "weekend", "holiday", "segregation", "duties",
    "sod", "po", "purchase", "order", "supplier", "suppliers",
    "fraud", "anomaly", "control", "controls", "audit", "accounting",
    "finance", "financial", "erp", "accounts", "payable", "ap",
    "budget", "cost", "split", "threshold", "limit", "over", "under",
    "same", "person", "user", "currency", "vat", "tax", "description",
    "keyword", "keywords", "suspicious", "unusual", "outlier",
    "near", "similar", "match", "matching", "window", "days", "weeks",
    "month", "year", "fiscal", "quarter",
}

_GARBAGE_PATTERN = re.compile(r"^[^a-zA-Z\s]{4,}$|(.)\1{4,}")


def _is_meaningful_prompt(prompt: str) -> bool:
    """Return False for prompts that are clearly not P2P-related or are gibberish."""
    cleaned = re.sub(r"[^a-z0-9 ]", " ", prompt.lower())
    words = [w for w in cleaned.split() if len(w) >= 3]
    if len(words) < 3:
        return False
    if _GARBAGE_PATTERN.search(prompt):
        return False
    return bool(P2P_KEYWORDS & set(words))


# ── GROQ key helper ──────────────────────────────────────────────────────────────
def _get_api_keys() -> list[str]:
    keys = []
    for k in ("GROQ_API_KEY", "GROQ_API_KEY_2"):
        v = os.getenv(k, "").strip()
        if v:
            keys.append(v)
    return keys


def build_test(prompt: str, sample_rows: list[dict]) -> dict:
    """
    Generate an executable P2P audit test from a natural-language prompt.

    Returns a dict with:
      test_name, description, required_fields, severity, code, explanation,
      type="generated", weightage, validation_warnings (list).

    Returns {"error": "..."} on failure.
    """
    # ── 1. Guard: reject nonsense ────────────────────────────────────────────────
    if not _is_meaningful_prompt(prompt):
        return {
            "error": (
                "Please describe an invoice, vendor, payment, approval, date, amount, "
                "or duplicate-risk pattern you want to test. "
                "Examples: 'Find invoices paid before the invoice date' or "
                "'Detect same-day capture and authorization'."
            )
        }

    # ── 2. Get LLM client — use code-optimised model for test generation ────────
    try:
        _llm_client, _llm_model = _get_code_client()
    except RuntimeError as e:
        return {"error": str(e)}

    # ── GROQ direct client (commented — now handled by llm_provider) ─────────
    # api_keys = _get_api_keys()
    # if not api_keys:
    #     return {"error": "GROQ_API_KEY not set in .env — get a free key at console.groq.com/keys"}

    # ── 3. Prepare context ───────────────────────────────────────────────────────
    sample_str = json.dumps(sample_rows[:5], indent=2, default=str)
    system = SYSTEM_PROMPT.format(
        field_descriptions=UDM_FIELD_DESCRIPTIONS,
        sample_rows=sample_str,
    )
    user_message = f'Write a run_test function that detects: "{prompt}"'

    # ── 4. Call LLM with one retry ───────────────────────────────────────────────
    # ── GROQ key-rotation loop (commented — llm_provider handles provider selection)
    # for api_key in api_keys:
    #     client = Groq(api_key=api_key)
    last_error = ""
    for attempt in range(2):
        try:
            resp = _llm_client.chat.completions.create(
                model=_llm_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_message},
                ],
                temperature=0.15,   # lower = more deterministic code
                max_tokens=3500,
            )
            if not resp.choices:
                last_error = f"Model {_llm_model} returned no choices"
                break

            raw = (resp.choices[0].message.content or "").strip()

            # strip reasoning-model <think> blocks
            raw = re.sub(r"<think>[\s\S]*?</think>", "", raw).strip()

            # strip markdown fences
            if "```" in raw:
                parts = raw.split("```")
                for p in parts:
                    p = p.strip()
                    if p.startswith("json"):
                        p = p[4:]
                    if p.strip().startswith("{"):
                        raw = p.strip()
                        break

            # extract first JSON object even if surrounded by stray text
            m = re.search(r"\{[\s\S]*\}", raw)
            raw = m.group(0) if m else raw

            result = json.loads(raw)

            if not isinstance(result, dict):
                raise ValueError(f"Model returned {type(result).__name__} instead of a JSON object")

            for key in ("test_name", "description", "code", "explanation", "severity"):
                if key not in result:
                    raise ValueError(f"Missing required key in response: {key}")

            # ── 5. Compile check (syntax) ────────────────────────────────────
            compile(result["code"], "<llm>", "exec")

            errors = payment_timing_errors(result["code"], prompt)
            if errors:
                if attempt == 0:
                    user_message += (
                        "\n\nYour prior code failed mandatory payment-date checks: "
                        + "; ".join(errors)
                        + ". Use the supplied date helpers and return corrected JSON."
                    )
                    continue
                return {"error": "Generated payment-date test failed validation: "
                        + "; ".join(errors) + ". No test was accepted."}

            # ── 6. Semantic smoke-test ───────────────────────────────────────
            validation_warnings = _validate_generated_code(result["code"], prompt)

            # ── 7. Enrich result ─────────────────────────────────────────────
            result["type"] = "generated"
            result["prompt"] = prompt
            result.setdefault("weightage", 7)
            result.pop("red_flag", None)
            result.setdefault("required_fields", [])
            result["validation_warnings"] = validation_warnings

            return result

        except json.JSONDecodeError:
            if attempt == 0:
                user_message += (
                    "\n\nIMPORTANT: Your previous response was not valid JSON. "
                    "Return ONLY the JSON object — absolutely nothing else, "
                    "no markdown code fences, no commentary."
                )
                continue
            last_error = "Model did not return valid JSON after retry"
            break

        except SyntaxError as e:
            if attempt == 0:
                user_message += (
                    f"\n\nIMPORTANT: The code you generated has a Python syntax error: {e}. "
                    "Fix the syntax and return the corrected JSON."
                )
                continue
            return {"error": f"Generated code has a syntax error: {e}. Try rephrasing your prompt."}

        except Exception as e:
            last_error = str(e)
            break

    # ── 8. Error mapping ─────────────────────────────────────────────────────────
    if "401" in last_error or "invalid_api_key" in last_error.lower():
        return {
            "error": "API key is invalid or expired. "
                     "Update GROQ_API_KEY in .env — get a new key at console.groq.com/keys"
        }

    return {"error": f"Test generation failed: {last_error or 'unknown error'}"}
