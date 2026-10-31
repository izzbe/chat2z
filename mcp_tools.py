"""MCP client layer for the agent.

MCP servers are configured via the MCP_SERVERS env var, a JSON list such as
    [{"name": "trading", "url": "https://example.run.app/mcp", "headers": {"Authorization": "Bearer ..."}}]
With no servers configured the agent simply runs without tools. Tool names are exposed to the
model as "<server>__<tool>" so several servers can coexist.

Artifacts
---------
A tool can hand the user something richer than text (a chart, table, report, image...). The UI shows
these in a side panel. An artifact is {id, kind, title, content, ...}; the model only ever sees a
one-line note about it, never the payload. A tool produces artifacts in any of these ways:
  * return an MCP image content block                        -> kind "image"
  * return an MCP embedded resource (mime type decides kind) -> see MIME_KINDS
  * return a JSON text block {"artifact": {...}} or {"artifacts": [{...}, ...]}
    with kind in: markdown | html | table | chart | image | code
      table:  content is a CSV string or {"columns": [...], "rows": [[...], ...]}
      chart:  content is a Vega-Lite spec (object)
      image:  content is a data: URL
      html:   rendered in a sandboxed iframe
To add a new kind, add a renderer in static/index.html (the `renderers` map) and, if it has a mime
type, a row in MIME_KINDS.
"""
import base64
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

log = logging.getLogger("mcp_tools")
SEP = "__"

MIME_KINDS = {
    "text/html": "html",
    "text/markdown": "markdown",
    "text/csv": "table",
    "text/plain": "code",
    "application/json": "code",
}


def load_servers() -> list[dict]:
    return json.loads(os.environ.get("MCP_SERVERS") or "[]")


@asynccontextmanager
async def _session(server: dict):
    async with streamablehttp_client(server["url"], headers=server.get("headers")) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def list_tools(allowed: list[str] | None = None) -> list[dict]:
    """Tool definitions in OpenAI function-calling format, across all (allowed) servers."""
    tools = []
    for server in load_servers():
        if allowed is not None and server["name"] not in allowed:
            continue
        try:
            async with _session(server) as s:
                for t in (await s.list_tools()).tools:
                    tools.append({
                        "type": "function",
                        "function": {
                            "name": f"{server['name']}{SEP}{t.name}",
                            "description": t.description or "",
                            "parameters": t.inputSchema,
                        },
                    })
        except Exception:
            log.exception("could not list tools for MCP server %s", server["name"])
    return tools


def _artifact(kind: str, title: str, content, **extra) -> dict:
    return {"id": uuid.uuid4().hex[:12], "kind": kind, "title": title or kind.title(), "content": content, **extra}


def _from_resource(res) -> dict | None:
    mime = (getattr(res, "mimeType", None) or "text/plain").split(";")[0]
    title = str(getattr(res, "uri", "") or "").rsplit("/", 1)[-1]
    text, blob = getattr(res, "text", None), getattr(res, "blob", None)
    if mime.startswith("image/"):
        data = blob or base64.b64encode((text or "").encode()).decode()
        return _artifact("image", title, f"data:{mime};base64,{data}")
    if text is None and blob:
        text = base64.b64decode(blob).decode(errors="replace")
    if text is None:
        return None
    kind = MIME_KINDS.get(mime, "code")
    content = text
    if mime == "application/json":
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and "$schema" in parsed and "vega" in str(parsed["$schema"]):
                kind, content = "chart", parsed
        except ValueError:
            pass
    return _artifact(kind, title, content)


def parse_result(result) -> tuple[str, list[dict]]:
    """Split an MCP tool result into (text for the model, artifacts for the user)."""
    texts, artifacts = [], []
    for c in result.content:
        ctype = getattr(c, "type", None)
        if ctype == "image":
            artifacts.append(_artifact("image", "Image", f"data:{c.mimeType};base64,{c.data}"))
        elif ctype == "resource":
            a = _from_resource(c.resource)
            if a:
                artifacts.append(a)
        elif getattr(c, "text", None):
            try:
                parsed = json.loads(c.text)
            except ValueError:
                parsed = None
            declared = []
            if isinstance(parsed, dict):
                declared = [parsed["artifact"]] if "artifact" in parsed else parsed.get("artifacts") or []
            if declared and all(isinstance(d, dict) and "kind" in d for d in declared):
                for d in declared:
                    artifacts.append(_artifact(d["kind"], d.get("title"), d.get("content"), **{
                        k: v for k, v in d.items() if k not in ("kind", "title", "content", "id")}))
                if isinstance(parsed, dict) and parsed.get("summary"):
                    texts.append(str(parsed["summary"]))
            else:
                texts.append(c.text)
    for a in artifacts:
        texts.append(f"[Artifact shown to the user: {a['kind']} \"{a['title']}\"]")
    return "\n".join(texts), artifacts


async def call_tool(qualified_name: str, arguments: dict) -> tuple[str, list[dict], bool]:
    """Returns (text for the model, artifacts, is_error)."""
    server_name, _, tool = qualified_name.partition(SEP)
    server = next((s for s in load_servers() if s["name"] == server_name), None)
    if server is None:
        return f"Error: unknown tool {qualified_name}", [], True
    try:
        async with _session(server) as s:
            result = await s.call_tool(tool, arguments)
    except Exception as e:
        log.exception("tool call failed: %s", qualified_name)
        return f"Error calling {qualified_name}: {e}", [], True
    text, artifacts = parse_result(result)
    return (f"Error: {text}" if result.isError else text), artifacts, bool(result.isError)
