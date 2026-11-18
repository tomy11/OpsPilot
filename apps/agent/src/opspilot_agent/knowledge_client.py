"""HTTP client for the OpsPilot Knowledge service.

The agent searches *as the user*: it mints a short-lived token carrying the user's id, role and
department, so Knowledge applies exactly the same access filter it applies to that user directly.
The agent never sees a document the user could not open themselves.
"""

from typing import Any

import httpx

from opspilot_core.auth import User, issue_token
from opspilot_core.config import CoreSettings


class KnowledgeUnavailable(Exception):
    pass


class KnowledgeClient:
    def __init__(
        self,
        base_url: str,
        settings: CoreSettings,
        *,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # On-behalf-of tokens live for minutes, not the full session TTL.
        self._settings = settings.model_copy(update={"jwt_ttl_minutes": 2})
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_s, transport=transport)

    async def search(self, user: User, query: str, k: int = 3) -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {issue_token(user, self._settings)}"}
        try:
            r = await self._client.post(
                "/knowledge/search", json={"query": query, "k": k}, headers=headers
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise KnowledgeUnavailable(f"knowledge search failed: {exc}") from exc
        return r.json()

    async def aclose(self) -> None:
        await self._client.aclose()
