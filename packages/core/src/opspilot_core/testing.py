"""Test helpers shared by the services' test suites."""

import httpx


async def token(client: httpx.AsyncClient, user_id: str, role: str, dept: str = "ops") -> dict:
    """Bearer header for a dev user, minted by the service under test."""
    r = await client.post(
        "/auth/dev-token", json={"user_id": user_id, "role": role, "department": dept}
    )
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}
