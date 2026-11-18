"""Tokenisation shared by lexical search and the hashing embedder.

Latin/digit runs become lowercase words. Thai runs (no word spacing) become character bigrams,
which gives usable recall without a Thai word segmenter; swap in PyThaiNLP `word_tokenize` later.
"""

import re

_RUN = re.compile(r"[฀-๿]+|[a-z0-9][a-z0-9_-]*")
_THAI = re.compile(r"[฀-๿]")
_STOP = frozenset(
    "a an and are can do does for how i in is it my of on or the to what when where which who "
    "why with you your".split()
)


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for run in _RUN.findall(text.lower()):
        if _THAI.match(run):
            tokens.extend(run[i : i + 2] for i in range(max(len(run) - 1, 1)))
        elif (len(run) > 1 or run.isdigit()) and run not in _STOP:
            tokens.append(run)
    return tokens
