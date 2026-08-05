"""Tests for the stateful Responses API surface (store / previous_response_id, ADR 0012).

Exercises the enabled path end to end via TestClient with a memory response store: storing a
response, retrieving it, listing input items, chaining with previous_response_id, deletion,
tenant isolation, and the not-found / not-enabled rejections.
"""

from __future__ import annotations

from app.main import create_app
from app.settings import Settings
from fastapi.testclient import TestClient

from tests.gateway_support import FakeRuntimeClient


def _chat_response(text="reply"):
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "default-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12},
    }


def _settings(enabled=True, **overrides):
    base = {
        "runtime_backend": "vllm",
        "ollama_base_url": "http://ollama:11434",
        "vllm_base_url": "http://vllm:8000",
        "model_id": "default-model",
        "request_timeout_seconds": 5,
        "responses_store_enabled": enabled,
        "responses_store_backend": "memory",
    }
    base.update(overrides)
    return Settings(**base)


def _client(enabled=True, response_text="reply"):
    app = create_app(_settings(enabled=enabled))
    fake = FakeRuntimeClient(response=_chat_response(response_text))
    app.state.runtime_client = fake
    return TestClient(app), fake


def test_store_persists_and_retrieve_and_input_items():
    client, _ = _client()
    created = client.post("/v1/responses", json={"input": "hi", "store": True}).json()
    assert created["object"] == "response"
    rid = created["id"]
    assert rid.startswith("resp_")

    got = client.get(f"/v1/responses/{rid}")
    assert got.status_code == 200
    assert got.json()["id"] == rid

    items = client.get(f"/v1/responses/{rid}/input_items")
    assert items.status_code == 200
    assert items.json()["object"] == "list"
    assert items.json()["data"][0]["content"][0]["text"] == "hi"


def test_previous_response_id_chains_prior_conversation():
    client, fake = _client(response_text="r1")
    first = client.post("/v1/responses", json={"input": "hi", "store": True}).json()
    # A follow-up turn chained onto the first: the runtime must see the full history.
    client.post("/v1/responses", json={"input": "again", "store": True, "previous_response_id": first["id"]})
    messages = fake.payload["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"] == "hi"
    assert messages[1]["content"] == "r1"  # the stored assistant reply
    assert messages[2]["content"] == "again"


def test_delete_removes_stored_response():
    client, _ = _client()
    rid = client.post("/v1/responses", json={"input": "hi", "store": True}).json()["id"]
    deleted = client.delete(f"/v1/responses/{rid}")
    assert deleted.json()["deleted"] is True
    assert client.get(f"/v1/responses/{rid}").status_code == 404


def test_tenant_isolation_on_stored_response():
    client, _ = _client()
    rid = client.post("/v1/responses", json={"input": "hi", "store": True}, headers={"X-Sandbox-ID": "team-a"}).json()[
        "id"
    ]
    assert client.get(f"/v1/responses/{rid}", headers={"X-Sandbox-ID": "team-b"}).status_code == 404
    assert client.get(f"/v1/responses/{rid}", headers={"X-Sandbox-ID": "team-a"}).status_code == 200


def test_previous_response_id_not_found_is_rejected():
    client, fake = _client()
    resp = client.post("/v1/responses", json={"input": "hi", "previous_response_id": "resp_nope"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "previous_response_not_found"
    assert fake.calls == 0  # rejected before the runtime call


def test_retrieval_endpoints_404_when_store_disabled():
    client, _ = _client(enabled=False)
    assert client.get("/v1/responses/resp_x").status_code == 404
    assert client.delete("/v1/responses/resp_x").status_code == 404
    assert client.get("/v1/responses/resp_x/input_items").status_code == 404
