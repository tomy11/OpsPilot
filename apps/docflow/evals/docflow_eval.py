"""End-to-end DocFlow eval: extraction accuracy + validation + routing, through the real API.

Per case (`cases.jsonl`, files in `samples/`):
  field_accuracy     extracted fields equal to ground truth (amounts within 0.01)
  issue_recall       expected issue codes that were raised
  false_issue_rate   clean documents that got any issue (sent to review for nothing)
  routing_accuracy   final status matches (posted / needs_review)
  unsafe_auto_posts  documents with expected issues that were posted anyway (must be 0)

Run: uv run python apps/docflow/evals/docflow_eval.py
     LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=... uv run python apps/docflow/evals/docflow_eval.py
The offline extractor only reads the text samples; to evaluate Claude, add scanned PDFs/photos
of the same invoices to `samples/` and cases pointing at them.
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from opspilot_core.testing import token
from opspilot_docflow.config import DocflowSettings
from opspilot_docflow.main import create_app
from opspilot_erp_mock.app import create_app as create_erp_app

HERE = Path(__file__).resolve().parent
SAMPLES = HERE.parent / "samples"
MEDIA = {".txt": "text/plain", ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg"}


@dataclass
class Report:
    fields_total: int = 0
    fields_ok: int = 0
    codes_expected: int = 0
    codes_found: int = 0
    clean: int = 0
    clean_with_issues: int = 0
    cases: int = 0
    routed_ok: int = 0
    unsafe_auto_posts: int = 0
    failures: list[str] = field(default_factory=list)

    def metrics(self) -> dict[str, float]:
        return {
            "field_accuracy": round(self.fields_ok / (self.fields_total or 1), 3),
            "issue_recall": round(self.codes_found / (self.codes_expected or 1), 3),
            "false_issue_rate": round(self.clean_with_issues / (self.clean or 1), 3),
            "routing_accuracy": round(self.routed_ok / (self.cases or 1), 3),
            "unsafe_auto_posts": self.unsafe_auto_posts,
        }


def _same(a, b) -> bool:
    if isinstance(a, float | int) and isinstance(b, float | int):
        return abs(a - b) <= 0.01
    return a == b


async def run_eval() -> Report:
    report = Report()
    with tempfile.TemporaryDirectory() as tmp:
        settings = DocflowSettings(
            app_env="test",
            jwt_secret="eval-only-secret-with-at-least-32-bytes",
            database_url=f"sqlite+aiosqlite:///{tmp}/eval.db",
            erp_base_url="http://erp",
            llm_provider=os.getenv("LLM_PROVIDER", "fake"),
        )
        erp = create_erp_app(error_rate=0.0, latency_ms=0)
        for line in (HERE / "cases.jsonl").read_text(encoding="utf-8").splitlines():
            case = json.loads(line)
            # Fresh ERP + DB per case so cases cannot see each other as duplicates.
            erp.state.store.reset()
            settings = settings.model_copy(
                update={"database_url": f"sqlite+aiosqlite:///{tmp}/{case['file']}.db"}
            )
            app = create_app(settings, erp_transport=httpx.ASGITransport(app=erp))
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://docflow"
                ) as api:
                    h = await token(api, "eval", "staff", "ops")
                    path = SAMPLES / case["file"]
                    r = await api.post(
                        "/docflow/documents",
                        files={"file": (path.name, path.read_bytes(), MEDIA[path.suffix])},
                        headers=h,
                    )
                    doc = r.json()
            _score(report, case, doc)
    return report


def _score(report: Report, case: dict, doc: dict) -> None:
    name = case["file"]
    report.cases += 1
    extraction = doc.get("extraction") or {}
    for key, want in case["fields"].items():
        report.fields_total += 1
        got = extraction.get(key)
        if _same(got, want):
            report.fields_ok += 1
        else:
            report.failures.append(f"{name}: {key} = {got!r}, want {want!r}")

    got_codes = {i["code"] for i in doc.get("issues", [])}
    report.codes_expected += len(case["codes"])
    report.codes_found += len(set(case["codes"]) & got_codes)
    for missing in set(case["codes"]) - got_codes:
        report.failures.append(f"{name}: missed issue {missing}")
    if not case["codes"]:
        report.clean += 1
        if got_codes:
            report.clean_with_issues += 1
            report.failures.append(f"{name}: clean doc flagged {sorted(got_codes)}")

    if doc.get("status") == case["status"]:
        report.routed_ok += 1
    else:
        report.failures.append(f"{name}: status {doc.get('status')}, want {case['status']}")
    if case["codes"] and doc.get("status") == "posted":
        report.unsafe_auto_posts += 1


def main() -> int:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    report = asyncio.run(run_eval())
    print(json.dumps(report.metrics(), indent=2))
    for f in report.failures:
        print("  -", f)
    return 1 if report.unsafe_auto_posts else 0


if __name__ == "__main__":
    sys.exit(main())
