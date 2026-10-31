"""The agent loop: model -> tool calls (MCP) -> model. Yields plain event dicts; knows nothing about HTTP or storage."""
import json
import os

import google.auth
import google.auth.transport.requests
import httpx

import mcp_tools

MODEL = os.environ.get("MODEL", "openai/gpt-oss-120b-maas")
LOCATION = os.environ.get("LOCATION", "us-central1")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "2048"))
MAX_STEPS = 6  # model/tool round trips per user message

creds, default_project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
PROJECT = os.environ.get("PROJECT") or default_project
URL = (
    f"https://{LOCATION}-aiplatform.googleapis.com/v1/projects/{PROJECT}"
    f"/locations/{LOCATION}/endpoints/openapi/chat/completions"
)


def token() -> str:
    if not creds.valid:
        creds.refresh(google.auth.transport.requests.Request())
    return creds.token


async def run(bot: dict, history: list[dict]):
    """history: prior turns plus the new user message, as {role, content}. Events: text | tool | artifact | error."""
    messages = [{"role": "system", "content": bot["system_prompt"]}, *history]
    model = bot.get("model", MODEL)
    tools = await mcp_tools.list_tools(bot.get("mcp_servers"))
    async with httpx.AsyncClient(timeout=120) as client:
        for _ in range(MAX_STEPS):
            payload = {"model": model, "messages": messages, "max_tokens": MAX_TOKENS, "stream": True}
            if tools:
                payload["tools"] = tools
            text, calls = "", {}
            async with client.stream(
                "POST", URL, json=payload, headers={"Authorization": f"Bearer {token()}"}
            ) as r:
                if r.status_code != 200:
                    yield {"type": "error", "error": f"Model error {r.status_code}"}
                    return
                async for line in r.aiter_lines():
                    if not line.startswith("data:") or line.strip() == "data: [DONE]":
                        continue
                    try:
                        delta = json.loads(line[5:])["choices"][0]["delta"]
                    except (ValueError, KeyError, IndexError):
                        continue
                    if delta.get("content"):
                        text += delta["content"]
                        yield {"type": "text", "text": delta["content"]}
                    for tc in delta.get("tool_calls") or []:
                        c = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                        c["id"] = tc.get("id") or c["id"]
                        fn = tc.get("function") or {}
                        c["name"] += fn.get("name") or ""
                        c["args"] += fn.get("arguments") or ""
            if not calls:
                return
            messages.append({
                "role": "assistant",
                "content": text or None,
                "tool_calls": [
                    {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                    for c in calls.values()
                ],
            })
            for c in calls.values():
                yield {"type": "tool", "id": c["id"], "name": c["name"], "status": "running"}
                artifacts, is_error = [], False
                try:
                    args = json.loads(c["args"] or "{}")
                except ValueError:
                    result, is_error = "Error: invalid JSON arguments", True
                else:
                    result, artifacts, is_error = await mcp_tools.call_tool(c["name"], args)
                for a in artifacts:
                    yield {"type": "artifact", **a}
                yield {"type": "tool", "id": c["id"], "name": c["name"], "status": "error" if is_error else "done"}
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
    yield {"type": "error", "error": "Stopped: too many tool steps"}
