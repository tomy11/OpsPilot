"""Invite-only chat: one-time links, sessions, and credits that cannot be overspent."""

import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select, update

from opspilot_agent import accounts, accounts_cli
from opspilot_agent.api.main import check_chat_settings, create_app
from opspilot_agent.config import AgentSettings
from opspilot_agent.credits import CreditDenied, CreditService, Pricing, pricing_for
from opspilot_agent.models import (
    AgentRun,
    ApprovalRequest,
    ChatAccount,
    ChatInvite,
    ChatSession,
    CreditLedger,
    CreditMonth,
    CreditReservation,
)
from opspilot_core.db import AuditLog, utcnow
from opspilot_core.llm import LLMError, LLMTurn, ToolCall
from opspilot_core.testing import token as dev_token

ASK = {"message": "สวัสดี"}
USAGE = {"input_tokens": 1000, "output_tokens": 500}  # 1000*1.4 + 500*4.4 = 3,600 micro-USD


class PaidLLM:
    """Scripted turns: an LLMTurn to return, an exception to raise, or a coroutine to await."""

    def __init__(self, *script) -> None:
        self.script, self.calls = list(script), 0

    async def respond(self, *, system, messages, tools) -> LLMTurn:
        self.calls += 1
        item = self.script.pop(0) if self.script else answer()
        if isinstance(item, Exception):
            raise item
        return await item() if callable(item) else item


def answer(text="ok", usage=USAGE) -> LLMTurn:
    return LLMTurn(content=[{"type": "text", "text": text}], text=text, usage=dict(usage))


def call(name, args, usage=USAGE) -> LLMTurn:
    block = {"type": "tool_use", "id": f"t-{name}", "name": name, "input": args}
    return LLMTurn(
        content=[block], tool_calls=[ToolCall(block["id"], name, args)], usage=dict(usage)
    )


class Chat:
    def __init__(self, app, llm, settings) -> None:
        self.app, self.llm, self.settings = app, llm, settings
        self.db, self.credits = app.state.core.db, app.state.services.credits

    def browser(self, ip="10.0.0.1") -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app, client=(ip, 1)), base_url="http://agent"
        )

    async def invite(self, credits=500, days=7) -> tuple[str, str]:
        async with self.db.sessionmaker() as s:
            account = await accounts.create_account(s, "Tester")
            if credits:
                await self.credits.grant(s, account.id, credits * 1000, "test")
            link = await accounts.create_invite(s, account.id, days)
            await s.commit()
        return account.id, link

    async def signed_in(self, api, **kw) -> tuple[str, dict]:
        account_id, link = await self.invite(**kw)
        r = await api.post("/session/redeem", json={"token": link})
        assert r.status_code == 200
        return account_id, {"Authorization": f"Bearer {r.json()['access_token']}"}

    async def check_books(self, account_id) -> int:
        """Ledger == balance, and nothing is left on hold. Returns the balance."""
        async with self.db.sessionmaker() as s:
            balance = (await s.get(ChatAccount, account_id)).balance_micro
            ledger = await s.scalar(
                select(func.sum(CreditLedger.amount_micro)).where(
                    CreditLedger.account_id == account_id
                )
            )
            assert await s.get(CreditReservation, account_id) is None
            assert (ledger or 0) == balance >= 0
            return balance


@pytest.fixture
async def chat(settings, erp_transport, knowledge_transport):
    settings.credits_enabled = True
    # Fixed prices, so these tests do not depend on the chosen model's list price.
    settings.credit_price_input_usd, settings.credit_price_output_usd = 1.40, 4.40
    llm = PaidLLM()
    app = create_app(
        settings, erp_transport=erp_transport, knowledge_transport=knowledge_transport, llm=llm
    )
    async with app.router.lifespan_context(app):
        yield Chat(app, llm, settings)


# ---- invite links and sessions ----------------------------------------------------------------


