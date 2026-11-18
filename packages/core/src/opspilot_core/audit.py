import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_core.db import AuditLog

# Minimal PII masking for audit payloads (PDPA). Extend per data classification policy.
_PII_PATTERNS = [
    (re.compile(r"\b\d{13}\b"), "[NATIONAL_ID]"),
    (re.compile(r"\b0\d{8,9}\b"), "[PHONE]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[EMAIL]"),
]


def mask_pii(value: Any) -> Any:
    if isinstance(value, str):
        for pattern, repl in _PII_PATTERNS:
            value = pattern.sub(repl, value)
        return value
    if isinstance(value, dict):
        return {k: mask_pii(v) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_pii(v) for v in value]
    return value


async def record_audit(
    session: AsyncSession,
    *,
    actor: str,
    action: str,
    status: str,
    target: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor=actor, action=action, status=status, target=target, detail=mask_pii(detail or {})
    )
    session.add(entry)
    await session.flush()
    return entry
