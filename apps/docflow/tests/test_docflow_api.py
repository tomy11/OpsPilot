from pathlib import Path

import httpx

from opspilot_core.testing import token

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


async def upload(api: httpx.AsyncClient, headers: dict, name: str, media_type: str = "text/plain"):
    data = (SAMPLES / name).read_bytes() if (SAMPLES / name).exists() else name.encode()
    r = await api.post(
        "/docflow/documents", files={"file": (name, data, media_type)}, headers=headers
    )
    assert r.status_code == 201, r.text
    return r.json()


def codes(doc: dict) -> set[str]:
    return {i["code"] for i in doc["issues"]}


async def decide(api, doc_id: str, headers: dict, approve: bool = True) -> httpx.Response:
    return await api.post(
        f"/docflow/documents/{doc_id}/decision", json={"approve": approve}, headers=headers
    )


# ---- happy paths ------------------------------------------------------------------------------


async def test_small_clean_thai_invoice_posts_automatically(api, erp_app):
    staff = await token(api, "s1", "staff", "ops")
    doc = await upload(api, staff, "th-dairy-small.txt")

    assert doc["status"] == "posted" and doc["decided_by"] == "system"
    assert doc["extraction"]["invoice_date"] == "2026-11-12"  # Buddhist era 2569 converted
    assert doc["erp_invoice_id"].startswith("AP-")
    store = erp_app.state.store
    assert len(store.ap_invoices) == 1
    assert store.purchase_orders["PO-2002"]["status"] == "invoiced"


async def test_large_clean_invoice_needs_a_manager(api, erp_app):
    staff = await token(api, "s1", "staff", "ops")
    manager = await token(api, "m1", "manager", "ops")
    doc = await upload(api, staff, "siam-coffee-large.txt")
    assert doc["status"] == "needs_review" and doc["issues"] == []

    queue = (await api.get("/docflow/review-queue", headers=manager)).json()
    assert [d["id"] for d in queue] == [doc["id"]]

    assert (await decide(api, doc["id"], staff)).status_code == 403  # staff cannot review
    r = await decide(api, doc["id"], manager)
    assert r.status_code == 200 and r.json()["status"] == "posted"
    assert (await decide(api, doc["id"], manager)).status_code == 409  # double click
    assert len(erp_app.state.store.ap_invoices) == 1


# ---- review rules -----------------------------------------------------------------------------


async def test_reviewer_rules(api):
    m1 = await token(api, "m1", "manager", "ops")
    m_fin = await token(api, "m9", "manager", "finance")
    doc = await upload(api, m1, "siam-coffee-large.txt")

    r = await decide(api, doc["id"], m1)
    assert r.status_code == 403 and "own" in r.json()["detail"]  # no self-approval
    assert (await decide(api, doc["id"], m_fin)).status_code == 404  # other dept cannot see it
    assert (await api.get("/docflow/review-queue", headers=m1)).json() == []  # own uploads hidden

    admin = await token(api, "a1", "admin", "it")
    audit = (await api.get(f"/audit?target={doc['id']}", headers=admin)).json()
    assert any(a["action"] == "docflow.review" and a["status"] == "denied" for a in audit)


async def test_errors_block_approval_until_corrected(api, erp_app):
    staff = await token(api, "s1", "staff", "ops")
    manager = await token(api, "m1", "manager", "ops")
    doc = await upload(api, staff, "siam-coffee-bad-math.txt")
    assert {"line_amount_mismatch", "po_amount_mismatch"} <= codes(doc)

    r = await decide(api, doc["id"], manager)
    assert r.status_code == 422 and erp_app.state.store.ap_invoices == []

    fixed = doc["extraction"] | {
        "line_items": [doc["extraction"]["line_items"][0] | {"amount": 9000.0}],
        "subtotal": 9000.0,
        "vat_amount": 630.0,
        "total": 9630.0,
    }
    r = await api.put(f"/docflow/documents/{doc['id']}/extraction", json=fixed, headers=manager)
    assert r.status_code == 200 and r.json()["issues"] == []
    assert (await decide(api, doc["id"], manager)).json()["status"] == "posted"

    audit = (await api.get(f"/audit?target={doc['id']}", headers=manager)).json()
    corrected = next(a for a in audit if a["action"] == "docflow.corrected")
    assert corrected["detail"]["fields"] == ["line_items", "subtotal", "total", "vat_amount"]
    assert corrected["detail"]["before"]["total"] == 10265.0


async def test_bad_tax_id_receipt_is_rejected(api):
    staff = await token(api, "s1", "staff", "ops")
    manager = await token(api, "m1", "manager", "ops")
    doc = await upload(api, staff, "unknown-vendor-bad-taxid.txt")
    assert doc["extraction"]["doc_type"] == "receipt"
    assert {"tax_id_invalid", "no_po"} <= codes(doc)
    assert doc["status"] == "needs_review"  # small total, but has issues: never auto-posted

    r = await decide(api, doc["id"], manager, approve=False)
    assert r.json()["status"] == "rejected"
    assert (await decide(api, doc["id"], manager)).status_code == 409


