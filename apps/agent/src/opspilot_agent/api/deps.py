from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request

from opspilot_agent.api.orchestrator import ApprovalOrchestrator
from opspilot_agent.config import AgentSettings
from opspilot_agent.tools import ToolContext
from opspilot_core.db import Database
from opspilot_core.llm import LLMClient


@dataclass
class Services:
    settings: AgentSettings
    db: Database
    tools: ToolContext
    llm: LLMClient
    orchestrator: ApprovalOrchestrator


def services(request: Request) -> Services:
    return request.app.state.services


ServicesDep = Annotated[Services, Depends(services)]
