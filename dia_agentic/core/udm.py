"""
Canonical UDM (Unified Data Model) for Invoice domain.
This is the contract all ERP sources must map to.
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class InvoiceUDM:
    # Vendor / company identifiers
    vendor_number: Optional[str] = None
    vendor_name: Optional[str] = None
    company_code: Optional[str] = None
    erp_source: Optional[str] = None

    # Invoice identifiers
    original_invoice_no: Optional[str] = None
    physical_invoice_no: Optional[str] = None
    system_invoice_no: Optional[str] = None
    ref_no: Optional[str] = None
    invoice_line_ref: Optional[str] = None
    invoice_type: Optional[str] = None
    invoice_status: Optional[str] = None

    # Dates
    date_documented: Optional[str] = None
    date_captured: Optional[str] = None
    date_authorized: Optional[str] = None
    due_date: Optional[str] = None
    scheduled_pay_date: Optional[str] = None
    payment_date: Optional[str] = None

    # Amounts (Local Currency)
    amount_vat_lc: Optional[str] = None
    amount_incl_lc: Optional[str] = None
    amount_excl_lc: Optional[str] = None
    reference_currency: Optional[str] = None

    # Amounts (Original Currency)
    amount_vat_oc: Optional[str] = None
    amount_incl_oc: Optional[str] = None
    amount_excl_oc: Optional[str] = None

    # Payment info
    payment_type: Optional[str] = None
    payment_number: Optional[str] = None
    payment_amount: Optional[str] = None
    posting_key: Optional[str] = None
    debit_credit_indicator: Optional[str] = None

    # People
    captured_by: Optional[str] = None
    authorized_by: Optional[str] = None
    invoice_name: Optional[str] = None

    # Other
    fiscal_year: Optional[str] = None
    gl_indicator: Optional[str] = None
    concept_search: Optional[str] = None
    invoice_description: Optional[str] = None

    # Extended fields (present in full invoice exports)
    eypk: Optional[str] = None                   # internal PK used by SP for row identity
    actual_pay_date: Optional[str] = None         # actual payment date (vs scheduled)
    po_non_po: Optional[str] = None               # PO / Non-PO indicator
    vendor_country: Optional[str] = None          # vendor country code
    block_group: Optional[str] = None             # AP22: client grouping/classification field


# Canonical column name → UDM field mapping (covers common ERP variants)
COLUMN_ALIAS_MAP: dict[str, str] = {
    # VendorNumber variants
    "vendornumber": "vendor_number",
    "vendor_number": "vendor_number",
    "vendor number": "vendor_number",
    "vendor_no": "vendor_number",
    "vendorno": "vendor_number",
    "vend_num": "vendor_number",
    "vendor_id": "vendor_number",
    "vendorid": "vendor_number",
    "supplier_number": "vendor_number",
    "suppliernumber": "vendor_number",

    # VendorName variants
    "vendorname": "vendor_name",
    "vendor_name": "vendor_name",
    "vendor name": "vendor_name",
    "supplier_name": "vendor_name",
    "suppliername": "vendor_name",

    # CompanyCode variants
    "companycode": "company_code",
    "company_code": "company_code",
    "company code": "company_code",
    "comp_code": "company_code",
    "bukrs": "company_code",

    # ERP source
    "erp": "erp_source",
    "erp_source": "erp_source",
    "source_system": "erp_source",
    "sourcesystem": "erp_source",

    # Invoice numbers
    "originalinvoiceno": "original_invoice_no",
    "original_invoice_no": "original_invoice_no",
    "original invoice no": "original_invoice_no",
    "original_invoice_number": "original_invoice_no",
    "orig_inv_no": "original_invoice_no",
    "incoice": "original_invoice_no",
    "invoice no": "original_invoice_no",
    "invoice_no": "original_invoice_no",
    "invoice_number": "original_invoice_no",
    "invoicenumber": "original_invoice_no",
    "physicalinvoiceno": "physical_invoice_no",
    "physical_invoice_no": "physical_invoice_no",
    "physical invoice no": "physical_invoice_no",
    "systeminvoiceno": "system_invoice_no",
    "system_invoice_no": "system_invoice_no",
    "system invoice no": "system_invoice_no",
    "refno": "ref_no",
    "ref_no": "ref_no",
    "ref no": "ref_no",
    "reference_no": "ref_no",
    "referenceno": "ref_no",
    "invoicelineref": "invoice_line_ref",
    "invoice_line_ref": "invoice_line_ref",

    # Invoice type/status
    "invoicetype": "invoice_type",
    "invoice_type": "invoice_type",
    "invoice type": "invoice_type",
    "invoicestatus": "invoice_status",
    "invoice_status": "invoice_status",
    "invoice status": "invoice_status",

    # Dates
    "datedocumented": "date_documented",
    "date_documented": "date_documented",
    "date documented": "date_documented",
    "invoice_date": "date_documented",
    "invoicedate": "date_documented",
    "datecaptured": "date_captured",
    "date_captured": "date_captured",
    "date captured": "date_captured",
    "capture_date": "date_captured",
    "dateauthorized": "date_authorized",
    "date_authorized": "date_authorized",
    "date authorized": "date_authorized",
    "duedate": "due_date",
    "due_date": "due_date",
    "due date": "due_date",
    "due_dt": "due_date",
    "scheduledpaydate": "scheduled_pay_date",
    "scheduled_pay_date": "scheduled_pay_date",
    "scheduled pay date": "scheduled_pay_date",
    "paymentdate": "payment_date",
    "payment_date": "payment_date",
    "payment date": "payment_date",

    # Amounts LC
    "amountvat_lc": "amount_vat_lc",
    "amount_vat_lc": "amount_vat_lc",
    "amountincl_lc": "amount_incl_lc",
    "amount_incl_lc": "amount_incl_lc",
    "amount_incl": "amount_incl_lc",
    "amountexcl_lc": "amount_excl_lc",
    "amount_excl_lc": "amount_excl_lc",
    "amount_excl": "amount_excl_lc",
    "referencecurrency": "reference_currency",
    "reference_currency": "reference_currency",
    "currency": "reference_currency",
    "curr": "reference_currency",

    # Amounts OC
    "amountvat_oc": "amount_vat_oc",
    "amount_vat_oc": "amount_vat_oc",
    "amountincl_oc": "amount_incl_oc",
    "amount_incl_oc": "amount_incl_oc",
    "amountexcl_oc": "amount_excl_oc",
    "amount_excl_oc": "amount_excl_oc",

    # Payment
    "paymenttype": "payment_type",
    "payment_type": "payment_type",
    "payment type": "payment_type",
    "paymentnumber": "payment_number",
    "payment_number": "payment_number",
    "paymentamount": "payment_amount",
    "payment_amount": "payment_amount",
    "postingkey": "posting_key",
    "posting_key": "posting_key",
    "posting key": "posting_key",
    "debit/creditindicator": "debit_credit_indicator",
    "debit_credit_indicator": "debit_credit_indicator",
    "dr_cr": "debit_credit_indicator",

    # People
    "capturedby": "captured_by",
    "captured_by": "captured_by",
    "captured by": "captured_by",
    "authorizedby": "authorized_by",
    "authorized_by": "authorized_by",
    "authorized by": "authorized_by",
    "invoicename": "invoice_name",
    "invoice_name": "invoice_name",
    "invoice name": "invoice_name",

    # Other
    "fiscalyear": "fiscal_year",
    "fiscal_year": "fiscal_year",
    "fiscal year": "fiscal_year",
    "fiscal_yr": "fiscal_year",
    "g/lindicator": "gl_indicator",
    "gl_indicator": "gl_indicator",
    "conceptserach": "concept_search",
    "concept_search": "concept_search",
    "invoicedescription": "invoice_description",
    "invoice_description": "invoice_description",
    "invoice description": "invoice_description",

    # Messy CSV abbreviations
    "sys_inv_no": "system_invoice_no",
    "inv_dt": "date_documented",
    "inv_date": "date_documented",
    "doc_date": "date_documented",
    "inv_status": "invoice_status",
    "inv_type": "invoice_type",
    "inv_desc": "invoice_description",
    "inv_cesc": "invoice_description",
    "inv_name": "invoice_name",
    "inv_line_ref": "invoice_line_ref",
    "excl_amt": "amount_excl_lc",
    "excl_amount": "amount_excl_lc",
    "vat_amt": "amount_vat_lc",
    "amt_vat": "amount_vat_lc",
    "incl_amt": "amount_incl_lc",
    "total_amount": "amount_incl_lc",
    "total_amt": "amount_incl_lc",
    "pmt_amt": "payment_amount",
    "pmt_amount": "payment_amount",
    "pmt_type": "payment_type",
    "pmt_date": "payment_date",
    "pmt_no": "payment_number",
    "pmt_num": "payment_number",
    "sched_pay_dt": "scheduled_pay_date",
    "sched_pay_date": "scheduled_pay_date",
    "due_dt": "due_date",
    "erp_system": "erp_source",
    "erp_type": "erp_source",
    "source_erp": "erp_source",
    "dr_cr_ind": "debit_credit_indicator",
    "debit_credit_ind": "debit_credit_indicator",
    "capture_dt": "date_captured",
    "captured_dt": "date_captured",
    "auth_by": "authorized_by",
    "auth_date": "date_authorized",
    "auth_dt": "date_authorized",
    "vend_name": "vendor_name",
    "vendor_nm": "vendor_name",
    "ref_currency": "reference_currency",
    "ref_curr": "reference_currency",
    "posting_dt": "date_documented",
    "physical_inv_no": "physical_invoice_no",
    "phys_inv_no": "physical_invoice_no",

    # Block Group — AP22 missing value check (client-specific grouping field)
    "block group": "block_group",
    "block_group": "block_group",
    "blockgroup": "block_group",
    "block grp": "block_group",

    # EYPK — internal row identity key used by SPs
    "eypk": "eypk",
    "ey_pk": "eypk",
    "ey pk": "eypk",
    "row_id": "eypk",

    # ActualPayDate — actual payment date
    "actualpaydate": "actual_pay_date",
    "actual_pay_date": "actual_pay_date",
    "actual pay date": "actual_pay_date",
    "actual_payment_date": "actual_pay_date",
    "actualpaymentdate": "actual_pay_date",

    # PO/NonPO — procurement indicator
    "po/nonpo": "po_non_po",
    "po_non_po": "po_non_po",
    "po/non-po": "po_non_po",
    "po_nonpo": "po_non_po",
    "purchaseorder_flag": "po_non_po",

    # VendorCountry
    "vendorcountry": "vendor_country",
    "vendor_country": "vendor_country",
    "vendor country": "vendor_country",
    "supplier_country": "vendor_country",
    "suppliercountry": "vendor_country",
}
