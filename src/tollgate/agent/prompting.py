from datetime import date
from pathlib import Path

from tollgate.agent.session import AuthenticatedSession
from tollgate.policy_config import PolicyConfig

PROMPTS = Path(__file__).resolve().parent / "prompts"


def system_prompt(
    mode: str, version: str, session: AuthenticatedSession, policy: PolicyConfig, today: date
) -> str:
    template = (PROMPTS / f"system_{mode}.{version}.md").read_text()
    return template.format(
        today=today.isoformat(),
        customer_name=session.customer_name,
        customer_ref=session.customer_ref,
        policy=policy.as_prompt_text(),
    ).strip()
