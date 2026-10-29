"""Trusted date parsing for evidence, presets and generated audit tests."""
from datetime import datetime

DATE_FORMATS = ("%d-%m-%Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y%m%d")
SENTINELS = {"none", "null", "nan", "n/a", "", "01-01-1900", "1900-01-01", "01/01/1900"}


def parse_invoice_date(value):
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in SENTINELS:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def actual_payment_date(row: dict):
    """Choose a *valid* executed-payment date, never a planned date or sentinel."""
    return (parse_invoice_date(row.get("payment_date"))
            or parse_invoice_date(row.get("actual_pay_date")))
