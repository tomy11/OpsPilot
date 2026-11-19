"""End-to-end through the API with the fake LLM and the inline approval backend."""

from opspilot_core.testing import token

TRANSFER_MSG = "เช็ค ORD-1001 ว่าของพอไหม ถ้าไม่พอให้โอนสต็อกจาก bangna"


async def test_read_only_question_runs_no_writes(api, erp_app):
    h = await token(api, "s1", "staff")
    r = await api.post("/agent/runs", json={"message": "เช็ค ORD-1002 หน่อย"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approvals"] == []
    assert "ORD-1002" in body["answer"]
    assert {s["tool"] for s in body["steps"] if s["type"] == "tool"} == {"get_order", "check_stock"}
    assert erp_app.state.store.write_count == 0


async def test_transfer_needs_approval_then_executes(api, erp_app):
    staff = await token(api, "s1", "staff")
    r = await api.post("/agent/runs", json={"message": TRANSFER_MSG}, headers=staff)
    body = r.json()

    # Agent proposed, ERP untouched.
    assert len(body["approvals"]) == 1
    approval = body["approvals"][0]
    assert approval["tool"] == "create_stock_transfer"
    assert approval["args"] == {
        "sku": "SKU-COFFEE",
        "qty": 7,
        "from_branch": "bangna",
        "to_branch": "silom",
        "order_id": "ORD-1001",
    }
    assert approval["status"] == "pending"
    assert approval["id"] in body["answer"]
    assert erp_app.state.store.write_count == 0

    # Requester cannot approve their own request; staff cannot approve at all.
    r = await api.post(
        f"/approvals/{approval['id']}/decision", json={"approve": True}, headers=staff
    )
    assert r.status_code == 403

    # Manager from another department cannot see it.
    other = await token(api, "m2", "manager", dept="finance")
    r = await api.post(
        f"/approvals/{approval['id']}/decision", json={"approve": True}, headers=other
    )
    assert r.status_code == 404

    manager = await token(api, "m1", "manager")
    r = await api.post(
        f"/approvals/{approval['id']}/decision",
        json={"approve": True, "reason": "ok"},
        headers=manager,
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "executed"
    assert erp_app.state.store.inventory[("silom", "SKU-COFFEE")] == 10
    assert erp_app.state.store.write_count == 1

    # Second click is rejected and nothing runs twice.
    r = await api.post(
        f"/approvals/{approval['id']}/decision", json={"approve": True}, headers=manager
    )
    assert r.status_code == 409
    assert erp_app.state.store.write_count == 1

    audit = (await api.get(f"/audit?target={approval['id']}", headers=manager)).json()
    actions = {a["action"] for a in audit}
    assert {
        "approval.requested",
        "approval.approved",
        "approval.executed",
        "approval.decide",
    } <= actions
    assert (await api.get(f"/audit?target={approval['id']}", headers=other)).json() == []
    assert (
        await api.get(f"/approvals/{approval['id']}/inventory", headers=other)
    ).status_code == 404


async def test_rejection_does_not_touch_erp(api, erp_app):
    staff = await token(api, "s1", "staff")
    approval = (
        await api.post("/agent/runs", json={"message": TRANSFER_MSG}, headers=staff)
    ).json()["approvals"][0]
    manager = await token(api, "m1", "manager")
    r = await api.post(
        f"/approvals/{approval['id']}/decision",
        json={"approve": False, "reason": "no"},
        headers=manager,
    )
    assert r.json()["status"] == "rejected"
    assert erp_app.state.store.write_count == 0


async def test_staff_cannot_use_admin_tool(api, erp_app):
    staff = await token(api, "s1", "staff")
    r = await api.post(
        "/agent/runs", json={"message": "ปรับสต็อก SKU-COFFEE silom +5"}, headers=staff
    )
    body = r.json()
    denied = [s for s in body["steps"] if s.get("status") == "denied"]
    assert [s["tool"] for s in denied] == ["adjust_inventory"]
    assert body["approvals"] == []
    assert "ไม่มีสิทธิ์" in body["answer"]
    assert erp_app.state.store.write_count == 0


async def test_failed_erp_write_is_recorded(api, erp_app):
    staff = await token(api, "s1", "staff")
    approval = (
        await api.post("/agent/runs", json={"message": TRANSFER_MSG}, headers=staff)
    ).json()["approvals"][0]
    # Stock disappears at the source before approval -> ERP returns 409 (permanent).
    erp_app.state.store.inventory[("bangna", "SKU-COFFEE")] = 1
    manager = await token(api, "m1", "manager")
    r = await api.post(
        f"/approvals/{approval['id']}/decision", json={"approve": True}, headers=manager
    )
    assert r.json()["status"] == "failed"
    assert "insufficient stock" in r.json()["error"]


async def test_requires_auth(api):
    assert (await api.post("/agent/runs", json={"message": "hi"})).status_code == 401
