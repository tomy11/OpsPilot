from datetime import date

from opspilot_docflow.schema import Extraction, LineItem
from opspilot_docflow.validation import ErpContext, thai_tax_id_valid, validate

VENDOR = {"tax_id": "0105550123451", "name": "บริษัท สยามคอฟฟี่ จำกัด"}
PO = {
    "id": "PO-2001",
    "vendor_tax_id": "0105550123451",
    "status": "open",
    "total_before_vat": 9000.0,
}
TODAY = date(2026, 11, 18)


def _x(**over) -> Extraction:
    base = dict(
        doc_type="tax_invoice",
        vendor_name="Siam Coffee Co., Ltd.",
        vendor_tax_id="0105550123451",
        invoice_number="SC-1",
        invoice_date="2026-11-10",
        due_date="2026-12-10",
        po_number="PO-2001",
        currency="THB",
        line_items=[
            LineItem(description="beans", sku="SKU-COFFEE", qty=20, unit_price=450, amount=9000)
        ],
        subtotal=9000.0,
        vat_amount=630.0,
        total=9630.0,
    )
    return Extraction(**(base | over))


def _codes(x: Extraction, ctx: ErpContext | None = None) -> set[str]:
    ctx = ctx or ErpContext(vendor=VENDOR | {"name": "Siam Coffee Co., Ltd."}, purchase_order=PO)
    return {i.code for i in validate(x, ctx, today=TODAY)}


def test_thai_tax_id_checksum():
    assert thai_tax_id_valid("0105550123451")
    assert not thai_tax_id_valid("0105550123452")  # wrong check digit
    assert not thai_tax_id_valid("010555012345")  # 12 digits


def test_clean_invoice_has_no_issues():
    assert _codes(_x()) == set()


def test_arithmetic_errors_are_caught_not_repaired():
    bad_line = [LineItem(description="beans", sku=None, qty=20, unit_price=450, amount=9500)]
    assert "line_amount_mismatch" in _codes(_x(line_items=bad_line))
    assert "vat_mismatch" in _codes(_x(vat_amount=700.0))
    assert "total_mismatch" in _codes(_x(total=9999.0))
    assert "subtotal_mismatch" in _codes(_x(subtotal=9100.0))


def test_one_satang_rounding_is_tolerated():
    assert _codes(_x(vat_amount=630.01, total=9630.01)) == set()


def test_vendor_rules():
    assert "tax_id_invalid" in _codes(_x(vendor_tax_id="0105550123452"))
    assert "vendor_unknown" in _codes(_x(), ErpContext(vendor=None, purchase_order=PO))
    mismatch = ErpContext(vendor=VENDOR | {"name": "Other Trading"}, purchase_order=PO)
    assert "vendor_name_mismatch" in _codes(_x(), mismatch)


def test_thai_and_english_company_names_match_after_normalising():
    ctx = ErpContext(
        vendor={"tax_id": "1", "name": "Thai Dairy Supply Co., Ltd."}, purchase_order=PO
    )
    assert "vendor_name_mismatch" not in _codes(_x(vendor_name="THAI DAIRY SUPPLY CO., LTD."), ctx)


def test_dates():
    assert "date_in_future" in _codes(_x(invoice_date="2027-01-01", due_date="2027-02-01"))
    assert "due_before_invoice" in _codes(_x(due_date="2026-11-01"))
    assert "date_invalid" in _codes(_x(invoice_date="12/11/2569"))


def test_po_rules():
    ctx = ErpContext(vendor=VENDOR | {"name": "Siam Coffee Co., Ltd."})
    assert "po_not_found" in _codes(_x(), ctx)
    other = ErpContext(vendor=ctx.vendor, purchase_order=PO | {"vendor_tax_id": "0105560987653"})
    assert "po_vendor_mismatch" in _codes(_x(), other)
    invoiced = ErpContext(vendor=ctx.vendor, purchase_order=PO | {"status": "invoiced"})
    assert "po_not_open" in _codes(_x(), invoiced)
    assert "no_po" in _codes(_x(po_number=None))


def test_erp_outage_downgrades_to_warnings():
    issues = validate(
        _x(), ErpContext(failed={"vendor", "purchase_order", "duplicates"}), today=TODAY
    )
    assert {i.code for i in issues} == {"vendor_unchecked", "po_unchecked", "duplicate_unchecked"}
    assert all(i.severity == "warning" for i in issues)


def test_duplicate_and_non_invoice():
    dup = ErpContext(
        vendor=VENDOR | {"name": "Siam Coffee Co., Ltd."},
        purchase_order=PO,
        duplicate_refs=["AP-1"],
    )
    assert "duplicate_invoice" in _codes(_x(), dup)
    assert "not_an_invoice" in _codes(_x(doc_type="other"))
