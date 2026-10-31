import json
from types import SimpleNamespace as NS

from mcp_tools import parse_result


def result(*content):
    return NS(content=list(content), isError=False)


def text(s):
    return NS(type="text", text=s)


def test_plain_text_passes_through():
    t, arts = parse_result(result(text("hello")))
    assert t == "hello" and arts == []


def test_declared_artifacts_are_extracted_and_hidden_from_model():
    payload = {"summary": "done", "artifacts": [{"kind": "table", "title": "T", "content": "a,b\n1,2"}]}
    t, arts = parse_result(result(text(json.dumps(payload))))
    assert len(arts) == 1 and arts[0]["kind"] == "table" and arts[0]["id"]
    assert "a,b" not in t and "done" in t and "table" in t


def test_image_content_becomes_image_artifact():
    t, arts = parse_result(result(NS(type="image", mimeType="image/png", data="AAAA")))
    assert arts[0]["kind"] == "image" and arts[0]["content"] == "data:image/png;base64,AAAA"


def test_embedded_vega_resource_becomes_chart():
    res = NS(uri="x://c.json", mimeType="application/json", text=json.dumps({"$schema": "https://vega.github.io/schema/vega-lite/v5.json"}), blob=None)
    _, arts = parse_result(result(NS(type="resource", resource=res)))
    assert arts[0]["kind"] == "chart"
