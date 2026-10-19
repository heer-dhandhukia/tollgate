"""CLI: python -m tollgate.eval {run,summarise,report}"""

import argparse
import asyncio
import logging
import sys

from tollgate.clock import FixedClock
from tollgate.config import REPO_ROOT, get_settings
from tollgate.eval.report import update_readme
from tollgate.eval.runner import PROMPT_VERSION, EvalRunner, write_summary
from tollgate.eval.sandbox import Sandboxes
from tollgate.eval.scenario import load_catalog, load_suites
from tollgate.llm.cache import ResponseCache
from tollgate.llm.client import LLMClient
from tollgate.llm.pricing import load_prices
from tollgate.policy_config import load_policy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tollgate.eval")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "summarise"):
        p = sub.add_parser(name)
        p.add_argument("--mode", required=True, choices=["naive", "guarded"])
        p.add_argument("--suites", default="legit,policy_edges")
        p.add_argument("--runs", type=int, default=3)
    sub.add_parser("report")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    settings = get_settings()
    if args.command == "report":
        update_readme(REPO_ROOT / "README.md", settings.results_dir)
        print("README results updated")
        return 0

    suites = load_suites(settings.scenarios_dir, args.suites.split(","))
    runs = list(range(1, args.runs + 1))
    policy = load_policy(settings.policy_file)
    prices = load_prices(settings.models_config)
    meta = {
        "model": settings.tollgate_agent_model,
        "prompt_version": f"{args.mode}.{PROMPT_VERSION}",
        "policy_version": policy.version,
        "eval_today": settings.eval_today.isoformat(),
        "usd_to_inr": prices.usd_to_inr,
        "usd_to_inr_as_of": prices.usd_to_inr_as_of,
    }

    exit_code = 0
    if args.command == "run":
        llm = LLMClient(
            settings.tollgate_agent_model,
            ResponseCache(settings.llm_cache_dir),
            prices,
            concurrency=settings.llm_concurrency,
            max_retries=settings.llm_max_retries,
            backoff_cap_s=settings.llm_backoff_cap_s,
        )
        runner = EvalRunner(
            llm=llm,
            sandboxes=Sandboxes(settings.eval_pg_admin_url),
            policy=policy,
            catalog=load_catalog(settings.scenarios_dir / "catalog.yaml"),
            clock=FixedClock(settings.eval_today),
            results_dir=settings.results_dir,
            provider_timeout_s=settings.provider_timeout_s,
        )
        summary = asyncio.run(runner.run(args.mode, suites, runs))
        print(
            f"completed={summary.completed} skipped={summary.skipped} "
            f"failed={len(summary.failed)} rate_limited={summary.stopped_for_rate_limit}"
        )
        for failure in summary.failed:
            print(f"  FAILED {failure}")
        exit_code = 0 if not summary.failed else 2

    path = write_summary(settings.results_dir, args.mode, suites, runs, meta)
    print(f"wrote {path.relative_to(REPO_ROOT)}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
