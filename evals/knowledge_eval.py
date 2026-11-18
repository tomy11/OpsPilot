"""Retrieval + answer eval for OpsPilot Knowledge.

Each case runs as a specific user (role@department) so the eval measures access control along
with quality. Metrics:
  hit@k            expected document in the top k retrieved chunks (answerable cases)
  mrr              mean reciprocal rank of the expected document
  grounded         answer came back with a valid citation to the expected document
  wrong_answer     answered with citations, but not to the expected document (should be ~0)
  no_answer_acc    unanswerable / not-permitted cases correctly refused
  acl_leaks        forbidden document appeared in retrieval or citations (must be 0)

Run: uv run python evals/knowledge_eval.py   (uses the offline hashing embedder + fake answerer;
set EMBEDDING_PROVIDER=voyage / LLM_PROVIDER=anthropic in the env to evaluate real models)
"""

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from opspilot.core.auth import Role, User
from opspilot.core.db import Database
from opspilot.core.llm import LLMClient
from opspilot.knowledge.answer import FakeGroundedLLM, answer_question
from opspilot.knowledge.embeddings import Embedder, HashingEmbedder
from opspilot.knowledge.service import KnowledgeService

CASES = Path(__file__).with_name("knowledge_cases.jsonl")


@dataclass
class Report:
    k: int
    answerable: int = 0
    hits: int = 0
    rr: float = 0.0
    grounded: int = 0
    unanswerable: int = 0
    refused: int = 0
    wrong: int = 0
    acl_leaks: int = 0
    failures: list[str] = field(default_factory=list)

    def metrics(self) -> dict[str, float]:
        a, u = self.answerable or 1, self.unanswerable or 1
        return {
            f"hit@{self.k}": round(self.hits / a, 3),
            "mrr": round(self.rr / a, 3),
            "grounded": round(self.grounded / a, 3),
            "wrong_answer": round(self.wrong / a, 3),
            "no_answer_acc": round(self.refused / u, 3),
            "acl_leaks": self.acl_leaks,
        }


def _user(spec: str) -> User:
    role, dept = spec.split("@")
    return User(id=f"eval-{role}-{dept}", role=Role(role), department=dept)


def _name(source: str) -> str:
    return source.rsplit("/", 1)[-1]


async def run_eval(
    embedder: Embedder | None = None, llm: LLMClient | None = None, k: int = 3
) -> Report:
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.create_all()
    kb = KnowledgeService(db, embedder or HashingEmbedder(), top_k=k)
    await kb.seed_dir()
    llm = llm or FakeGroundedLLM()
    report = Report(k=k)
    try:
        for line in CASES.read_text(encoding="utf-8").splitlines():
            case = json.loads(line)
            user = _user(case["user"])
            hits = await kb.search_as(user, case["question"], k)
            answer = await answer_question(llm, case["question"], hits)
            retrieved = [_name(h.source) for h in hits]
            cited = [_name(c.hit.source) for c in answer.citations]

            leaked = set(case.get("forbid", [])) & set(retrieved + cited)
            if leaked:
                report.acl_leaks += 1
                report.failures.append(f"{case['id']}: ACL LEAK {sorted(leaked)}")

            expect = case["expect"]
            if expect:
                report.answerable += 1
                if expect in retrieved:
                    report.hits += 1
                    report.rr += 1 / (retrieved.index(expect) + 1)
                else:
                    report.failures.append(f"{case['id']}: missed {expect}, got {retrieved}")
                if answer.grounded and expect in cited:
                    report.grounded += 1
                elif answer.grounded:
                    report.wrong += 1
                    report.failures.append(f"{case['id']}: answered from wrong doc {cited}")
                else:
                    report.failures.append(f"{case['id']}: refused ({answer.reason})")
            else:
                report.unanswerable += 1
                if not answer.grounded:
                    report.refused += 1
                else:
                    report.failures.append(f"{case['id']}: answered but should refuse: {cited}")
    finally:
        await db.dispose()
    return report


def main() -> int:
    report = asyncio.run(run_eval())
    print(json.dumps(report.metrics(), indent=2))
    for f in report.failures:
        print("  -", f)
    return 1 if report.acl_leaks else 0


if __name__ == "__main__":
    sys.exit(main())
