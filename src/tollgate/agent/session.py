from dataclasses import dataclass


@dataclass(frozen=True)
class AuthenticatedSession:
    """Who the agent is talking to, established by authentication outside the conversation.

    Tools take the customer from here and never from chat content, so "I'm customer X" or
    "I'm the store manager" in a message cannot change whose orders are visible."""

    customer_id: int
    customer_ref: str
    customer_name: str
    conversation_id: str
