"""LangChain-backed agent base class: system prompt + streaming chat model + an optional tool loop.

Subclass it, set the class attributes, and register it:

    @register
    class Momentum(LangChainAgent):
        id = "momentum"
        name = "Momentum"
        description = "Screens for momentum setups"
        system_prompt = "You are ..."

        def tools(self):                 # optional: LangChain tools (BaseTool / @tool functions)
            return [get_prices, run_backtest]

Override `build_model()` to use a different model, or override `run()` entirely for custom control flow.
"""
import logging
from collections.abc import AsyncIterator

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool

from agents import events
from agents.base import AgentContext
from agents.models import vertex_chat_model

log = logging.getLogger("agents")


def _text_of(content) -> str:
    """Chat models return a string, or a list of content blocks; keep only the text."""
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content or [])


class LangChainAgent:
    id: str
    name: str
    description: str
    system_prompt: str = ""
    model_name: str | None = None   # None = settings.DEFAULT_MODEL
    max_steps = 6                   # model/tool round trips per user message

    def tools(self) -> list[BaseTool]:
        return []

    async def build_model(self) -> BaseChatModel:
        return await vertex_chat_model(self.model_name)

    def build_messages(self, ctx: AgentContext) -> list[BaseMessage]:
        msgs: list[BaseMessage] = [SystemMessage(self.system_prompt)] if self.system_prompt else []
        msgs += [(HumanMessage if m["role"] == "user" else AIMessage)(m["content"]) for m in ctx.messages]
        return msgs

    async def run(self, ctx: AgentContext) -> AsyncIterator[events.Event]:
        try:
            tools = {t.name: t for t in self.tools()}
            model = await self.build_model()
            if tools:
                model = model.bind_tools(list(tools.values()))
            messages = self.build_messages(ctx)

            for _ in range(self.max_steps):
                full = None
                async for chunk in model.astream(messages):
                    piece = _text_of(chunk.content)
                    if piece:
                        yield events.text(piece)
                    full = chunk if full is None else full + chunk
                if full is None or not tools or not full.tool_calls:
                    return

                messages.append(AIMessage(content=_text_of(full.content), tool_calls=full.tool_calls))
                for call in full.tool_calls:
                    yield events.tool(call["id"], call["name"], "running")
                    result, arts, failed = await self._call_tool(tools, call)
                    for a in arts:
                        yield a
                    yield events.tool(call["id"], call["name"], "error" if failed else "done")
                    messages.append(ToolMessage(content=result, tool_call_id=call["id"]))
            yield events.error("Stopped: too many tool steps")
        except Exception:
            log.exception("agent %s failed", self.id)
            yield events.error("The model request failed. Please try again.")

    async def _call_tool(self, tools: dict[str, BaseTool], call: dict) -> tuple[str, list, bool]:
        tool = tools.get(call["name"])
        if tool is None:
            return f"Error: unknown tool {call['name']}", [], True
        try:
            raw = await tool.ainvoke(call["args"])
        except Exception as e:
            log.exception("tool %s failed", call["name"])
            return f"Error calling {call['name']}: {e}", [], True
        text, arts = events.split_artifacts(raw)
        return text, arts, False
