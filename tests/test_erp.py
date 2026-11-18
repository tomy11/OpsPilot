import httpx
import pytest

from aiw.agent.erp_client import ErpClient, ErpError
from aiw.mocks.erp_pos.app import create_app


async def test_write_is_idempotent(erp_app, erp_transport):
    erp = ErpClient("http://erp", transport=erp_transport)
    payload = {
        "sku": "SKU-COFFEE",
        "qty": 7,
        "from_branch": "bangna",
        "to_branch": "silom",
        "order_id": "ORD-1001",
    }
    first = await erp.create_stock_transfer(payload, "key-1")
    second = await erp.create_stock_transfer(payload, "key-1")
    assert first == second
    assert erp_app.state.store.write_count == 1
    assert erp_app.state.store.inventory[("silom", "SKU-COFFEE")] == 10
    await erp.aclose()


async def test_client_errors_are_not_retried(erp_transport):
    erp = ErpClient("http://erp", transport=erp_transport)
    with pytest.raises(ErpError) as exc:
        await erp.get_order("ORD-404")
    assert exc.value.status == 404 and not exc.value.retryable
    await erp.aclose()


async def test_transient_errors_are_retried():
    app = create_app(error_rate=0.0)
    attempts = {"n": 0}
    real = httpx.ASGITransport(app=app)

    class Flaky(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            attempts["n"] += 1
            if attempts["n"] < 3:
                return httpx.Response(503, json={"detail": "down"})
            return await real.handle_async_request(request)

    erp = ErpClient("http://erp", transport=Flaky(), max_attempts=4)
    order = await erp.get_order("ORD-1001")
    assert order["id"] == "ORD-1001"
    assert attempts["n"] == 3
    await erp.aclose()
