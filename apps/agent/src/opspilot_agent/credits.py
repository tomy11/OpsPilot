"""Credits for invite-only chat: reserve before a run, charge real usage, refund the rest.

Money is integer micro-USD (1 credit = 1,000). Three limits apply to every run:
- the account balance (conditional UPDATE, so it can never go negative);
- one open reservation per account (primary key), i.e. one run at a time;
- a budget for the whole installation per calendar month.

`MeteredLLM` checks the worst case of each model call against the reservation *before* sending
it, so a run stops instead of overspending. A reservation left behind by a crash is settled with
the spend recorded so far, the next time that account sends a message.
"""

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot_agent.config import AgentSettings
from opspilot_agent.models import ChatAccount, CreditLedger, CreditMonth, CreditReservation
from opspilot_core.db import Database, new_id, utcnow
from opspilot_core.llm import LLMClient, LLMError, LLMTurn

MICRO_PER_CREDIT = 1_000
# USD per 1M input / output / cached input tokens, https://ollama.com/pricing on 2026-11-24.
LIST_PRICES = {
    "glm-5.2": (1.40, 4.40, 0.26),
    "glm-5.3": (1.40, 4.40, 0.26),
    "glm-5.3-flash": (0.15, 0.50, 0.03),
}
BANGKOK = timezone(timedelta(hours=7))  # days and months follow the users' clock
STALE_GRACE = timedelta(minutes=2)