async def test_link_works_once_even_when_redeemed_twice_at_the_same_time(chat):
    _, link = await chat.invite()
    async with chat.browser() as a, chat.browser("10.0.0.2") as b:
        results = await asyncio.gather(
            a.post("/session/redeem", json={"token": link}),
            b.post("/session/redeem", json={"token": link}),
        )
    assert sorted(r.status_code for r in results) == [200, 410]


async def test_expired_replaced_disabled_and_unknown_links_are_refused(chat):
    expired_id, expired = await chat.invite()
    replaced_id, replaced = await chat.invite()
    disabled_id, disabled = await chat.invite()
    async with chat.db.sessionmaker() as s:
        await s.execute(
            update(ChatInvite)
            .where(ChatInvite.account_id == expired_id)
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
        await accounts.create_invite(s, replaced_id, 7)
        await accounts.set_active(s, disabled_id, False)
        await s.commit()
    async with chat.browser() as api:
        for bad in (expired, replaced, disabled, "x" * 43):
            assert (await api.post("/session/redeem", json={"token": bad})).status_code == 410


async def test_guessing_links_is_throttled_per_ip_without_locking_others_out(chat):
    _, link = await chat.invite()
    async with chat.browser("10.9.9.9") as attacker, chat.browser("10.0.0.5") as guest:
        for _ in range(5):
            await attacker.post("/session/redeem", json={"token": "x" * 43})
        blocked = await attacker.post("/session/redeem", json={"token": link})
        assert blocked.status_code == 429
        assert (await guest.post("/session/redeem", json={"token": link})).status_code == 200


async def test_session_cookie_renews_tokens_until_logout(chat):
    async with chat.browser() as api:
        account_id, link = await chat.invite(credits=500)
        r = await api.post("/session/redeem", json={"token": link})
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie and "path=/session" in cookie

        fresh = (await api.post("/session/refresh")).json()["access_token"]
        me = (await api.get("/session/me", headers={"Authorization": f"Bearer {fresh}"})).json()
        assert (me["id"], me["role"], me["invited"], me["credits"]) == (
            account_id,
            "staff",
            True,
            500.0,
        )
        stolen = api.cookies["opspilot_session"]
        assert (await api.post("/session/logout")).status_code == 204
        api.cookies.set("opspilot_session", stolen, path="/session")
        assert (await api.post("/session/refresh")).status_code == 401  # ended on the server


async def test_expired_session_or_inactive_account_cannot_renew(chat):
    async with chat.browser() as api:
        account_id, _ = await chat.signed_in(api)

        async def renew_after(statement) -> int:
            async with chat.db.sessionmaker() as s:
                await s.execute(statement)
                await s.commit()
            return (await api.post("/session/refresh")).status_code

        # The session row stays in place, so each condition is checked on its own.
        mine = update(ChatAccount).where(ChatAccount.id == account_id)
        assert await renew_after(mine.values(active=False)) == 401
        assert await renew_after(mine.values(active=True)) == 200
        past = utcnow() - timedelta(seconds=1)
        assert await renew_after(update(ChatSession).values(expires_at=past)) == 401


async def test_disabled_account_is_stopped_at_once_even_with_a_valid_token(chat):
    async with chat.browser() as api:
        account_id, h = await chat.signed_in(api)
        async with chat.db.sessionmaker() as s:
            await accounts.set_active(s, account_id, False)
            await s.commit()
        assert (await api.post("/agent/runs", json=ASK, headers=h)).status_code == 403
        assert (await api.post("/session/refresh")).status_code == 401
        assert chat.llm.calls == 0


async def test_invited_user_cannot_approve_or_see_other_peoples_requests(chat):
    proposal = {
        "sku": "SKU-COFFEE",
        "qty": 1,
        "from_branch": "bangna",
        "to_branch": "silom",
        "order_id": "ORD-1001",
    }
    chat.llm.script = [call("create_stock_transfer", proposal), answer()]
    async with chat.browser() as a, chat.browser() as b:
        _, ha = await chat.signed_in(a)
        _, hb = await chat.signed_in(b)
        [req] = (await a.post("/agent/runs", json=ASK, headers=ha)).json()["approvals"]
        assert (await b.get(f"/approvals/{req['id']}", headers=hb)).status_code == 404
        r = await a.post(f"/approvals/{req['id']}/decision", json={"approve": True}, headers=ha)
        assert r.status_code == 403
        assert (await a.get("/audit", headers=ha)).status_code == 403


# ---- charging ---------------------------------------------------------------------------------


def test_cost_uses_the_cached_price_and_rounds_up():
    p = Pricing(1.40, 4.40, 0.26)
    assert p.cost(USAGE) == 3600
    assert p.cost({"input_tokens": 0, "cache_read_input_tokens": 1000}) == 260
    assert p.cost({"input_tokens": 0, "cache_creation_input_tokens": 1000}) == 1400
    assert p.cost({"input_tokens": 1}) == 2
    assert p.cost({}) == 0


def test_worst_case_covers_the_real_cost_even_with_nothing_cached():
    p = Pricing(0.15, 0.50, 0.03)
    fully_fresh = {"input_tokens": 5000, "output_tokens": 4000}
    assert p.worst_case(5000, 4000) >= p.cost(fully_fresh)


async def test_run_is_charged_for_real_usage_and_the_rest_is_refunded(chat):
    chat.llm.script = [call("get_order", {"order_id": "ORD-1001"}), answer()]
    async with chat.browser() as api:
        account_id, h = await chat.signed_in(api, credits=500)
        r = (await api.post("/agent/runs", json=ASK, headers=h)).json()
    assert r["credits"] == {"charged": 7.2, "balance": 492.8}  # two calls of 3.6 credits
    assert await chat.check_books(account_id) == 492_800
    async with chat.db.sessionmaker() as s:
        assert (await s.scalars(select(CreditMonth.committed_micro))).one() == 7_200


async def test_too_little_credit_never_reaches_the_model(chat):
    async with chat.browser() as api:
        account_id, h = await chat.signed_in(api, credits=1)
        r = await api.post("/agent/runs", json=ASK, headers=h)
    assert r.status_code == 402 and chat.llm.calls == 0
    assert await chat.check_books(account_id) == 1_000


async def test_run_stops_before_a_call_the_reservation_cannot_cover(chat):
    big = {"input_tokens": 1000, "output_tokens": 4000}  # 19 credits per call
    chat.llm.script = [
        call("check_stock", {"sku": "SKU-COFFEE", "branch": "all"}, big) for _ in range(8)
    ]
    chat.settings.credit_max_per_run = 60
    async with chat.browser() as api:
        account_id, h = await chat.signed_in(api, credits=500)
        r = (await api.post("/agent/runs", json=ASK, headers=h)).json()
    assert r["stopped_reason"] == "credit_limit" and "เครดิต" in r["answer"]
    assert 1 <= chat.llm.calls < 8 and r["credits"]["charged"] <= 60
    assert await chat.check_books(account_id) == 500_000 - chat.llm.calls * 19_000


async def test_truncated_answer_is_charged_but_an_outage_is_not(chat):
    chat.llm.script = [LLMError("truncated", usage=USAGE), LLMError("connection error")]
    async with chat.browser() as api:
        account_id, h = await chat.signed_in(api)
        first = (await api.post("/agent/runs", json=ASK, headers=h)).json()
        second = (await api.post("/agent/runs", json=ASK, headers=h)).json()
    assert first["credits"]["charged"] == 3.6 and second["credits"]["charged"] == 0
    assert await chat.check_books(account_id) == 496_400


async def test_timeout_keeps_the_spend_already_made_and_saves_no_half_made_request(chat):
    async def hang():
        await asyncio.sleep(30)

    proposal = {
        "sku": "SKU-COFFEE",
        "qty": 1,
        "from_branch": "bangna",
        "to_branch": "silom",
        "order_id": "ORD-1001",
    }
    chat.llm.script = [call("create_stock_transfer", proposal), hang]
    chat.settings.agent_run_timeout_s = 0.5
    async with chat.browser() as api:
        account_id, h = await chat.signed_in(api)
        r = await api.post("/agent/runs", json=ASK, headers=h)
    assert r.status_code == 504
    assert await chat.check_books(account_id) == 496_400
    async with chat.db.sessionmaker() as s:
        assert (await s.scalars(select(ApprovalRequest))).all() == []
        assert (await s.scalars(select(AgentRun))).one().answer is None


async def test_unexpected_crash_settles_too(chat):
    chat.llm.script = [
        call("get_order", {"order_id": "ORD-1001"}),
        RuntimeError("bug"),
    ]
    async with chat.browser() as api:
        account_id, h = await chat.signed_in(api)
        with pytest.raises(RuntimeError):
            await api.post("/agent/runs", json=ASK, headers=h)
    assert await chat.check_books(account_id) == 496_400


async def test_one_run_at_a_time_per_account(chat):
    started, gate = asyncio.Event(), asyncio.Event()

    async def slow():
        started.set()
        await gate.wait()
        return answer()

    chat.llm.script = [slow]
    async with chat.browser() as api:
        account_id, h = await chat.signed_in(api)
        first = asyncio.create_task(api.post("/agent/runs", json=ASK, headers=h))
        await started.wait()
        assert (await api.post("/agent/runs", json=ASK, headers=h)).status_code == 409
        gate.set()
        assert (await first).status_code == 200
    assert chat.llm.calls == 1
    assert await chat.check_books(account_id) == 496_400


async def test_reservation_left_by_a_crashed_server_is_settled_with_recorded_spend(chat):
    account_id, _ = await chat.invite(credits=500)
    hold = await chat.credits.reserve(account_id, "r1", 1)
    await chat.credits.record_spend(hold, 5_000)
    with pytest.raises(CreditDenied) as busy:
        await chat.credits.reserve(account_id, "r2", 1)
    assert busy.value.status == 409  # still fresh: the run may be alive

    async with chat.db.sessionmaker() as s:
        old = utcnow() - chat.credits.stale_after - timedelta(seconds=1)
        await s.execute(update(CreditReservation).values(created_at=old))
        await s.commit()
    again = await chat.credits.reserve(account_id, "r3", 1)
    assert await chat.credits.settle(again) == 0
    assert await chat.credits.settle(again) == 0  # settling twice refunds once
    assert await chat.credits.settle(hold) == 0
    assert await chat.check_books(account_id) == 495_000


async def test_two_settlements_racing_on_one_reservation_refund_once(chat):
    account_id, _ = await chat.invite(credits=500)
    hold = await chat.credits.reserve(account_id, "r1", 1)
    async with chat.db.sessionmaker() as slow:
        row = await slow.get(CreditReservation, account_id)  # read before the other one commits
        slow.expunge(row)  # keep its values past the rollback
        await slow.rollback()
        assert await chat.credits.settle(hold) == 0
        assert await chat.credits._settle(slow, row) == 0
        await slow.commit()
    assert await chat.check_books(account_id) == 500_000


async def test_daily_message_limit(chat):
    chat.settings.credit_daily_runs = 2
    async with chat.browser() as api:
        account_id, h = await chat.signed_in(api)
        codes = [(await api.post("/agent/runs", json=ASK, headers=h)).status_code for _ in range(3)]
    assert codes == [200, 200, 429] and chat.llm.calls == 2
    await chat.check_books(account_id)


async def test_monthly_budget_covers_all_accounts_together(chat):
    chat.settings.credit_monthly_budget_usd = 0.2  # one 150-credit hold fits, two do not
    a, _ = await chat.invite(credits=500)
    b, _ = await chat.invite(credits=500)
    results = await asyncio.gather(
        chat.credits.reserve(a, "ra", 1), chat.credits.reserve(b, "rb", 1), return_exceptions=True
    )
    denied = [r for r in results if isinstance(r, CreditDenied)]
    holds = [r for r in results if not isinstance(r, Exception)]
    assert len(holds) == 1 and [d.status for d in denied] == [503]
    await chat.credits.settle(holds[0])
    for account_id in (a, b):
        assert await chat.check_books(account_id) == 500_000


async def test_shared_demo_accounts_cannot_spend_when_credits_are_on(chat):
    async with chat.browser() as api:
        h = await dev_token(api, "demo-staff", "staff")
        assert (await api.post("/agent/runs", json=ASK, headers=h)).status_code == 403
    assert chat.llm.calls == 0


# ---- operator command line and startup checks -------------------------------------------------


async def test_cli_invites_tops_up_and_disables(chat, capsys):
    async def cli(*argv) -> list[str]:
        assert await accounts_cli.run(list(argv), chat.settings) == 0
        return capsys.readouterr().out.split()

    account_id, url = await cli(
        "invite", "--label", "Nok", "--credits", "100", "--base-url", "https://x.example"
    )
    prefix = "https://x.example/#invite="
    assert url.startswith(prefix)
    await cli("grant", account_id, "--credits", "50")
    async with chat.browser() as api:
        assert (
            await api.post("/session/redeem", json={"token": url.removeprefix(prefix)})
        ).status_code == 200
        _, second = await cli("link", account_id)  # another device
        link = second.split("#invite=")[1]
        assert (await api.post("/session/redeem", json={"token": link})).status_code == 200
    assert await chat.check_books(account_id) == 150_000
    await cli("disable", account_id)
    assert "disabled" in await cli("list")
    assert await accounts_cli.run(["grant", "nobody", "--credits", "1"], chat.settings) == 1
    async with chat.db.sessionmaker() as s:
        rows = (await s.scalars(select(AuditLog).where(AuditLog.target == account_id))).all()
        assert url.removeprefix(prefix) not in str([r.detail for r in rows])


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("glm-5.3-flash:cloud", Pricing(0.15, 0.50, 0.03)),
        ("glm-5.3-flash", Pricing(0.15, 0.50, 0.03)),
        ("glm-5.2", Pricing(1.40, 4.40, 0.26)),
    ],
)
def test_price_follows_the_model(model, expected):
    assert pricing_for(AgentSettings(_env_file=None, ollama_model=model)) == expected


