import json
import os
import time
from collections import defaultdict, deque
from pathlib import Path

import google.auth
import google.auth.transport.requests
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

import mcp_tools
from bots import BOTS

MODEL = os.environ.get("MODEL", "openai/gpt-oss-120b-maas")
LOCATION = os.environ.get("LOCATION", "us-central1")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "2048"))
MAX_HISTORY = 20
MAX_STEPS = 6  # model/tool round trips per user message
RATE_LIMIT, RATE_WINDOW = 20, 60  # requests per window (seconds) per IP

creds, default_project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
PROJECT = os.environ.get("PROJECT") or default_project
URL = (
    f"https://{LOCATION}-aiplatform.googleapis.com/v1/projects/{PROJECT}"
    f"/locations/{LOCATION}/endpoints/openapi/chat/completions"
)

STATIC = Path(__file__).parent / "static"
app = FastAPI()
hits: dict[str, deque] = defaultdict(deque)


def token() -> str:
    if not creds.valid:
        creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def check_rate(ip: str) -> None:
    now = time.monotonic()
    q = hits[ip]
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_LIMIT:
        raise HTTPException(429, "Too many requests, slow down.")
    q.append(now)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/bots")
def bots():
    return [{"id": k, "name": v["name"], "description": v.get("description", "")} for k, v in BOTS.items()]


@app.post("/api/chat/{bot_id}")
async def chat(bot_id: str, request: Request):
    bot = BOTS.get(bot_id)
    if bot is None:
        raise HTTPException(404, "Unknown bot")
    ip = (request.headers.get("x-forwarded-for") or request.client.host).split(",")[0].strip()
    check_rate(ip)

    body = await request.json()
    history = [
        {"role": "assistant" if m.get("role") == "assistant" else "user", "content": m.get("text") or ""}
        for m in body.get("messages", [])
        if m.get("text")
    ][-MAX_HISTORY:]
    messages = [{"role": "system", "content": bot["system_prompt"]}, *history]
    model = bot.get("model", MODEL)

    def sse(**kw) -> str:
        return f"data: {json.dumps(kw)}\n\n"

    async def events():
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
                        yield sse(type="error", error=f"Model error {r.status_code}")
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
                            yield sse(type="text", text=delta["content"])
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
                    yield sse(type="tool", id=c["id"], name=c["name"], status="running")
                    artifacts, is_error = [], False
                    try:
                        args = json.loads(c["args"] or "{}")
                    except ValueError:
                        result, is_error = "Error: invalid JSON arguments", True
                    else:
                        result, artifacts, is_error = await mcp_tools.call_tool(c["name"], args)
                    for a in artifacts:
                        yield sse(type="artifact", **a)
                    yield sse(type="tool", id=c["id"], name=c["name"], status="error" if is_error else "done")
                    messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
            yield sse(type="error", error="Stopped: too many tool steps")

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