async def test_non_invoice_with_injected_instructions_is_not_posted(api, erp_app):
    staff = await token(api, "s1", "staff", "ops")
    doc = await upload(api, staff, "not-an-invoice.txt")
    assert doc["status"] == "needs_review" and "not_an_invoice" in codes(doc)
    assert erp_app.state.store.ap_invoices == []


async def test_duplicate_invoice_is_flagged(api):
    staff = await token(api, "s1", "staff", "ops")
    first = await upload(api, staff, "th-dairy-small.txt")
    second = await upload(api, staff, "th-dairy-small.txt")
    assert first["status"] == "posted"
    assert second["status"] == "needs_review"
    assert {"duplicate_invoice", "po_not_open"} <= codes(second)


async def test_pending_docflow_duplicate_is_flagged_before_erp_knows(api):
    staff = await token(api, "s1", "staff", "ops")
    first = await upload(api, staff, "siam-coffee-large.txt")
    second = await upload(api, staff, "siam-coffee-large.txt")
    dup = next(i for i in second["issues"] if i["code"] == "duplicate_invoice")
    assert f"docflow:{first['id']}" in dup["message"]


# ---- ERP failures -----------------------------------------------------------------------------


async def test_post_failure_then_retry_posts_exactly_once(api, erp_app):
    staff = await token(api, "s1", "staff", "ops")
    manager = await token(api, "m1", "manager", "ops")
    doc = await upload(api, staff, "siam-coffee-large.txt")

    erp_app.state.error_rate = 1.0  # ERP down for the approval
    r = await decide(api, doc["id"], manager)
    assert r.json()["status"] == "post_failed" and "503" in r.json()["error"]

    erp_app.state.error_rate = 0.0
    r = await api.post(f"/docflow/documents/{doc['id']}/retry-post", headers=manager)
    assert r.json()["status"] == "posted"
    r = await api.post(f"/docflow/documents/{doc['id']}/retry-post", headers=manager)
    assert r.status_code == 409
    assert len(erp_app.state.store.ap_invoices) == 1


async def test_erp_outage_during_upload_gives_warnings_not_errors(api, erp_app):
    staff = await token(api, "s1", "staff", "ops")
    erp_app.state.error_rate = 1.0
    doc = await upload(api, staff, "th-dairy-small.txt")
    erp_app.state.error_rate = 0.0
    assert doc["status"] == "needs_review"  # warnings stop auto-post
    assert codes(doc) == {"vendor_unchecked", "po_unchecked", "duplicate_unchecked"}


# ---- uploads, manual entry, visibility --------------------------------------------------------


async def test_unsupported_type_and_offline_pdf(api):
    staff = await token(api, "s1", "staff", "ops")
    manager = await token(api, "m1", "manager", "ops")
    r = await api.post(
        "/docflow/documents", files={"file": ("x.zip", b"PK", "application/zip")}, headers=staff
    )
    assert r.status_code == 415

    doc = await upload(api, staff, "scan.pdf", media_type="application/pdf")
    assert doc["status"] == "extraction_failed" and "text/plain only" in doc["error"]

    manual = {
        "doc_type": "tax_invoice",
        "vendor_name": "Thai Dairy Supply Co., Ltd.",
        "vendor_tax_id": "0105560987653",
        "invoice_number": "TD-MANUAL-1",
        "invoice_date": "2026-11-15",
        "due_date": None,
        "po_number": "PO-2002",
        "currency": "THB",
        "line_items": [
            {"description": "milk", "sku": "SKU-MILK", "qty": 30, "unit_price": 85, "amount": 2550}
        ],
        "subtotal": 2550.0,
        "vat_amount": 178.5,
        "total": 2728.5,
    }
    r = await api.put(f"/docflow/documents/{doc['id']}/extraction", json=manual, headers=manager)
    assert r.json()["status"] == "needs_review" and r.json()["issues"] == []


async def test_staff_only_see_their_own_documents(api):
    s1 = await token(api, "s1", "staff", "ops")
    s2 = await token(api, "s2", "staff", "ops")
    doc = await upload(api, s1, "siam-coffee-large.txt")

    assert (await api.get(f"/docflow/documents/{doc['id']}", headers=s2)).status_code == 404
    assert (await api.get("/docflow/documents", headers=s2)).json() == []
    f = await api.get(f"/docflow/documents/{doc['id']}/file", headers=s1)
    assert f.content == (SAMPLES / "siam-coffee-large.txt").read_bytes()

    listed = await api.get("/docflow/documents?status=needs_review", headers=s1)
    assert [d["id"] for d in listed.json()] == [doc["id"]]
