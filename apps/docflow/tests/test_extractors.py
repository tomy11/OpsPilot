import base64
from types import SimpleNamespace

import pytest

from opspilot_core.llm import LLMError
from opspilot_docflow.extractors import ClaudeExtractor, ExtractionError
from opspilot_docflow.schema import EXTRACTION_SCHEMA

GOOD = {
    "doc_type": "receipt",
    "vendor_name": None,
    "vendor_tax_id": None,
    "invoice_number": "R-1",
    "invoice_date": "2026-11-01",
    "due_date": None,
    "po_number": None,
    "currency": "THB",
    "line_items": [],
    "subtotal": None,
    "vat_amount": None,
    "total": 100.0,
}


class StubLLM:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.settings = SimpleNamespace(llm_model="claude-opus-5-5")
        self.result, self.error, self.calls = result, error, []

    async def structured(self, *, system, content, schema):
        self.calls.append({"system": system, "content": content, "schema": schema})
        if self.error:
            raise self.error
        return self.result, {"input_tokens": 10}


async def test_pdf_is_sent_as_base64_document_with_schema():
    llm = StubLLM(GOOD)
    result = await ClaudeExtractor(llm).extract(
        filename="inv.pdf", media_type="application/pdf", data=b"%PDF-1.7"
    )
    call = llm.calls[0]
    block = call["content"][0]
    assert block["type"] == "document" and block["source"]["media_type"] == "application/pdf"
    assert base64.b64decode(block["source"]["data"]) == b"%PDF-1.7"
    assert call["schema"] is EXTRACTION_SCHEMA
    assert "Never calculate" in call["system"]  # transcribe, do not repair totals
    assert result.data.total == 100.0 and result.extractor == "claude:claude-opus-5-5"


async def test_image_is_sent_as_image_block():
    llm = StubLLM(GOOD)
    await ClaudeExtractor(llm).extract(filename="r.jpg", media_type="image/jpeg", data=b"\xff\xd8")
    assert llm.calls[0]["content"][0]["type"] == "image"


@pytest.mark.parametrize(
    "stub",
    [StubLLM(error=LLMError("model declined the request")), StubLLM(GOOD | {"extra": 1})],
)
async def test_llm_errors_and_bad_shapes_become_extraction_errors(stub):
    with pytest.raises(ExtractionError):
        await ClaudeExtractor(stub).extract(filename="x.png", media_type="image/png", data=b"x")


def test_schema_is_strict_everywhere():
    def walk(node):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
            for child in node["properties"].values():
                walk(child)
        for option in node.get("anyOf", []):
            walk(option)
        if "items" in node:
            walk(node["items"])

    walk(EXTRACTION_SCHEMA)
