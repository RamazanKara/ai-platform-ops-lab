"""Tests for the OpenAI Responses API endpoint (POST /v1/responses, stateless subset).

The endpoint translates a Responses request to the internal OpenAI chat shape, runs it
through the SAME governance path as /v1/chat/completions, and translates the OpenAI
completion into a Responses object. These tests assert that path end to end: admission
(max_output_tokens cap, missing input), budget, audit, forwarding, the Responses response
shape (output[].content[].output_text / usage.input_tokens/output_tokens), prompt-secret
modes, auth/tenant binding, streaming rejection, and the stateless-subset rejection of
store / previous_response_id. Plus direct unit tests of the responses.py pure functions.

The runtime fake is the shared tests.gateway_support one; the JWT helpers stay local
because this suite exercises tenant binding with its own key shapes.
"""

import base64
import hashlib
import hmac
import json
import logging
import time

import httpx
from app.jwt_auth import JwksCache
from app.main import create_app
from app.settings import Settings
from fastapi.testclient import TestClient

from tests.gateway_support import FakeRuntimeClient


def _tool_settings(**overrides):
    base = {
        "runtime_backend": "vllm",
        "ollama_base_url": "http://ollama:11434",
        "vllm_base_url": "http://vllm:8000",
        "model_id": "default-model",
        "request_timeout_seconds": 5,
    }
    base.update(overrides)
    return Settings(**base)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _signed_hs256_jwt(secret: bytes, claims: dict, kid: str = "test-key") -> str:
    header = {"alg": "HS256", "typ": "JWT", "kid": kid}
    encoded_header = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    encoded_claims = _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{encoded_header}.{encoded_claims}".encode("ascii")
    signature = hmac.new(secret, signing_input, "sha256").digest()
    return f"{encoded_header}.{encoded_claims}.{_b64url(signature)}"


def _hs256_jwt_settings(secret, **overrides):
    # The HS256 secret is provided to verification via the JwksCache.keys() mock (the ``k``
    # JWK field), not a settings field. ``secret`` stays in the helper signature because
    # callers also use it to build the matching token and cache entry.
    base = {
        "runtime_backend": "vllm",
        "ollama_base_url": "http://ollama:11434",
        "vllm_base_url": "http://vllm:8000",
        "model_id": "default-model",
        "request_timeout_seconds": 5,
        "jwt_auth_enabled": True,
        "jwt_jwks_url": "https://issuer.example/.well-known/jwks.json",
    }
    base.update(overrides)
    return Settings(**base)


def _chat_response(text="hello from responses", finish_reason="stop", usage=None):
    return {
        "id": "chatcmpl-abc123",
        "object": "chat.completion",
        "model": "default-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage or {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12},
    }


# --- happy path: string input, admitted, budgeted, audited, forwarded, Responses-shaped ---


def test_responses_string_input_admitted_budgeted_audited_and_forwarded(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    app = create_app(
        _tool_settings(
            allowed_models=("default-model",),
            sandbox_budget_enabled=True,
            sandbox_request_budget=5,
            sandbox_estimated_token_budget=1000,
            budget_estimated_chars_per_token=4,
        )
    )
    fake = FakeRuntimeClient(response=_chat_response())
    app.state.runtime_client = fake
    client = TestClient(app)

    response = client.post(
        "/v1/responses",
        headers={"X-Sandbox-ID": "budget-lab"},
        json={
            "model": "default-model",
            "instructions": "You are terse.",
            "input": "hello",
            "max_output_tokens": 64,
        },
    )

    assert response.status_code == 200
    body = response.json()
    # Responses-shaped response.
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["model"] == "default-model"
    assert body["output"][0]["type"] == "message"
    assert body["output"][0]["role"] == "assistant"
    assert body["output"][0]["content"] == [{"type": "output_text", "text": "hello from responses", "annotations": []}]
    assert body["usage"] == {"input_tokens": 7, "output_tokens": 5, "total_tokens": 12}
    assert body["id"].startswith("resp_")

    # Forwarded to the runtime as an OpenAI chat payload: instructions -> system, input ->
    # user, max_output_tokens -> max_tokens.
    assert fake.payload["model"] == "default-model"
    assert fake.payload["max_tokens"] == 64
    assert fake.payload["messages"][0] == {"role": "system", "content": "You are terse."}
    assert fake.payload["messages"][1] == {"role": "user", "content": "hello"}

    # Budgeted (OpenAI-style budget headers present on the response).
    assert response.headers["x-ratelimit-remaining-requests"] == "4"

    # Audited as an inference_request receipt with the (translated) prompt fingerprint.
    event = next(
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "ai_platform_ops_lab.audit" and '"inference_request"' in record.getMessage()
    )
    assert event["message_count"] == 2
    assert event["message_roles"] == ["system", "user"]


# --- happy path: array input (messages + content parts) is translated and forwarded ---


def test_responses_array_input_translated_and_forwarded():
    app = create_app(_tool_settings(allowed_models=("default-model",)))
    fake = FakeRuntimeClient(response=_chat_response())
    app.state.runtime_client = fake
    client = TestClient(app)

    response = client.post(
        "/v1/responses",
        json={
            "input": [
                {"role": "user", "content": "first"},
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "second-a "},
                        {"type": "input_text", "text": "second-b"},
                    ],
                },
            ],
            "max_output_tokens": 64,
        },
    )

    assert response.status_code == 200
    # The array input is translated to two user messages; content parts are concatenated.
    assert fake.payload["messages"] == [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second-a second-b"},
    ]


