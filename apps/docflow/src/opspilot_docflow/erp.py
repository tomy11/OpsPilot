"""ERP calls DocFlow needs: vendor master, purchase orders, AP invoices."""

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
        self.status, self.detail = status, detail

    @property
    def retryable(self) -> bool:
        return self.status >= 500 or self.status in (0, 408, 429)


def _retryable(exc: BaseException) -> bool:
    return isinstance(exc, ErpError) and exc.retryable


class ErpClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 5.0,
        attempts: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout_s, transport=transport)
        self._attempts = attempts

    async def _request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception(_retryable),
            stop=stop_after_attempt(self._attempts),
            wait=wait_exponential_jitter(multiplier=0.05, max=1),
            reraise=True,
        ):
            with attempt:
                try:
                    r = await self._client.request(method, url, **kw)
                except httpx.TransportError as exc:
                    raise ErpError(0, f"transport: {exc}") from exc
                if r.status_code >= 400 and r.status_code != 404:
                    try:
                        detail = str(r.json().get("detail", r.text))
                    except ValueError:
                        detail = r.text
                    raise ErpError(r.status_code, detail)
        return r

    async def _get_or_none(self, url: str) -> dict[str, Any] | None:
        r = await self._request("GET", url)
        return None if r.status_code == 404 else r.json()

    async def get_vendor(self, tax_id: str) -> dict[str, Any] | None:
        return await self._get_or_none(f"/vendors/{tax_id}")

    async def get_purchase_order(self, po_id: str) -> dict[str, Any] | None:
        return await self._get_or_none(f"/purchase-orders/{po_id}")

    async def find_invoices(self, vendor_tax_id: str, invoice_number: str) -> list[dict[str, Any]]:
        r = await self._request(
            "GET",
            "/ap-invoices",
            params={"vendor_tax_id": vendor_tax_id, "invoice_number": invoice_number},
        )
        return r.json()

    async def post_invoice(self, body: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        r = await self._request(
            "POST", "/ap-invoices", json=body, headers={"Idempotency-Key": idempotency_key}
        )
        if r.status_code == 404:
            raise ErpError(404, "ap-invoices endpoint not found")
        return r.json()

    async def aclose(self) -> None:
        await self._client.aclose()