def test_unknown_model_needs_prices_before_anyone_is_charged():
    unknown = {"_env_file": None, "ollama_model": "mystery:cloud", "credits_enabled": True}
    with pytest.raises(ValueError, match="CREDIT_PRICE"):
        check_chat_settings(AgentSettings(**unknown))
    priced = AgentSettings(**unknown, credit_price_input_usd=1, credit_price_output_usd=2)
    check_chat_settings(priced)
    assert pricing_for(priced) == Pricing(1, 2, 1)  # cached defaults to the full input price
    priced.credit_price_cached_usd = 0.0
    assert pricing_for(priced) == Pricing(1, 2, 0.0)


def test_paid_provider_needs_credits_in_prod():
    def s(**kw):
        return AgentSettings(_env_file=None, **kw)

    with pytest.raises(ValueError, match="CREDITS_ENABLED"):
        check_chat_settings(s(app_env="prod", agent_llm_provider="ollama"))
    with pytest.raises(ValueError, match="prices"):
        check_chat_settings(s(llm_provider="anthropic", credits_enabled=True))
    check_chat_settings(s(app_env="prod", agent_llm_provider="ollama", credits_enabled=True))
    assert s(llm_provider="anthropic", agent_llm_provider="ollama").provider == "ollama"
    assert isinstance(CreditService(None, s()).pricing, Pricing)
