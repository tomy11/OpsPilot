import sys
from pathlib import Path

import pytest

from opspilot_core.llm import LLMTurn
from opspilot_core.testing import token
from opspilot_knowledge.answer import NOT_FOUND_TEXT, answer_question, verify_citations
from opspilot_knowledge.chunking import chunk_markdown, parse_front_matter
from opspilot_knowledge.service import Hit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))


def _hit(n: int) -> Hit:
    return Hit(f"c{n}", f"d{n}", f"Doc {n}", f"docs/{n}.md", 1, "H", "text", 0.1, 1.0, 0.5)


class _StubLLM:
    def __init__(self, text: str) -> None:
        self.text = text

    async def respond(self, *, system, messages, tools) -> LLMTurn:
        return LLMTurn(content=[], text=self.text, stop_reason="end_turn")


# ---- units ------------------------------------------------------------------------------------


def test_front_matter_and_heading_paths():
    doc = parse_front_matter("---\ntitle: T\ndepartments: hr,ops\n---\n# T\n## A\nx\n\n## B\ny\n")
    assert doc.meta == {"title": "T", "departments": "hr,ops"}
    chunks = chunk_markdown(doc.body, "T")
    assert [(c.heading, c.text) for c in chunks] == [("T > A", "x"), ("T > B", "y")]


def test_long_thai_paragraph_is_split_under_limit():
    chunks = chunk_markdown("## S\n" + "ลาพักร้อน " * 200, "T", max_chars=300)
    assert len(chunks) > 1
    assert all(len(c.text) <= 300 for c in chunks)


def test_verify_citations_drops_unknown_sources():
    text, ids = verify_citations("A [1]. B [9]. C [1, 2].", [_hit(1), _hit(2)])
    assert ids == [1, 2]
    assert "[9]" not in text and "[1][2]" in text


async def test_uncited_answer_is_replaced_by_refusal():
    result = await answer_question(_StubLLM("It is 42 days."), "q", [_hit(1)])
    assert not result.grounded and result.text == NOT_FOUND_TEXT
    assert result.reason == "uncited_answer"


async def test_citation_to_unsent_source_is_not_grounded():
    result = await answer_question(_StubLLM("It is 42 days [3]."), "q", [_hit(1)])
    assert not result.grounded


# ---- API: grounded answers and access control -------------------------------------------------


async def test_ask_returns_grounded_answer_with_citation(api):
    h = await token(api, "s1", "staff", "ops")
    r = await api.post("/knowledge/ask", json={"question": "ลาพักร้อนได้กี่วันต่อปี"}, headers=h)
    body = r.json()
    assert r.status_code == 200 and body["grounded"]
    assert "10 วัน" in body["answer"]
    assert body["citations"][0]["source"] == "seed/leave-policy.md"


@pytest.mark.parametrize(
    ("user", "role", "dept", "visible"),
    [
        ("s-ops", "staff", "ops", False),
        ("s-hr", "staff", "hr", False),  # right department, role too low
        ("m-ops", "manager", "ops", False),  # right role, wrong department
        ("a-it", "admin", "it", False),  # admins get no read bypass
        ("m-hr", "manager", "hr", True),
    ],
)
async def test_confidential_doc_only_reaches_hr_managers(api, user, role, dept, visible):
    h = await token(api, user, role, dept)
    q = {"query": "เงินเดือนระดับ Manager"}
    hits = (await api.post("/knowledge/search", json=q, headers=h)).json()
    docs = (await api.get("/knowledge/documents", headers=h)).json()
    ask = (await api.post("/knowledge/ask", json={"question": q["query"]}, headers=h)).json()

    salary = "seed/salary-structure.md"
    assert any(x["source"] == salary for x in hits) is visible
    assert any(d["source"] == salary for d in docs) is visible
    assert any(c["source"] == salary for c in ask["citations"]) is visible
    if not visible:
        assert "50,000" not in ask["answer"]


async def test_department_doc_hidden_from_other_departments(api):
    q = {"query": "when is the general ledger locked"}
    fin = await api.post(
        "/knowledge/search", json=q, headers=await token(api, "f", "staff", "finance")
    )
    ops = await api.post("/knowledge/search", json=q, headers=await token(api, "o", "staff", "ops"))
    assert fin.json()[0]["source"] == "seed/month-end-close.md"
    assert all(x["source"] != "seed/month-end-close.md" for x in ops.json())


async def test_off_topic_question_is_refused(api):
    h = await token(api, "s1", "staff", "ops")
    body = (await api.post("/knowledge/ask", json={"question": "ราคาหุ้นวันนี้"}, headers=h)).json()
    assert not body["grounded"] and body["citations"] == []


# ---- API: publishing, versioning, removal -----------------------------------------------------

