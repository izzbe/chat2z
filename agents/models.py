"""Chat model factories. Today: open models on Vertex AI via its OpenAI-compatible endpoint."""
import asyncio

import google.auth
import google.auth.transport.requests
from langchain_openai import ChatOpenAI

import settings

_creds = None
_project = None


def _token() -> str:
    global _creds, _project
    if _creds is None:
        _creds, _project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    if not _creds.valid:
        _creds.refresh(google.auth.transport.requests.Request())
    return _creds.token


async def vertex_chat_model(model: str | None = None, **kwargs) -> ChatOpenAI:
    """A chat model for a Vertex AI Model Garden (MaaS) model. Built per call because the access token expires hourly."""
    token = await asyncio.to_thread(_token)
    project = settings.VERTEX_PROJECT or _project
    loc = settings.VERTEX_LOCATION
    return ChatOpenAI(
        model=model or settings.DEFAULT_MODEL,
        base_url=f"https://{loc}-aiplatform.googleapis.com/v1/projects/{project}/locations/{loc}/endpoints/openapi",
        api_key=token,
        max_tokens=kwargs.pop("max_tokens", settings.MAX_TOKENS),
        streaming=True,
        **kwargs,
    )
