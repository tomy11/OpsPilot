"""Invite-only accounts. An invite link works once; the browser then keeps a session cookie.

Accounts are always `staff`: invited people can chat and propose, never approve. The only way to
create, top up or disable an account is the operator command line (`opspilot_agent.accounts_cli`).
"""

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_agent.models import ChatAccount, ChatInvite, ChatSession
from opspilot_core.audit import record_audit
from opspilot_core.auth import Role, User
from opspilot_core.db import utcnow

ACCOUNT_PREFIX = "invite-"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def to_user(account: ChatAccount) -> User:
    return User(id=account.id, role=Role.STAFF, department=account.department)


async def create_account(s: AsyncSession, label: str) -> ChatAccount:
    account = ChatAccount(
        id=ACCOUNT_PREFIX + secrets.token_hex(8),
        label=label,
        department="ops",
        active=True,
        balance_micro=0,
    )
    s.add(account)
    await s.flush()
    await _audit(s, "account.created", account.id)
    return account


async def create_invite(s: AsyncSession, account_id: str, days: int) -> str:
    """A new link replaces any unused one, so only the latest link can be redeemed."""
    await s.execute(
        delete(ChatInvite).where(ChatInvite.account_id == account_id, ChatInvite.used_at.is_(None))
    )
    token = secrets.token_urlsafe(32)
    s.add(
        ChatInvite(
            token_hash=hash_token(token),
            account_id=account_id,
            expires_at=utcnow() + timedelta(days=days),
        )
    )
    await s.flush()
    await _audit(s, "account.invited", account_id)
    return token


async def redeem(s: AsyncSession, token: str, session_days: int) -> tuple[ChatAccount, str] | None:
    digest = hash_token(token)
    used = await s.execute(
        update(ChatInvite)
        .where(
            ChatInvite.token_hash == digest,
            ChatInvite.used_at.is_(None),
            ChatInvite.expires_at > utcnow(),
            ChatInvite.account_id.in_(select(ChatAccount.id).where(ChatAccount.active.is_(True))),
        )
        .values(used_at=utcnow())
    )
    if used.rowcount != 1:
        return None
    invite = await s.get(ChatInvite, digest)
    account = await s.get(ChatAccount, invite.account_id)
    session_token = secrets.token_urlsafe(32)
    s.add(
        ChatSession(
            token_hash=hash_token(session_token),
            account_id=account.id,
            expires_at=utcnow() + timedelta(days=session_days),
        )
    )
    await _audit(s, "account.redeemed", account.id, actor=account.id)
    return account, session_token


async def resume(s: AsyncSession, session_token: str) -> ChatAccount | None:
    return await s.scalar(
        select(ChatAccount)
        .join(ChatSession, ChatSession.account_id == ChatAccount.id)
        .where(
            ChatSession.token_hash == hash_token(session_token),
            ChatSession.expires_at > utcnow(),
            ChatAccount.active.is_(True),
        )
    )


async def end_session(s: AsyncSession, session_token: str) -> None:
    await s.execute(delete(ChatSession).where(ChatSession.token_hash == hash_token(session_token)))


async def set_active(s: AsyncSession, account_id: str, active: bool) -> None:
    await s.execute(update(ChatAccount).where(ChatAccount.id == account_id).values(active=active))
    if not active:
        await s.execute(delete(ChatSession).where(ChatSession.account_id == account_id))
        await s.execute(delete(ChatInvite).where(ChatInvite.account_id == account_id))
    await _audit(s, "account.enabled" if active else "account.disabled", account_id)


async def _audit(s: AsyncSession, action: str, target: str, actor: str = "operator") -> None:
    await record_audit(s, actor=actor, action=action, status="ok", target=target)
