"""Temporal worker: `python -m opspilot_agent.worker.main`."""

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from opspilot_agent import models  # noqa: F401 - register tables before create_all
from opspilot_agent.config import get_settings
from opspilot_agent.erp_client import ErpClient
from opspilot_agent.tools import ToolContext
from opspilot_agent.worker.activities import ApprovalActivities
from opspilot_agent.worker.workflows import ApprovalWorkflow
from opspilot_core.db import Database


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    s = get_settings()
    db = Database(s.database_url)
    await db.create_all()
    erp = ErpClient(s.erp_base_url, timeout_s=s.erp_timeout_s)
    acts = ApprovalActivities(db, ToolContext(erp=erp))
    client = await Client.connect(s.temporal_host)
    worker = Worker(
        client,
        task_queue=s.temporal_task_queue,
        workflows=[ApprovalWorkflow],
        activities=[acts.execute, acts.mark_failed, acts.expire],
    )
    try:
        await worker.run()
    finally:
        await erp.aclose()
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
