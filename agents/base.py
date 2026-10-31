"""The interface every agent implements.

An agent is anything with an id, a name, a description and an async `run` that turns a conversation into a
stream of events. It is a typing.Protocol, so an agent does not have to inherit from anything: subclass
`LangChainAgent` for the usual case, or write a plain class (a hand-rolled loop, a LangGraph graph, a
non-LLM rules engine) as long as it has this shape.
"""
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol, TypedDict, runtime_checkable

from agents.events import Event


class Message(TypedDict):
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class AgentContext:
    """Everything an agent may use for one turn. Add fields here as agents need them (never reach into globals)."""

    messages: list[Message]          # prior turns, ending with the new user message
    user_id: str
    username: str
    chat_id: str
    extras: dict = field(default_factory=dict)


@runtime_checkable
class Agent(Protocol):
    id: str            # stable key: URL path, DB `bot_id`, UI selection. Never change once chats exist.
    name: str          # shown in the picker
    description: str   # shown under the name in the picker

    def run(self, ctx: AgentContext) -> AsyncIterator[Event]:
        """Stream events for one turn. Write it as `async def run(...)` with `yield`."""
        ...
