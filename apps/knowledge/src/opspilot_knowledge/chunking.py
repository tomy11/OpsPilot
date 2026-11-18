"""Markdown-aware chunking.

Splits on headings so each chunk keeps its heading path ("Doc > Section") for citations, then
packs paragraphs up to `max_chars`. Thai text has no spaces between words, so limits are in
characters, and an oversized paragraph is cut on sentence-ish boundaries (newline, space, ฯ).
"""

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


@dataclass
class Chunk:
    heading: str
    text: str


@dataclass
class ParsedDoc:
    meta: dict[str, str]
    body: str


def parse_front_matter(raw: str) -> ParsedDoc:
    """Minimal `---` key: value front-matter (no YAML dependency)."""
    if not raw.startswith("---\n"):
        return ParsedDoc({}, raw)
    end = raw.find("\n---", 4)
    if end == -1:
        return ParsedDoc({}, raw)
    meta: dict[str, str] = {}
    for line in raw[4:end].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return ParsedDoc(meta, raw[end + 4 :].lstrip("\n"))


def _split_long(text: str, max_chars: int) -> list[str]:
    parts: list[str] = []
    while len(text) > max_chars:
        cut = max(text.rfind(sep, 0, max_chars) for sep in ("\n", " ", "ฯ", "."))
        cut = cut if cut > max_chars // 2 else max_chars
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        parts.append(text)
    return parts


def chunk_markdown(body: str, title: str, max_chars: int = 800) -> list[Chunk]:
    path: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    paragraphs: list[str] = []
    buf: list[str] = []

    def flush_paragraph() -> None:
        if buf:
            paragraphs.append(" ".join(s.strip() for s in buf).strip())
            buf.clear()

    def flush_section() -> None:
        flush_paragraph()
        if any(paragraphs):
            heading = " > ".join([title, *path[1:]] if path else [title])
            sections.append((heading, [p for p in paragraphs if p]))
        paragraphs.clear()

    for line in body.splitlines():
        m = _HEADING.match(line)
        if m:
            flush_section()
            level = len(m.group(1))
            path[:] = path[: level - 1] + [m.group(2).strip()]
        elif not line.strip():
            flush_paragraph()
        else:
            buf.append(line)
    flush_section()

    chunks: list[Chunk] = []
    for heading, paras in sections:
        current = ""
        for para in paras:
            for piece in _split_long(para, max_chars):
                if current and len(current) + 1 + len(piece) > max_chars:
                    chunks.append(Chunk(heading, current))
                    current = piece
                else:
                    current = f"{current}\n{piece}" if current else piece
        if current:
            chunks.append(Chunk(heading, current))
    return chunks
