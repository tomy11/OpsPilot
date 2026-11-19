"""Verify the Gateway → Agent API → Temporal → ERP flow on a seeded staging instance."""

import argparse
import json
import os
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

TRANSFER = "เช็ค ORD-1001 ว่าของพอไหม ถ้าไม่พอให้โอนสต็อกจาก bangna"
TRANSFER_EN = "Check stock for ORD-1001 and request a transfer from bangna for any shortage."


def run(base_url: str, staff_password: str, manager_password: str) -> None:
    def call(path, *, token=None, body=None, expected=200):
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body).encode()
        try:
            with urlopen(Request(base_url + path, data=payload, headers=headers), timeout=30) as r:
                status, content = r.status, r.read()
        except HTTPError as exc:
            status, content = exc.code, exc.read()
        if status != expected:
            raise RuntimeError(f"{path} returned {status}, expected {expected}")
        return json.loads(content) if status == 200 and path != "/" else None

    def login(username, password):
        return call("/session/login", body={"username": username, "password": password})[
            "access_token"
        ]

    def wait_for(path, token, predicate):
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            row = call(path, token=token)
            if row["status"] in ("failed", "expired", "rejected"):
                raise RuntimeError(f"{path} reached {row['status']}")
            if predicate(row):
                return row
            time.sleep(0.75)
        raise TimeoutError(path)

    def inventory(path, token):
        return {
            row["branch"]: row["on_hand"] for row in call(path + "/inventory", token=token)["stock"]
        }

    call("/")
    call("/ready")
    call("/auth/dev-token", body={}, expected=404)
    call("/_admin/reset", body={}, expected=404)
    call("/approvals", expected=401)
    staff = login("staff", staff_password)
    manager = login("manager", manager_password)
    assert call("/session/me", token=staff)["approval_backend"] == "temporal"

    def propose(message):
        result = call("/agent/runs", token=staff, body={"message": message})
        approvals = result["approvals"]
        assert len(approvals) == 1 and approvals[0]["args"]["qty"] == 7
        return "/approvals/" + approvals[0]["id"]

    approval = propose(TRANSFER)
    assert inventory(approval, staff) == {"silom": 3, "bangna": 50}
    call(approval + "/decision", token=staff, body={"approve": True}, expected=403)
    wait_for(approval, manager, lambda row: row["workflow_id"] is not None)
    call(approval + "/decision", token=manager, body={"approve": True, "reason": "Staging smoke"})
    row = wait_for(approval, manager, lambda row: row["status"] == "executed")
    assert row["result"]["qty"] == 7
    call(approval + "/decision", token=manager, body={"approve": True}, expected=409)
    assert inventory(approval, manager) == {"silom": 10, "bangna": 43}
    audit = call("/audit?target=" + row["id"], token=manager)
    actions = [event["action"] for event in audit]
    assert actions.count("approval.executed") == 1
    assert {"approval.requested", "approval.decide", "approval.approved"} <= set(actions)

    fresh = propose(TRANSFER_EN)
    assert inventory(fresh, manager) == {"silom": 3, "bangna": 50}
    wait_for(fresh, manager, lambda row: row["workflow_id"] is not None)
    rejected = call(
        fresh + "/decision", token=manager, body={"approve": False, "reason": "Staging smoke"}
    )
    assert rejected["status"] == "rejected"
    assert inventory(fresh, manager) == {"silom": 3, "bangna": 50}
    print(
        "Staging passed: authentication, approval, stock write, duplicate protection, audit and repeatable runs."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url")
    args = parser.parse_args()
    run(
        args.base_url.rstrip("/"),
        os.environ["DEMO_STAFF_PASSWORD"],
        os.environ["DEMO_MANAGER_PASSWORD"],
    )
