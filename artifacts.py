"""Artifact payloads in Google Cloud Storage. Postgres keeps only metadata (see store.py)."""
import json

from google.cloud import storage

import settings

_bucket = None


def _b():
    global _bucket
    if _bucket is None:
        if not settings.ARTIFACT_BUCKET:
            raise RuntimeError("ARTIFACT_BUCKET is not set")
        _bucket = storage.Client().bucket(settings.ARTIFACT_BUCKET)
    return _bucket


def put(path: str, doc: dict) -> int:
    data = json.dumps(doc).encode()
    if len(data) > settings.MAX_ARTIFACT_BYTES:
        raise ValueError("artifact too large")
    _b().blob(path).upload_from_string(data, content_type="application/json")
    return len(data)


def get(path: str) -> dict:
    return json.loads(_b().blob(path).download_as_bytes())


def delete(paths: list[str]) -> None:
    for p in paths:
        _b().blob(p).delete()
