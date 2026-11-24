"""Agent-owned tables: agent runs and approval requests."""

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from opspilot_core.db import Base, new_id, utcnow


class ApprovalStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"
    FAILED = "failed"


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    department: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    answer: Mapped[str | None] = mapped_column(Text)
    steps: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    tool: Mapped[str] = mapped_column(String(64))
    args: Mapped[dict[str, Any]] = mapped_column(default=dict)
    summary: Mapped[str] = mapped_column(Text)
    requested_by: Mapped[str] = mapped_column(String(64))
    department: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus, native_enum=False), default=ApprovalStatus.PENDING, index=True
    )
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    workflow_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ApprovalDelivery(Base):
    __tablename__ = "approval_deliveries"

    approval_id: Mapped[str] = mapped_column(ForeignKey("approval_requests.id"), primary_key=True)
    next_attempt: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class RunSandbox(Base):
    __tablename__ = "run_sandboxes"

    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), primary_key=True)


# ---- invite-only chat: accounts, invites, sessions, credits -------------------------------------
# Secrets (invite and session tokens) are stored only as SHA-256 hashes.


class ChatAccount(Base):
    __tablename__ = "chat_accounts"
    __table_args__ = (CheckConstraint("balance_micro >= 0", name="chat_balance_not_negative"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(128))
    department: Mapped[str] = mapped_column(String(64), default="ops")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    balance_micro: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatInvite(Base):
    __tablename__ = "chat_invites"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("chat_accounts.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("chat_accounts.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CreditReservation(Base):
    __tablename__ = "credit_reservations"

    # One open reservation per account: a second concurrent run hits the primary key.
    account_id: Mapped[str] = mapped_column(ForeignKey("chat_accounts.id"), primary_key=True)
    id: Mapped[str] = mapped_column(String(32), unique=True)
    run_id: Mapped[str] = mapped_column(String(32))
    month: Mapped[str] = mapped_column(String(7))
    amount_micro: Mapped[int] = mapped_column(BigInteger)
    spent_micro: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CreditLedger(Base):
    """Append-only. The sum of an account's rows always equals its balance."""

    __tablename__ = "credit_ledger"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey("chat_accounts.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # grant | reserve | settle
    amount_micro: Mapped[int] = mapped_column(BigInteger)
    run_id: Mapped[str | None] = mapped_column(String(32))
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class CreditMonth(Base):
    __tablename__ = "credit_months"

    month: Mapped[str] = mapped_column(String(7), primary_key=True)  # YYYY-MM, Bangkok time
    committed_micro: Mapped[int] = mapped_column(BigInteger, default=0)