DOC = {
    "title": "Ops Shift Handover",
    "source": "ops/shift-handover",
    "content": "# Handover\n## Cash\nCount the cash drawer twice before handing over the shift.",
    "departments": ["ops"],
}


async def test_publish_permissions(api):
    staff = await token(api, "s1", "staff", "ops")
    mgr = await token(api, "m1", "manager", "ops")
    admin = await token(api, "a1", "admin", "it")
    assert (await api.post("/knowledge/documents", json=DOC, headers=staff)).status_code == 403
    company_wide = DOC | {"departments": ["*"]}
    assert (
        await api.post("/knowledge/documents", json=company_wide, headers=mgr)
    ).status_code == 403
    assert (await api.post("/knowledge/documents", json=DOC, headers=mgr)).status_code == 201
    assert (
        await api.post("/knowledge/documents", json=company_wide, headers=admin)
    ).status_code == 201


async def test_reupload_versions_and_old_text_disappears(api):
    mgr = await token(api, "m1", "manager", "ops")
    first = (await api.post("/knowledge/documents", json=DOC, headers=mgr)).json()
    same = (await api.post("/knowledge/documents", json=DOC, headers=mgr)).json()
    assert first["created"] and not same["created"]

    v2 = DOC | {
        "content": "# Handover\n## Cash\nCount the cash drawer three times and sign the log."
    }
    second = (await api.post("/knowledge/documents", json=v2, headers=mgr)).json()
    assert second["document"]["version"] == 2

    hits = (
        await api.post("/knowledge/search", json={"query": "cash drawer handover"}, headers=mgr)
    ).json()
    texts = [x["text"] for x in hits if x["source"] == "ops/shift-handover"]
    assert texts == ["Count the cash drawer three times and sign the log."]


async def test_deactivate_hides_doc_and_does_not_leak_existence(api):
    mgr_ops = await token(api, "m1", "manager", "ops")
    mgr_hr = await token(api, "m2", "manager", "hr")
    doc_id = (await api.post("/knowledge/documents", json=DOC, headers=mgr_ops)).json()["document"][
        "id"
    ]

    assert (await api.delete(f"/knowledge/documents/{doc_id}", headers=mgr_hr)).status_code == 404
    assert (await api.delete(f"/knowledge/documents/{doc_id}", headers=mgr_ops)).status_code == 204
    hits = (
        await api.post("/knowledge/search", json={"query": "cash drawer"}, headers=mgr_ops)
    ).json()
    assert all(x["document_id"] != doc_id for x in hits)


async def test_ask_is_audited_with_pii_masked(api):
    staff = await token(api, "s1", "staff", "ops")
    q = "ลาพักร้อนได้กี่วัน โทรกลับ 0812345678"
    await api.post("/knowledge/ask", json={"question": q}, headers=staff)
    mgr = await token(api, "m1", "manager", "ops")
    entries = [
        a for a in (await api.get("/audit", headers=mgr)).json() if a["action"] == "knowledge.ask"
    ]
    assert entries and "0812345678" not in entries[0]["detail"]["question"]
    assert entries[0]["detail"]["cited"]


# ---- eval gate --------------------------------------------------------------------------------


async def test_knowledge_eval_thresholds():
    from knowledge_eval import run_eval

    m = (await run_eval()).metrics()
    assert m["acl_leaks"] == 0
    assert m["no_answer_acc"] == 1.0
    assert m["hit@3"] >= 0.9


async def test_document_audit_follows_the_documents_acl_not_the_actor(api):
    admin = await token(api, "a1", "admin", "it")
    ops_doc = DOC | {"source": "ops/handover-by-admin"}
    hr_doc = DOC | {"source": "hr/bonus", "departments": ["hr"], "min_role": "manager"}
    ops_id = (await api.post("/knowledge/documents", json=ops_doc, headers=admin)).json()
    hr_id = (await api.post("/knowledge/documents", json=hr_doc, headers=admin)).json()
    ops_id, hr_id = ops_id["document"]["id"], hr_id["document"]["id"]
    assert (await api.delete(f"/knowledge/documents/{ops_id}", headers=admin)).status_code == 204

    async def doc_events(user, dept):
        h = await token(api, user, "manager", dept)
        rows = (await api.get("/audit", headers=h)).json()
        return {(a["action"], a["target"]) for a in rows if a["action"].startswith("knowledge.doc")}

    # Published by an IT admin, but it is an ops document: ops managers see it, IT managers don't.
    assert await doc_events("m-ops", "ops") == {
        ("knowledge.document.ingested", ops_id),
        ("knowledge.document.deactivated", ops_id),
    }
    assert await doc_events("m-it", "it") == set()  # not even the HR doc's name leaks
    assert await doc_events("m-hr", "hr") == {("knowledge.document.ingested", hr_id)}