def test_responses_maps_length_finish_reason_to_incomplete():
    app = create_app(_tool_settings(allowed_models=("default-model",)))
    app.state.runtime_client = FakeRuntimeClient(response=_chat_response(text="truncated", finish_reason="length"))
    client = TestClient(app)

    response = client.post(
        "/v1/responses",
        json={"input": "write a long essay", "max_output_tokens": 16},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "incomplete"
    assert body["incomplete_details"] == {"reason": "max_output_tokens"}


def test_responses_maps_tool_calls_to_function_call_output_items():
    app = create_app(_tool_settings(allowed_models=("default-model",)))
    fake = FakeRuntimeClient(
        response={
            "id": "chatcmpl-xyz",
            "object": "chat.completion",
            "model": "default-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "get_weather", "arguments": '{"city": "Berlin"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
        }
    )
    app.state.runtime_client = fake
    client = TestClient(app)

    response = client.post(
        "/v1/responses",
        json={
            "input": "weather in Berlin?",
            "max_output_tokens": 128,
            "tools": [
                {
                    "type": "function",
                    "name": "get_weather",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    # tool_calls -> function_call output item (name, arguments, call_id).
    function_calls = [item for item in body["output"] if item["type"] == "function_call"]
    assert len(function_calls) == 1
    assert function_calls[0]["name"] == "get_weather"
    assert function_calls[0]["arguments"] == '{"city": "Berlin"}'
    assert function_calls[0]["call_id"] == "call_1"
    assert body["usage"] == {"input_tokens": 11, "output_tokens": 3, "total_tokens": 14}
    # tools are forwarded verbatim (the Responses function-tool shape matches OpenAI chat's).
    assert fake.payload["tools"][0]["function" if "function" in fake.payload["tools"][0] else "name"]


# --- missing input is a 4xx (empty translated messages -> shared missing_messages) ---


def test_responses_missing_input_is_rejected():
    app = create_app(_tool_settings(allowed_models=("default-model",)))
    fake = FakeRuntimeClient(response=_chat_response())
    app.state.runtime_client = fake
    client = TestClient(app)

    # input is a required field: pydantic rejects its absence before the runtime is called.
    missing = client.post("/v1/responses", json={"max_output_tokens": 64})
    assert missing.status_code == 422
    assert fake.calls == 0

    # An empty-array input translates to no messages and is rejected by shared admission.
    empty = client.post("/v1/responses", json={"input": [], "max_output_tokens": 64})
    assert empty.status_code == 400
    assert empty.json()["detail"]["reason"] == "missing_messages"
    assert fake.calls == 0


# --- over-cap max_output_tokens is rejected by the shared admission cap ---


def test_responses_rejects_over_cap_max_output_tokens():
    app = create_app(_tool_settings(allowed_models=("default-model",), max_completion_tokens=10))
    fake = FakeRuntimeClient(response=_chat_response())
    app.state.runtime_client = fake
    client = TestClient(app)

    response = client.post(
        "/v1/responses",
        json={"input": "hi", "max_output_tokens": 50},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "max_tokens_too_large"
    assert body["detail"]["reason"] == "max_tokens_too_large"
    # The over-cap request never reaches the runtime.
    assert fake.calls == 0


# --- stateless subset: store / previous_response_id -> 400 stateful_not_supported ---


def test_responses_rejects_store_true_as_stateful():
    app = create_app(_tool_settings(allowed_models=("default-model",)))
    fake = FakeRuntimeClient(response=_chat_response())
    app.state.runtime_client = fake
    client = TestClient(app)

    response = client.post(
        "/v1/responses",
        json={"input": "hi", "max_output_tokens": 64, "store": True},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "stateful_not_supported"
    assert fake.calls == 0


def test_responses_rejects_previous_response_id_as_stateful():
    app = create_app(_tool_settings(allowed_models=("default-model",)))
    fake = FakeRuntimeClient(response=_chat_response())
    app.state.runtime_client = fake
    client = TestClient(app)

    response = client.post(
        "/v1/responses",
        json={"input": "hi", "max_output_tokens": 64, "previous_response_id": "resp_prev"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "stateful_not_supported"
    assert fake.calls == 0


# --- store=false is fine (explicitly stateless is allowed) ---


def test_responses_store_false_is_allowed():
    app = create_app(_tool_settings(allowed_models=("default-model",)))
    fake = FakeRuntimeClient(response=_chat_response())
    app.state.runtime_client = fake
    client = TestClient(app)

    response = client.post(
        "/v1/responses",
        json={"input": "hi", "max_output_tokens": 64, "store": False},
    )

    assert response.status_code == 200
    assert fake.calls == 1


# --- prompt-secret modes are honored on the translated messages ---


def test_responses_rejects_prompt_secret_in_block_mode():
    app = create_app(_tool_settings(allowed_models=("default-model",)))
    fake = FakeRuntimeClient(response=_chat_response())
    app.state.runtime_client = fake
    client = TestClient(app)

    response = client.post(
        "/v1/responses",
        json={
            "input": "token ghp_0123456789abcdefghijABCDEFGHIJ012345",
            "max_output_tokens": 64,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "prompt_secret_detected"
    assert fake.calls == 0


def test_responses_redacts_prompt_secret_in_redact_mode():
    app = create_app(_tool_settings(allowed_models=("default-model",), prompt_secret_mode="redact"))
    fake = FakeRuntimeClient(response=_chat_response(text="ok"))
    app.state.runtime_client = fake
    client = TestClient(app)

    response = client.post(
        "/v1/responses",
        json={
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "here is my key ghp_0123456789abcdefghijABCDEFGHIJ012345"}
                    ],
                }
            ],
            "max_output_tokens": 64,
        },
    )

    assert response.status_code == 200
    assert response.headers["X-Prompt-Guardrail"] == "redacted"
    # The credential is redacted in the translated message before it reaches the runtime.
    forwarded = json.dumps(fake.payload["messages"])
    assert "ghp_0123456789abcdefghijABCDEFGHIJ012345" not in forwarded
    assert "[REDACTED:github_token]" in forwarded


# --- streaming is rejected with a clear error (non-streaming-first, like /v1/messages) ---


def test_responses_rejects_streaming_with_clear_error():
    app = create_app(_tool_settings(allowed_models=("default-model",), allow_streaming=True))
    fake = FakeRuntimeClient(response=_chat_response())
    app.state.runtime_client = fake
    client = TestClient(app)

    response = client.post(
        "/v1/responses",
        json={"input": "hi", "max_output_tokens": 64, "stream": True},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "streaming_not_supported"
    assert fake.calls == 0


def test_responses_rejects_unapproved_model():
    app = create_app(_tool_settings(allowed_models=("default-model",)))
    app.state.runtime_client = FakeRuntimeClient(response=_chat_response())
    client = TestClient(app)

    response = client.post(
        "/v1/responses",
        json={"model": "rogue-model", "input": "hi", "max_output_tokens": 64},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "model_not_allowed"


# --- auth / tenant binding apply exactly like the other routes ---


def test_responses_requires_api_key_when_enabled():
    settings = _tool_settings(
        allowed_models=("default-model",),
        api_key_auth_enabled=True,
        api_key_sha256s=(hashlib.sha256(b"secret-key").hexdigest(),),
    )
    app = create_app(settings)
    fake = FakeRuntimeClient(response=_chat_response())
    app.state.runtime_client = fake
    client = TestClient(app)

    body = {"input": "hi", "max_output_tokens": 64}
    missing = client.post("/v1/responses", json=body)
    wrong = client.post("/v1/responses", headers={"X-API-Key": "wrong"}, json=body)
    valid = client.post("/v1/responses", headers={"X-API-Key": "secret-key"}, json=body)

    assert missing.status_code == 401
    assert missing.json()["detail"]["reason"] == "invalid_or_missing_api_key"
    assert wrong.status_code == 401
    assert valid.status_code == 200
    # Only the authenticated call reaches the runtime.
    assert fake.calls == 1


def test_responses_jwt_tenant_claim_binds_and_mismatch_is_rejected(monkeypatch):
    secret = b"jwt-test-secret"

    async def fake_keys(self):
        return [{"kty": "oct", "kid": "test-key", "k": _b64url(secret)}]

    monkeypatch.setattr(JwksCache, "keys", fake_keys)
    app = create_app(_hs256_jwt_settings(secret, allowed_models=("default-model",), jwt_tenant_claim="sandbox"))
    fake = FakeRuntimeClient(response=_chat_response())
    app.state.runtime_client = fake
    client = TestClient(app)
    token = _signed_hs256_jwt(secret, {"sandbox": "team-a", "exp": int(time.time()) + 300})
    body = {"input": "hi", "max_output_tokens": 64}

    # No header: the sandbox is bound from the verified tenant claim.
    bound = client.post("/v1/responses", headers={"Authorization": f"Bearer {token}"}, json=body)
    assert bound.status_code == 200
    assert bound.headers["X-Sandbox-ID"] == "team-a"

    # A contradicting header is rejected with the same reason as the other routes (403).
    mismatch = client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {token}", "X-Sandbox-ID": "team-b"},
        json=body,
    )
    assert mismatch.status_code == 403
    assert mismatch.json()["detail"]["reason"] == "sandbox_identity_mismatch"


def test_responses_runtime_error_maps_to_502():
    app = create_app(_tool_settings(allowed_models=("default-model",)))
    app.state.runtime_client = FakeRuntimeClient(error=httpx.ConnectError("no route"))
    client = TestClient(app)

    response = client.post(
        "/v1/responses",
        json={"input": "hi", "max_output_tokens": 64},
    )

    assert response.status_code == 502
    assert response.json()["error"]["message"] == "runtime request failed"


# --- Unit coverage for the Responses<->OpenAI translation helpers -----------

from app.responses import (  # noqa: E402
    ResponsesRequest,
    _content_to_text,
    _input_item_to_message,
    _input_to_messages,
    chat_completion_to_responses,
    responses_to_chat_payload,
)


def test_content_to_text_variants():
    assert _content_to_text(None) == ""
    assert _content_to_text("plain") == "plain"
    # input_text / text / bare-string parts are concatenated; unknown parts contribute nothing.
    assert (
        _content_to_text(
            [
                {"type": "input_text", "text": "a"},
                {"type": "text", "text": "b"},
                "c",
                {"type": "input_image", "image_url": "x"},
                {"type": "input_text"},  # missing text -> ""
            ]
        )
        == "abc"
    )
    assert _content_to_text(42) == "42"


def test_input_item_to_message_shapes():
    # A bare string becomes a user message.
    assert _input_item_to_message("hi") == {"role": "user", "content": "hi"}
    # A {role, content} message item; role defaults to user when absent.
    assert _input_item_to_message({"role": "assistant", "content": "yo"}) == {"role": "assistant", "content": "yo"}
    assert _input_item_to_message({"content": "no role"}) == {"role": "user", "content": "no role"}
    # An explicit type:"message" item is handled.
    assert _input_item_to_message({"type": "message", "role": "user", "content": "m"}) == {
        "role": "user",
        "content": "m",
    }
    # Non-message item types (e.g. function_call) and non-dicts are skipped.
    assert _input_item_to_message({"type": "function_call", "name": "f"}) is None
    assert _input_item_to_message(7) is None


def test_input_to_messages_string_array_and_other():
    assert _input_to_messages("hello") == [{"role": "user", "content": "hello"}]
    # A mixed array drops skippable items but keeps the messages.
    assert _input_to_messages([{"role": "user", "content": "a"}, {"type": "function_call"}, "b"]) == [
        {"role": "user", "content": "a"},
        {"role": "user", "content": "b"},
    ]
    # A non-string / non-list input coerces to a single user message.
    assert _input_to_messages(99) == [{"role": "user", "content": "99"}]


def test_responses_to_chat_payload_full_translation():
    request = ResponsesRequest(
        model="m",
        instructions="be terse",
        input="hello",
        max_output_tokens=32,
        temperature=0.5,
        top_p=0.9,
        tools=[{"type": "function", "name": "t", "parameters": {"type": "object"}}],
        tool_choice="auto",
    )
    payload = responses_to_chat_payload(request)

    # instructions -> system message, prepended before the input-derived user message.
    assert payload["messages"][0] == {"role": "system", "content": "be terse"}
    assert payload["messages"][1] == {"role": "user", "content": "hello"}
    # max_output_tokens -> max_tokens so the admission cap applies.
    assert payload["max_tokens"] == 32
    assert payload["model"] == "m"
    assert payload["temperature"] == 0.5
    assert payload["top_p"] == 0.9
    # tools / tool_choice are forwarded verbatim (shapes match OpenAI chat's).
    assert payload["tools"] == [{"type": "function", "name": "t", "parameters": {"type": "object"}}]
    assert payload["tool_choice"] == "auto"


def test_responses_to_chat_payload_no_instructions_no_optional_fields():
    payload = responses_to_chat_payload(ResponsesRequest(input="just this"))
    assert payload["messages"] == [{"role": "user", "content": "just this"}]
    # No instructions -> no leading system message; optional fields omitted when unset.
    assert "max_tokens" not in payload
    assert "temperature" not in payload
    assert "tools" not in payload
    assert "model" not in payload


def test_chat_completion_to_responses_text_and_usage():
    out = chat_completion_to_responses(
        {
            "id": "cmpl-1",
            "model": "srv-model",
            "created": 1234567890,
            "choices": [{"message": {"content": "hi there"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        },
        request_model="req-model",
    )
    assert out["object"] == "response"
    assert out["status"] == "completed"
    assert out["id"] == "resp_cmpl-1"
    assert out["model"] == "srv-model"
    assert out["created_at"] == 1234567890
    assert out["output"][0]["type"] == "message"
    assert out["output"][0]["content"] == [{"type": "output_text", "text": "hi there", "annotations": []}]
    assert out["usage"] == {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
    assert out["incomplete_details"] is None


def test_chat_completion_to_responses_finish_reason_mapping():
    def status_for(fr):
        return chat_completion_to_responses(
            {"choices": [{"message": {"content": "x"}, "finish_reason": fr}]}, request_model="m"
        )

    # length -> incomplete + incomplete_details.reason.
    length = status_for("length")
    assert length["status"] == "incomplete"
    assert length["incomplete_details"] == {"reason": "max_output_tokens"}
    # stop / content_filter / unknown -> completed.
    assert status_for("stop")["status"] == "completed"
    assert status_for("content_filter")["status"] == "completed"
    assert status_for("something-else")["status"] == "completed"


def test_chat_completion_to_responses_tool_calls_and_bad_args():
    out = chat_completion_to_responses(
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {"id": "c1", "function": {"name": "f", "arguments": '{"a": 1}'}},
                            {"function": {"name": "g", "arguments": {"already": "obj"}}},
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        request_model="m",
    )
    function_calls = [item for item in out["output"] if item["type"] == "function_call"]
    assert function_calls[0]["name"] == "f"
    assert function_calls[0]["call_id"] == "c1"
    assert function_calls[0]["arguments"] == '{"a": 1}'
    # A non-string arguments object is serialized to a JSON string; missing id gets generated.
    assert function_calls[1]["arguments"] == '{"already": "obj"}'
    assert function_calls[1]["call_id"].startswith("call_")
    # content is None with only tool_calls -> no empty message item, just the function_call.
    assert not any(item["type"] == "message" for item in out["output"])
    # Missing usage defaults to zeros; total is derived from input+output.
    assert out["usage"] == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    assert out["id"].startswith("resp_")
    assert out["model"] == "m"


def test_chat_completion_to_responses_list_content_parts_and_empty():
    # A content-part array on the completion is projected to output_text.
    out = chat_completion_to_responses(
        {"choices": [{"message": {"content": [{"type": "output_text", "text": "p1"}, {"type": "other"}]}}]},
        request_model="m",
    )
    assert out["output"][0]["content"] == [{"type": "output_text", "text": "p1", "annotations": []}]

    # A completion with no choices still yields a well-formed empty message item.
    empty = chat_completion_to_responses({}, request_model="m")
    assert empty["output"][0]["type"] == "message"
    assert empty["output"][0]["content"] == [{"type": "output_text", "text": "", "annotations": []}]
    assert empty["status"] == "completed"
    assert empty["model"] == "m"
