"""The approval queue, for humans: python -m tollgate.approvals {list,approve,reject}

    python -m tollgate.approvals list
    python -m tollgate.approvals approve int_3f2a… --by priya.ops
    python -m tollgate.approvals reject  int_3f2a… --by priya.ops --note "duplicate claim"

Approving re-checks the policy (limits may have been used up while the request waited) and then
executes through the same executor as everything else. Every decision is in the audit log under
`human:<name>`.
"""

import argparse
import asyncio
import sys

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tollgate.clock import SystemClock
from tollgate.config import get_settings
from tollgate.guarded.executor import ApprovalError, Executor
from tollgate.money import format_inr
from tollgate.policy_config import load_policy
from tollgate.provider_client import ProviderClient


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with httpx.AsyncClient(base_url=settings.mockpay_url) as http:
            executor = Executor(
                maker,
                ProviderClient(http, settings.provider_timeout_s),
                load_policy(settings.policy_file),
                SystemClock(),
            )
            if args.command == "list":
                pending = await executor.pending_approvals()
                if not pending:
                    print("No requests waiting for approval.")
                for intent in pending:
                    print(
                        f"{intent.id}  {intent.kind:6}  {format_inr(intent.amount_paise):>12}  "
                        f"conversation {intent.conversation_id}  reason: {intent.reason}"
                    )
                return 0
            try:
                if args.command == "approve":
                    outcome = await executor.approve(args.intent_id, args.by)
                else:
                    outcome = await executor.reject(args.intent_id, args.by, args.note)
            except ApprovalError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
            print(f"{outcome.intent_id}: {outcome.status}. {outcome.message}".strip())
            return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tollgate.approvals")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("intent_id")
        p.add_argument("--by", required=True, help="who is deciding (recorded in the audit log)")
        if name == "reject":
            p.add_argument("--note", default="")
    return asyncio.run(_run(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
