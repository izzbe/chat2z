"""Agents. To add one, drop a module into agents/library/ that defines a class decorated with @register.

Everything in agents/library/ is imported at startup, so a new file is all it takes: it then appears in the
UI picker and is served at POST /api/chat/<agent id>. See agents/base.py for the interface and
agents/langchain_agent.py for the base class most agents will extend.
"""
import importlib
import pkgutil

from agents import library
from agents.base import Agent, AgentContext, Message
from agents.registry import all_agents, get, register

for _m in pkgutil.iter_modules(library.__path__):
    if not _m.name.startswith("_"):
        importlib.import_module(f"agents.library.{_m.name}")

__all__ = ["Agent", "AgentContext", "Message", "all_agents", "get", "register"]
