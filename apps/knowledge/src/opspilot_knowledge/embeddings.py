"""Embedding providers.

`HashingEmbedder` is deterministic and offline (character n-grams hashed into a fixed vector), so
tests and demos need no model. `OllamaEmbedder` runs a real multilingual model (bge-m3) on your own
Ollama, so document text stays on your machines. `VoyageEmbedder` calls Voyage AI.
All return L2-normalised vectors, so cosine similarity is a dot product.

`min_vector` is the similarity above which a chunk counts as being about the question. It depends
on the model: each one spreads its scores differently, so measure it before adding a model.
"""

import hashlib
import math
import re
from typing import Literal, Protocol

import httpx

InputType = Literal["document", "query"]


class Embedder(Protocol):
    name: str
    min_vector: float

    async def embed(self, texts: list[str], input_type: InputType) -> list[list[float]]: ...


class EmbeddingError(Exception):
    pass


def _normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


class HashingEmbedder:
    min_vector = 0.3

    def __init__(self, dims: int = 512, n: int = 3) -> None:
        self.dims, self.n = dims, n
        self.name = f"hash-{dims}-{n}"

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dims
        text = re.sub(r"\s+", " ", text.lower()).strip()
        for i in range(max(len(text) - self.n + 1, 1)):
            h = int.from_bytes(
                hashlib.blake2b(text[i : i + self.n].encode(), digest_size=8).digest()
            )
            vec[h % self.dims] += 1.0 if (h >> 63) & 1 else -1.0
        return _normalise(vec)

    async def embed(self, texts: list[str], input_type: InputType) -> list[list[float]]:
        return [self._vector(t) for t in texts]


# Measured with evals/semantic_cases.jsonl (split "tune"); see the README for how.
MEASURED_MIN_VECTOR = {"bge-m3": 0.52}


class OllamaEmbedder:
    """A local embedding model served by Ollama's `/api/embed`."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        min_vector: float | None = None,
        timeout_s: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        batch_size: int = 32,
    ) -> None:
        if min_vector is None:
            min_vector = MEASURED_MIN_VECTOR.get(model)
        if min_vector is None:
            raise ValueError(f"set KNOWLEDGE_MIN_VECTOR: no measured threshold for {model}")
        self.name = f"ollama:{model}"
        self.model, self.min_vector, self.batch_size = model, min_vector, batch_size
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_s, transport=transport)

    async def embed(self, texts: list[str], input_type: InputType) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            try:
                r = await self._client.post(
                    "/api/embed", json={"model": self.model, "input": batch}
                )
                r.raise_for_status()
                vectors = r.json()["embeddings"]
            except (httpx.HTTPError, KeyError, ValueError) as exc:
                raise EmbeddingError(f"embedding request failed: {exc}") from exc
            if len(vectors) != len(batch):
                raise EmbeddingError("embedding count does not match the request")
            out.extend(_normalise(v) for v in vectors)
        return out

    async def aclose(self) -> None:
        await self._client.aclose()


class VoyageEmbedder:
    min_vector = 0.5  # not measured yet

    URL = "https://api.voyageai.com/v1/embeddings"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        batch_size: int = 64,
    ) -> None:
        if not api_key:
            raise EmbeddingError("VOYAGE_API_KEY is required for embedding_provider=voyage")
        self.name = model
        self.batch_size = batch_size
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0, transport=transport
        )

    async def embed(self, texts: list[str], input_type: InputType) -> list[list[float]]:
        out: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            try:
                r = await self._client.post(
                    self.URL, json={"input": batch, "model": self.name, "input_type": input_type}
                )
                r.raise_for_status()
            except httpx.HTTPError as exc:
                raise EmbeddingError(f"embedding request failed: {exc}") from exc
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            out.extend(_normalise(d["embedding"]) for d in data)
        return out

    async def aclose(self) -> None:
        await self._client.aclose()


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))
