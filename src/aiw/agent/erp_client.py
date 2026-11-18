from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)


class ErpError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"ERP {status}: {detail}")
        self.status = status
        self.detail = detail

    @property
    def retryable(self) -> bool:
        return self.status >= 500 or self.status in (408, 429)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, ErpError):
        return exc.retryable
    return isinstance(exc, httpx.TransportError)


class ErpClient:
    """Thin ERP/POS client. Reads retry on transient errors; writes carry an Idempotency-Key
    so a retry (here or from a Temporal activity) never applies the same change twice."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        max_attempts: int = 4,
    ) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout_s, transport=transport)
        self._max_attempts = max_attempts

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_exponential_jitter(multiplier=0.05, max=2),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        ):
            with attempt:
                resp = await self._http.request(method, path, **kwargs)
                if resp.status_code >= 400:
                    try:
                        detail = resp.json().get("detail", resp.text)
                    except ValueError:
                        detail = resp.text
                    raise ErpError(resp.status_code, str(detail))
                return resp.json()
        raise AssertionError("unreachable")

    async def get_order(self, order_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/orders/{order_id}")

    async def get_inventory(self, sku: str, branch: str | None = None) -> dict[str, Any]:
        params = {"sku": sku} | ({"branch": branch} if branch else {})
        return await self._request("GET", "/inventory", params=params)

    async def create_stock_transfer(self, payload: dict[str, Any], idem_key: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/stock-transfers", json=payload, headers={"Idempotency-Key": idem_key}
        )

    async def update_order(
        self, order_id: str, payload: dict[str, Any], idem_key: str
    ) -> dict[str, Any]:
        return await self._request(
            "PATCH", f"/orders/{order_id}", json=payload, headers={"Idempotency-Key": idem_key}
        )

    async def adjust_inventory(self, payload: dict[str, Any], idem_key: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/inventory/adjustments", json=payload, headers={"Idempotency-Key": idem_key}
        )
