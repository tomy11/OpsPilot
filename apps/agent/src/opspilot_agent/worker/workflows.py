"""Durable approval workflow.

One workflow per approval request: wait (possibly for hours) for a human decision signal,
then execute the approved ERP action with retries. Survives worker restarts.
"""

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from opspilot_agent.worker.activities import ApprovalActivities


@dataclass
class ApprovalInput:
    approval_id: str
    timeout_hours: int = 24


@dataclass
class DecisionSignal:
    approve: bool
    approver_id: str
    reason: str | None = None


EXECUTE_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=6,
    non_retryable_error_types=["PermanentActionError"],
)


@workflow.defn
class ApprovalWorkflow:
    def __init__(self) -> None:
        self._decision: DecisionSignal | None = None
        self._state = "pending"

    @workflow.signal
    def decide(self, decision: DecisionSignal) -> None:
        if self._decision is None:  # first decision wins; duplicates are ignored
            self._decision = decision

    @workflow.query
    def state(self) -> str:
        return self._state

    @workflow.run
    async def run(self, inp: ApprovalInput) -> str:
        try:
            await workflow.wait_condition(
                lambda: self._decision is not None,
                timeout=timedelta(hours=inp.timeout_hours),
            )
        except TimeoutError:  # asyncio.TimeoutError is an alias on Python 3.11+
            await workflow.execute_activity_method(
                ApprovalActivities.expire,
                inp.approval_id,
                start_to_close_timeout=timedelta(seconds=30),
            )
            self._state = "expired"
            return self._state

        assert self._decision is not None
        if not self._decision.approve:
            self._state = "rejected"
            return self._state

        self._state = "executing"
        try:
            await workflow.execute_activity_method(
                ApprovalActivities.execute,
                inp.approval_id,
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=EXECUTE_RETRY,
            )
        except ActivityError as exc:
            await workflow.execute_activity_method(
                ApprovalActivities.mark_failed,
                args=[inp.approval_id, str(exc.cause or exc)],
                start_to_close_timeout=timedelta(seconds=30),
            )
            self._state = "failed"
            return self._state

        self._state = "executed"
        return self._state
