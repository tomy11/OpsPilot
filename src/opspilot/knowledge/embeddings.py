"""Embedding providers.

`HashingEmbedder` is deterministic and offline (character n-grams hashed into a fixed vector), so
tests and demos need no API key. `VoyageEmbedder` calls Voyage AI's embeddings endpoint for real
semantic vectors. Both return L2-normalised vectors, so cosine similarity is a dot product.
"""

import hashlib
import math
import re
from typing import Literal, Protocol

import httpx

InputType = Literal["document", "query"]


class Embedder(Protocol):
    name: str

    async def embed(self, texts: list[str], input_type: InputType) -> list[list[float]]: ...


class EmbeddingError(Exception):
    pass


def _normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec] if norm else vec


class HashingEmbedder:
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


class VoyageEmbedder:
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
