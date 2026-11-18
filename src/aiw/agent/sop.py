"""Keyword search over SOP markdown files.

Placeholder for Project A (Enterprise RAG): swap `SopIndex.search` for hybrid search on pgvector
with department-aware filtering. The tool contract (query -> snippets with source) stays the same.
"""

import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SOP_DIR = Path(__file__).resolve().parents[3] / "docs" / "sop"


@dataclass
class SopChunk:
    source: str
    heading: str
    text: str


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[\w฀-๿-]+", text.lower()) if len(t) > 1}


class SopIndex:
    def __init__(self, sop_dir: Path = DEFAULT_SOP_DIR) -> None:
        self.chunks: list[SopChunk] = []
        for path in sorted(sop_dir.glob("*.md")):
            heading = path.stem
            buf: list[str] = []
            for line in path.read_text(encoding="utf-8").splitlines() + ["## "]:
                if line.startswith("## "):
                    if buf:
                        self.chunks.append(SopChunk(path.name, heading, "\n".join(buf).strip()))
                    heading, buf = line[3:].strip(), []
                elif not line.startswith("# "):
                    buf.append(line)

    def search(self, query: str, k: int = 3) -> list[dict[str, str]]:
        q = _tokens(query)
        scored = [(len(q & _tokens(c.heading + " " + c.text)), c) for c in self.chunks if c.text]
        scored = [s for s in scored if s[0] > 0]
        scored.sort(key=lambda s: s[0], reverse=True)
        return [{"source": c.source, "section": c.heading, "text": c.text} for _, c in scored[:k]]
