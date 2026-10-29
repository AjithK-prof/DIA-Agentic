"""Conservative, read-only payment evidence matching for an invoice run.

No payment record changes invoice fields, test hits, scores, or decisions.
"""
import csv
from collections import Counter, defaultdict

from .date_utils import parse_invoice_date

REQUIRED_COLUMNS = {
    "CompanyCode", "SystemInvoiceNo", "VendorNumber", "FiscalYear",
    "SystemPaymentNo", "PaymentStatus", "DocumentType", "DatePay",
}
MAX_PAYMENT_ROWS = 100_000


def _text(value, limit=100):
    val = str(value or "").strip()
    return "" if val.lower() in ("null", "none", "nan") else val[:limit]


def _vendor(value):
    val = _text(value, 40)
    return val.lstrip("0") or "0" if val.isdigit() else val.casefold()


def _key(row):
    key = (
        _text(row.get("CompanyCode"), 40).casefold(),
        _text(row.get("SystemInvoiceNo"), 40).casefold(),
        _vendor(row.get("VendorNumber")),
        _text(row.get("FiscalYear"), 20),
    )
    return key if all(key) else None


def _date(value):
    parsed = parse_invoice_date(value)
    return parsed.isoformat() if parsed else None


def _evidence(row, source_row):
    # Explicit allowlist. Never retain account/IBAN, address, payee, descriptions,
    # model scores, or labels from an external payment export.
    return {
        "source_row": source_row,
        "system_payment_no": _text(row.get("SystemPaymentNo"), 40),
        "physical_payment_no": _text(row.get("PhysicalPaymentNo"), 40),
        "document_type": _text(row.get("DocumentType"), 80),
        "payment_status": _text(row.get("PaymentStatus"), 80),
        "date_pay": _date(row.get("DatePay")),
        "date_cleared": _date(row.get("DateCleared")),
        "date_approved": _date(row.get("DateApproved")),
        "amount_incl_lc": _text(row.get("AmountIncl_LC"), 40),
        "currency": _text(row.get("CurrencyCode"), 20),
        "payment_method": _text(row.get("PaymentMethod"), 60),
    }


def match_payments(invoice_rows: list[dict], csv_file):
    """Return (summary, records, candidate links), rejecting malformed uploads.

    A candidate belongs to an invoice only when all four normalized keys match.
    Multiple invoice rows with that key remain ambiguous, even if their amounts
    happen to be equal. Multiple payment records can point to one invoice row.
    """
    reader = csv.DictReader(csv_file)
    if not reader.fieldnames or not REQUIRED_COLUMNS <= set(reader.fieldnames):
        missing = sorted(REQUIRED_COLUMNS - set(reader.fieldnames or []))
        raise ValueError("Missing payment columns: " + ", ".join(missing))
    invoices = defaultdict(list)
    for idx, row in enumerate(invoice_rows):
        key = _key({
            "CompanyCode": row.get("company_code"),
            "SystemInvoiceNo": row.get("system_invoice_no"),
            "VendorNumber": row.get("vendor_number"),
            "FiscalYear": row.get("fiscal_year"),
        })
        if key:
            invoices[key].append(idx)
    counts = Counter()
    records = []
    links = []
    for source_row, row in enumerate(reader, 2):
        if source_row > MAX_PAYMENT_ROWS + 1:
            raise ValueError(f"Payment upload exceeds {MAX_PAYMENT_ROWS:,} data rows")
        candidates = invoices.get(_key(row), ())
        status = "matched" if len(candidates) == 1 else "ambiguous" if candidates else "unmatched"
        counts[status] += 1
        payment_idx = source_row - 2
        records.append((payment_idx, status, len(candidates), _evidence(row, source_row)))
        links.extend((idx, payment_idx) for idx in candidates)
    if not records:
        raise ValueError("Payment file has no data rows")
    return (
        {"total": len(records), "matched": counts["matched"],
         "ambiguous": counts["ambiguous"], "unmatched": counts["unmatched"]},
        records, links,
    )
