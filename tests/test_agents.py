"""Offline tests: no network, no credentials. A scripted fake chat model stands in for Vertex."""
import json

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_core.tools import tool

import agents
from agents import Agent, AgentContext, events
from agents.langchain_agent import LangChainAgent
from agents.registry import register


class ScriptedModel(BaseChatModel):
    """Yields one scripted reply per call. A reply is text, or a (text, tool_calls) pair."""

    script: list
    calls: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # pragma: no cover - streaming only
        raise NotImplementedError

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(list(messages))
        step = self.script.pop(0)
        text, tool_calls = step if isinstance(step, tuple) else (step, [])
        for word in text.split(" ") if text else []:
            yield ChatGenerationChunk(message=AIMessageChunk(content=word + " "))
        if tool_calls:
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=[
                {"name": c["name"], "args": json.dumps(c["args"]), "id": c["id"], "index": i}
                for i, c in enumerate(tool_calls)
            ]))


def make_agent(script, tools=(), **attrs):
    class T(LangChainAgent):
        id, name, description, system_prompt = "t", "T", "test agent", "be brief"

        def tools(self):
            return list(tools)

        async def build_model(self):
            self.model = ScriptedModel(script=script, calls=[])  # kept so tests can inspect what was sent
            return self.model

    for k, v in attrs.items():
        setattr(T, k, v)
    return T()


def ctx(*msgs):
    return AgentContext(messages=list(msgs) or [{"role": "user", "content": "hi"}], user_id="u", username="u", chat_id="c")


async def collect(agent, c=None):
    return [e async for e in agent.run(c or ctx())]


def test_carmen_is_registered_and_conforms():
    carmen = agents.get("carmen")
    assert isinstance(carmen, Agent)
    assert [a.id for a in agents.all_agents()] == ["carmen"]
    assert carmen.name == "Carmen" and carmen.description


def test_registry_rejects_bad_agents():
    with pytest.raises(TypeError):
        register(object())

    class Dup(LangChainAgent):
        id, name, description = "carmen", "Dup", "d"

    with pytest.raises(ValueError, match="duplicate"):
        register(Dup)

    class BadId(LangChainAgent):
        id, name, description = "has space", "B", "d"

    with pytest.raises(ValueError, match="alphanumeric"):
        register(BadId)


async def test_plain_agent_without_langchain_satisfies_protocol():
    class Echo:  # no base class at all
        id, name, description = "echo", "Echo", "echoes"

        async def run(self, ctx):
            yield events.text(ctx.messages[-1]["content"])

    assert isinstance(Echo(), Agent)
    assert [e["text"] async for e in Echo().run(ctx())] == ["hi"]


async def test_streams_text_and_sends_system_prompt_and_history():
    a = make_agent(["hello there friend"])
    out = await collect(a, ctx({"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}))
    assert "".join(e["text"] for e in out).strip() == "hello there friend"
    assert {e["type"] for e in out} == {"text"}
    sent = a.model.calls[0]
    assert [m.type for m in sent] == ["system", "human", "ai", "human"]
    assert sent[0].content == "be brief"


async def test_tool_loop_runs_tool_and_streams_final_answer():
    @tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    a = make_agent([("", [{"id": "c1", "name": "add", "args": {"a": 2, "b": 3}}]), "the answer is 5"], tools=[add])
    out = await collect(a)
    kinds = [(e["type"], e.get("status")) for e in out]
    assert kinds[0] == ("tool", "running") and kinds[1] == ("tool", "done")
    assert "".join(e["text"] for e in out if e["type"] == "text").strip() == "the answer is 5"
    tool_msg = a.model.calls[1][-1]
    assert tool_msg.type == "tool" and tool_msg.content == "5"


async def test_tool_can_hand_the_user_an_artifact():
    @tool
    def report() -> str:
        """Make a report."""
        return json.dumps({"summary": "3 rows", "artifacts": [{"kind": "table", "title": "Rows", "content": "a\n1\n2\n3"}]})

    a = make_agent([("", [{"id": "c1", "name": "report", "args": {}}]), "done"], tools=[report])
    out = await collect(a)
    art = next(e for e in out if e["type"] == "artifact")
    assert art["kind"] == "table" and art["title"] == "Rows" and art["content"].startswith("a\n1")
    sent_to_model = a.model.calls[1][-1].content
    assert "3 rows" in sent_to_model and "a\n1" not in sent_to_model  # the model never sees the payload


async def test_failing_and_unknown_tools_report_error_status():
    @tool
    def boom() -> str:
        """Always fails."""
        raise RuntimeError("kaput")

    a = make_agent([("", [{"id": "c1", "name": "boom", "args": {}}, {"id": "c2", "name": "nope", "args": {}}]), "ok"], tools=[boom])
    out = await collect(a)
    statuses = [(e["id"], e["status"]) for e in out if e["type"] == "tool" and e["status"] != "running"]
    assert statuses == [("c1", "error"), ("c2", "error")]
    assert out[-1]["type"] == "text"  # the turn still completes


async def test_step_limit_stops_runaway_tool_loops():
    @tool
    def loop() -> str:
        """Loops."""
        return "again"

    script = [("", [{"id": f"c{i}", "name": "loop", "args": {}}]) for i in range(10)]
    out = await collect(make_agent(script, tools=[loop], max_steps=3))
    assert out[-1] == {"type": "error", "error": "Stopped: too many tool steps"}


async def test_model_failure_becomes_an_error_event_not_an_exception():
    class Broken(LangChainAgent):
        id, name, description = "broken", "B", "d"

        async def build_model(self):
            raise RuntimeError("vertex down")

    out = await collect(Broken())
    assert len(out) == 1 and out[0]["type"] == "error" and "vertex down" not in out[0]["error"]


def test_split_artifacts_leaves_plain_results_alone():
    assert events.split_artifacts("just text") == ("just text", [])
    assert events.split_artifacts(42) == ("42", [])
    text, arts = events.split_artifacts({"artifacts": [{"kind": "code", "content": "x"}]})
    assert len(arts) == 1 and "code" in text
