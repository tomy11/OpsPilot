"""Operator commands for invite-only chat: `python -m opspilot_agent.accounts_cli --help`.

invite --label "Name" --credits 500     create an account and print its one-time link
link ACCOUNT                            new one-time link (new device, or the first one expired)
grant ACCOUNT --credits 200             top up
disable ACCOUNT / enable ACCOUNT        block or restore; disabling ends every session
list                                    accounts, balances and status
"""

import argparse
import asyncio
import sys

from sqlalchemy import select

from opspilot_agent.accounts import create_account, create_invite, set_active
from opspilot_agent.config import AgentSettings, get_settings
from opspilot_agent.credits import MICRO_PER_CREDIT, CreditService, to_credits
from opspilot_agent.models import ChatAccount
from opspilot_core.db import Database


def _positive(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="accounts_cli", description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = p.add_subparsers(dest="command", required=True)
    invite = sub.add_parser("invite")
    invite.add_argument("--label", required=True)
    invite.add_argument("--credits", type=_positive, required=True)
    link = sub.add_parser("link")
    for cmd in (invite, link):
        cmd.add_argument("--days", type=_positive, default=7)
        cmd.add_argument("--base-url", default="http://localhost:8000")
    grant = sub.add_parser("grant")
    grant.add_argument("--credits", type=_positive, required=True)
    for cmd in (link, grant, sub.add_parser("disable"), sub.add_parser("enable")):
        cmd.add_argument("account")
    sub.add_parser("list")
    return p


async def run(argv: list[str], settings: AgentSettings) -> int:
    args = _parser().parse_args(argv)
    db = Database(settings.database_url)
    try:
        await db.create_all()
        credits = CreditService(db, settings)
        async with db.sessionmaker() as s:
            if args.command == "list":
                for a in await s.scalars(select(ChatAccount).order_by(ChatAccount.created_at)):
                    state = "active" if a.active else "disabled"
                    print(f"{a.id}  {to_credits(a.balance_micro):>10}  {state:8}  {a.label}")
                return 0
            if args.command == "invite":
                account = await create_account(s, args.label)
                await credits.grant(s, account.id, args.credits * MICRO_PER_CREDIT, "invite")
            else:
                account = await s.get(ChatAccount, args.account)
                if account is None:
                    print(f"no such account: {args.account}", file=sys.stderr)
                    return 1
            if args.command in ("invite", "link"):
                if not account.active:
                    print("account is disabled; enable it first", file=sys.stderr)
                    return 1
                token = await create_invite(s, account.id, args.days)
            elif args.command == "grant":
                await credits.grant(s, account.id, args.credits * MICRO_PER_CREDIT, "top-up")
            else:
                await set_active(s, account.id, args.command == "enable")
            await s.commit()
        print(account.id)
        if args.command in ("invite", "link"):
            # In the fragment, so the token never reaches server or proxy logs.
            print(f"{args.base_url.rstrip('/')}/#invite={token}")
        return 0
    finally:
        await db.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(run(sys.argv[1:], get_settings())))
