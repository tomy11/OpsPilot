"""Temporal worker: `python -m aiw.worker.main`."""

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from aiw.agent.erp_client import ErpClient
from aiw.agent.sop import SopIndex
from aiw.agent.tools import ToolContext
from aiw.core.config import get_settings
from aiw.core.db import Database
from aiw.worker.activities import ApprovalActivities
from aiw.worker.workflows import ApprovalWorkflow


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    s = get_settings()
    db = Database(s.database_url)
    await db.create_all()
    erp = ErpClient(s.erp_base_url, timeout_s=s.erp_timeout_s)
    acts = ApprovalActivities(db, ToolContext(erp=erp, sop=SopIndex()))
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
