"""What an agent streams back. These dicts go over SSE to the UI unchanged, so the shapes are the contract.

  text      {"type": "text", "text": "..."}                     a chunk of the reply
  tool      {"type": "tool", "id", "name", "status"}            status: running | done | error
  artifact  {"type": "artifact", "id", "kind", "title", "content"}   shown in the side panel
  error     {"type": "error", "error": "..."}                   shown inline; ends the turn

Artifact kinds the UI renders: markdown | html | table | chart | image | code
  table: content is a CSV string or {"columns": [...], "rows": [[...]]}
  chart: content is a Vega-Lite spec (object)
  image: content is a data: URL
  html:  rendered in a sandboxed iframe
To add a kind, add a renderer in static/index.html (the `renderers` map).
"""
import json
import uuid
from typing import Any, Literal, TypedDict


class TextEvent(TypedDict):
    type: Literal["text"]
    text: str


class ToolEvent(TypedDict):
    type: Literal["tool"]
    id: str
    name: str
    status: Literal["running", "done", "error"]


class ArtifactEvent(TypedDict):
    type: Literal["artifact"]
    id: str
    kind: str
    title: str
    content: Any


class ErrorEvent(TypedDict):
    type: Literal["error"]
    error: str


Event = TextEvent | ToolEvent | ArtifactEvent | ErrorEvent


def text(s: str) -> TextEvent:
    return {"type": "text", "text": s}


def tool(id: str, name: str, status: Literal["running", "done", "error"]) -> ToolEvent:
    return {"type": "tool", "id": id, "name": name, "status": status}


def artifact(kind: str, title: str, content: Any) -> ArtifactEvent:
    return {"type": "artifact", "id": uuid.uuid4().hex[:20], "kind": kind, "title": title or kind.title(), "content": content}


def error(message: str) -> ErrorEvent:
    return {"type": "error", "error": message}


def split_artifacts(result: Any) -> tuple[str, list[ArtifactEvent]]:
    """Turn a tool's return value into (text the model sees, artifacts the user sees).

    A tool returns either a plain value (just text for the model) or, to hand the user something richer,
    a dict / JSON string like {"summary": "...", "artifacts": [{"kind", "title", "content"}, ...]}.
    The model only ever gets the summary plus a one-line note per artifact, never the payload.
    """
    doc = result
    if isinstance(result, str):
        try:
            doc = json.loads(result)
        except ValueError:
            return result, []
    items = doc.get("artifacts") if isinstance(doc, dict) else None
    if not items or not all(isinstance(i, dict) and "kind" in i for i in items):
        return (result if isinstance(result, str) else json.dumps(result, default=str)), []
    arts = [artifact(i["kind"], i.get("title", ""), i.get("content")) for i in items]
    notes = [f'[Artifact shown to the user: {a["kind"]} "{a["title"]}"]' for a in arts]
    return "\n".join([str(doc.get("summary", "")).strip(), *notes]).strip(), arts