class CreditDenied(Exception):
    """A run may not start. `status` is the HTTP status the API answers with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class CreditExhausted(LLMError):
    """The next model call could exceed what this run reserved."""


def to_credits(micro: int) -> float:
    return round(micro / MICRO_PER_CREDIT, 1)


@dataclass(frozen=True)
class Pricing:
    input_usd: float  # per 1M tokens, which is the same number as micro-USD per token
    output_usd: float
    cached_usd: float

    def cost(self, usage: dict[str, Any]) -> int:
        # `input_tokens` does not include tokens read from the cache.
        def count(key: str) -> int:
            return int(usage.get(key) or 0)

        return math.ceil(
            (count("input_tokens") + count("cache_creation_input_tokens")) * self.input_usd
            + count("cache_read_input_tokens") * self.cached_usd
            + count("output_tokens") * self.output_usd
        )

    def worst_case(self, payload_bytes: int, max_tokens: int) -> int:
        # Assumes nothing is cached, so it always covers the real cost.
        # A byte-level tokenizer never produces more tokens than UTF-8 bytes.
        return math.ceil(payload_bytes * self.input_usd + max_tokens * self.output_usd)


def pricing_for(s: AgentSettings) -> Pricing:
    """Configured prices, else the list price. An unknown model must not be charged by guess."""
    if s.credit_price_input_usd is not None and s.credit_price_output_usd is not None:
        cached = s.credit_price_cached_usd
        return Pricing(
            s.credit_price_input_usd,
            s.credit_price_output_usd,
            s.credit_price_input_usd if cached is None else cached,
        )
    listed = LIST_PRICES.get(s.ollama_model.removesuffix(":cloud"))
    if listed is None:
        raise ValueError(
            f"no list price for {s.ollama_model}: set CREDIT_PRICE_INPUT_USD and "
            "CREDIT_PRICE_OUTPUT_USD"
        )
    return Pricing(*listed)


def payload_bytes(system: str, messages: list[Any], tools: list[Any]) -> int:
    return len(json.dumps([system, messages, tools], ensure_ascii=False, default=str).encode())


@dataclass
class Hold:
    id: str
    account_id: str
    amount: int


class CreditService:
    def __init__(self, db: Database, settings: AgentSettings) -> None:
        self.db, self.s = db, settings
        self.pricing = pricing_for(settings)

    @property
    def stale_after(self) -> timedelta:
        return timedelta(seconds=self.s.agent_run_timeout_s) + STALE_GRACE

    def first_call_cost(self, system: str, message: str, tools: list[Any]) -> int:
        size = payload_bytes(system, [{"role": "user", "content": message}], tools)
        return self.pricing.worst_case(size, self.s.ollama_max_tokens)

    async def reserve(self, account_id: str, run_id: str, minimum: int) -> Hold:
        async with self.db.sessionmaker() as s:
            stale = await s.get(CreditReservation, account_id)
            if stale is not None and _aware(stale.created_at) < utcnow() - self.stale_after:
                await self._settle(s, stale)
                await s.commit()

        async with self.db.sessionmaker() as s:
            account = await s.get(ChatAccount, account_id)
            if account is None or not account.active:
                raise CreditDenied(403, "บัญชีนี้ไม่ได้รับอนุญาตให้ใช้แชต")
            if await self._runs_today(s, account_id) >= self.s.credit_daily_runs:
                raise CreditDenied(429, "ใช้ครบจำนวนข้อความของวันนี้แล้ว")
            amount = min(account.balance_micro, self.s.credit_max_per_run * MICRO_PER_CREDIT)
            if amount < minimum:
                raise CreditDenied(402, "เครดิตคงเหลือไม่พอสำหรับข้อความนี้")

            month = datetime.now(UTC).astimezone(BANGKOK).strftime("%Y-%m")
            hold = CreditReservation(
                account_id=account_id, id=new_id(), run_id=run_id, month=month, amount_micro=amount
            )
            s.add(hold)
            try:
                await s.flush()
            except IntegrityError as exc:
                raise CreditDenied(409, "ข้อความก่อนหน้ายังประมวลผลไม่เสร็จ") from exc
            await self._commit_month(s, month, amount)
            taken = await s.execute(
                update(ChatAccount)
                .where(
                    ChatAccount.id == account_id,
                    ChatAccount.active.is_(True),
                    ChatAccount.balance_micro >= amount,
                )
                .values(balance_micro=ChatAccount.balance_micro - amount)
            )
            if taken.rowcount != 1:
                raise CreditDenied(409, "ยอดเครดิตเปลี่ยนระหว่างดำเนินการ กรุณาลองใหม่")
            await _ledger(s, account_id, "reserve", -amount, run_id=run_id)
            await s.commit()
            return Hold(hold.id, account_id, amount)

    async def _commit_month(self, s: AsyncSession, month: str, amount: int) -> None:
        budget = int(self.s.credit_monthly_budget_usd * 1_000_000)
        if await s.get(CreditMonth, month) is None:
            try:
                async with s.begin_nested():
                    s.add(CreditMonth(month=month, committed_micro=0))
            except IntegrityError:
                pass  # another run opened the month first
        ok = await s.execute(
            update(CreditMonth)
            .where(CreditMonth.month == month, CreditMonth.committed_micro + amount <= budget)
            .values(committed_micro=CreditMonth.committed_micro + amount)
        )
        if ok.rowcount != 1:
            raise CreditDenied(503, "งบรวมของเดือนนี้หมดแล้ว กรุณาติดต่อผู้ดูแล")

    async def record_spend(self, hold: Hold, spent: int) -> None:
        """Persist progress after each model call, so a crash cannot lose what was billed."""
        async with self.db.sessionmaker() as s:
            await s.execute(
                update(CreditReservation)
                .where(CreditReservation.id == hold.id)
                .values(spent_micro=spent)
            )
            await s.commit()

    async def settle(self, hold: Hold, detail: dict[str, Any] | None = None) -> int:
        """Charge what was spent, refund the rest. Returns the charge. Safe to call twice."""
        async with self.db.sessionmaker() as s:
            row = await s.scalar(select(CreditReservation).where(CreditReservation.id == hold.id))
            if row is None:
                return 0
            charged = await self._settle(s, row, detail)
            await s.commit()
            return charged

    async def _settle(
        self, s: AsyncSession, row: CreditReservation, detail: dict[str, Any] | None = None
    ) -> int:
        gone = await s.execute(delete(CreditReservation).where(CreditReservation.id == row.id))
        if gone.rowcount != 1:
            return 0  # someone else settled it
        charged = min(row.spent_micro, row.amount_micro)
        refund = row.amount_micro - charged
        await s.execute(
            update(ChatAccount)
            .where(ChatAccount.id == row.account_id)
            .values(balance_micro=ChatAccount.balance_micro + refund)
        )
        await s.execute(
            update(CreditMonth)
            .where(CreditMonth.month == row.month)
            .values(committed_micro=CreditMonth.committed_micro - refund)
        )
        await _ledger(
            s,
            row.account_id,
            "settle",
            refund,
            run_id=row.run_id,
            detail={"charged_micro": charged, "spent_micro": row.spent_micro, **(detail or {})},
        )
        return charged

    async def grant(self, s: AsyncSession, account_id: str, micro: int, note: str) -> None:
        await s.execute(
            update(ChatAccount)
            .where(ChatAccount.id == account_id)
            .values(balance_micro=ChatAccount.balance_micro + micro)
        )
        await _ledger(s, account_id, "grant", micro, detail={"note": note})

    async def balance(self, account_id: str) -> int | None:
        async with self.db.sessionmaker() as s:
            account = await s.get(ChatAccount, account_id)
            return account.balance_micro if account is not None else None

    async def _runs_today(self, s: AsyncSession, account_id: str) -> int:
        midnight = datetime.now(BANGKOK).replace(hour=0, minute=0, second=0, microsecond=0)
        return (
            await s.scalar(
                select(func.count())
                .select_from(CreditLedger)
                .where(
                    CreditLedger.account_id == account_id,
                    CreditLedger.kind == "reserve",
                    CreditLedger.created_at >= midnight.astimezone(UTC),
                )
            )
            or 0
        )


class MeteredLLM:
    """Wraps the model for one run: refuses a call the reservation cannot cover, counts the rest."""

    def __init__(
        self, inner: LLMClient, credits: CreditService, hold: Hold, max_tokens: int
    ) -> None:
        self.inner, self.credits, self.hold, self.max_tokens = inner, credits, hold, max_tokens
        self.spent = 0
        self.calls = 0
        self.unmetered_calls = 0

    async def respond(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMTurn:
        pricing = self.credits.pricing
        worst = pricing.worst_case(payload_bytes(system, messages, tools), self.max_tokens)
        if self.spent + worst > self.hold.amount:
            raise CreditExhausted("credit limit for this run reached")
        self.calls += 1
        try:
            turn = await self.inner.respond(system=system, messages=messages, tools=tools)
        except LLMError as exc:
            if not exc.usage:
                self.unmetered_calls += 1  # no usage came back: not charged to the user
            await self._add(exc.usage)
            raise
        await self._add(turn.usage)
        return turn

    async def _add(self, usage: dict[str, Any]) -> None:
        cost = self.credits.pricing.cost(usage)
        if cost:
            self.spent += cost
            await self.credits.record_spend(self.hold, self.spent)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _ledger(
    s: AsyncSession,
    account_id: str,
    kind: str,
    amount: int,
    *,
    run_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    s.add(
        CreditLedger(
            account_id=account_id,
            kind=kind,
            amount_micro=amount,
            run_id=run_id,
            detail=detail or {},
        )
    )
    await s.flush()
