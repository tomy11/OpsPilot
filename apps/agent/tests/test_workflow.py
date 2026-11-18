"""ApprovalWorkflow against Temporal's time-skipping test server.

The test server binary is downloaded on first use; the tests skip if that is not possible.
"""

import uuid

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from opspilot_agent.worker.workflows import ApprovalInput, ApprovalWorkflow, DecisionSignal

calls: list[tuple[str, str]] = []
fail_mode = {"transient": 0, "permanent": False}


@activity.defn(name="approval.execute")
async def fake_execute(approval_id: str) -> dict:
    calls.append(("execute", approval_id))
    if fail_mode["permanent"]:
        raise ApplicationError("409 insufficient", type="PermanentActionError", non_retryable=True)
    if fail_mode["transient"] > 0:
        fail_mode["transient"] -= 1
        raise RuntimeError("503 upstream")
    return {"ok": True}


@activity.defn(name="approval.mark_failed")
async def fake_mark_failed(approval_id: str, error: str) -> None:
    calls.append(("mark_failed", approval_id))


@activity.defn(name="approval.expire")
async def fake_expire(approval_id: str) -> None:
    calls.append(("expire", approval_id))


@pytest.fixture
async def env():
    try:
        e = await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"temporal test server unavailable: {exc}")
    async with e:
        yield e


@pytest.fixture(autouse=True)
def reset():
    calls.clear()
    fail_mode.update(transient=0, permanent=False)


async def _run(env, signal: DecisionSignal | None, timeout_hours: int = 24) -> str:
    queue = f"q-{uuid.uuid4().hex}"
    async with Worker(
        env.client,
        task_queue=queue,
        workflows=[ApprovalWorkflow],
        activities=[fake_execute, fake_mark_failed, fake_expire],
    ):
        handle = await env.client.start_workflow(
            ApprovalWorkflow.run,
            ApprovalInput("a1", timeout_hours),
            id=f"approval-{uuid.uuid4().hex}",
            task_queue=queue,
        )
        if signal:
            await handle.signal(ApprovalWorkflow.decide, signal)
            await handle.signal(ApprovalWorkflow.decide, signal)  # duplicate is ignored
        return await handle.result()


async def test_approved_executes_once(env):
    assert await _run(env, DecisionSignal(True, "m1")) == "executed"
    assert calls == [("execute", "a1")]


async def test_transient_failures_are_retried(env):
    fail_mode["transient"] = 2
    assert await _run(env, DecisionSignal(True, "m1")) == "executed"
    assert [c[0] for c in calls] == ["execute"] * 3


async def test_permanent_failure_marks_failed(env):
    fail_mode["permanent"] = True
    assert await _run(env, DecisionSignal(True, "m1")) == "failed"
    assert [c[0] for c in calls] == ["execute", "mark_failed"]


async def test_rejected_does_nothing(env):
    assert await _run(env, DecisionSignal(False, "m1")) == "rejected"
    assert calls == []


async def test_no_decision_expires(env):
    assert await _run(env, None, timeout_hours=1) == "expired"
    assert calls == [("expire", "a1")]
