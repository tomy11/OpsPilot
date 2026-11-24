"""Retrieval + answer eval for OpsPilot Knowledge.

Each case runs as a specific user (role@department) so the eval measures access control along
with quality. Metrics:
  hit@k            expected document in the top k retrieved chunks (answerable cases)
  mrr              mean reciprocal rank of the expected document
  grounded         answer came back with a valid citation to the expected document
  wrong_answer     answered with citations, but not to the expected document (should be ~0)
  nothing_retrieved  unanswerable cases where the relevance gate let nothing through
  no_answer_acc    unanswerable / not-permitted cases correctly refused
  acl_leaks        forbidden document appeared in retrieval or citations (must be 0)

Run: uv run python apps/knowledge/evals/knowledge_eval.py
     add `--cases semantic --split test` for questions that share no words with the documents
     (paraphrases and Thai questions about English documents, and the reverse)
     set EMBEDDING_PROVIDER=ollama to evaluate bge-m3, EVAL_DATABASE_URL to run on Postgres

Thresholds may be chosen on the "tune" split only; "test" is for reporting.
The answerer is the offline extractive one, so `grounded` says nothing about a real model.
"""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from opspilot_core.auth import Role, User
from opspilot_core.db import Database
from opspilot_core.llm import LLMClient
from opspilot_knowledge.answer import FakeGroundedLLM, answer_question
from opspilot_knowledge.config import KnowledgeSettings
from opspilot_knowledge.embeddings import Embedder, HashingEmbedder
from opspilot_knowledge.main import build_embedder
from opspilot_knowledge.service import KnowledgeService

HERE = Path(__file__).resolve().parent
FILES = {
    "keyword": HERE / "knowledge_cases.jsonl",
    "semantic": HERE / "semantic_cases.jsonl",
}


@dataclass
class Report:
    k: int
    answerable: int = 0
    hits: int = 0
    rr: float = 0.0
    grounded: int = 0
    unanswerable: int = 0
    refused: int = 0
    empty: int = 0
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
            "nothing_retrieved": round(self.empty / u, 3),
            "no_answer_acc": round(self.refused / u, 3),
            "acl_leaks": self.acl_leaks,
        }


def _user(spec: str) -> User:
    role, dept = spec.split("@")
    return User(id=f"eval-{role}-{dept}", role=Role(role), department=dept)


def _name(source: str) -> str:
    return source.rsplit("/", 1)[-1]


async def run_eval(
    embedder: Embedder | None = None,
    llm: LLMClient | None = None,
    k: int = 3,
    *,
    cases: str = "keyword",
    split: str | None = None,
    database_url: str = "sqlite+aiosqlite:///:memory:",
) -> Report:
    db = Database(database_url)
    await db.create_all()
    kb = KnowledgeService(db, embedder or HashingEmbedder(), top_k=k)
    await kb.setup()
    await kb.seed_dir()
    await kb.index_missing()  # fail loudly: an eval without vectors is not an eval
    llm = llm or FakeGroundedLLM()
    report = Report(k=k)
    try:
        for line in FILES[cases].read_text(encoding="utf-8").splitlines():
            case = json.loads(line)
            if split and case.get("split") != split:
                continue
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
                report.empty += not retrieved
                if not answer.grounded:
                    report.refused += 1
                else:
                    report.failures.append(f"{case['id']}: answered but should refuse: {cited}")
    finally:
        await db.dispose()
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", choices=FILES, default="keyword")
    parser.add_argument("--split", choices=["tune", "test"])
    args = parser.parse_args()
    embedder = build_embedder(KnowledgeSettings(_env_file=None))
    print(f"embedder: {embedder.name}, min_vector: {embedder.min_vector}")
    report = asyncio.run(
        run_eval(
            embedder,
            cases=args.cases,
            split=args.split,
            database_url=os.getenv("EVAL_DATABASE_URL", "sqlite+aiosqlite:///:memory:"),
        )
    )
    print(json.dumps(report.metrics(), indent=2))
    for f in report.failures:
        print("  -", f)
    return 1 if report.acl_leaks else 0


if __name__ == "__main__":
    sys.exit(main())
