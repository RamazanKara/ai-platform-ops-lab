import json
import logging
import types

import pytest
from app.audit import payload_fingerprint
from app.main import create_app
from app.request_context import _request_id_from_header
from app.streaming import _rewrite_stream_segment
from fastapi.testclient import TestClient

from tests.gateway_support import (
    FakeRuntimeClient,
    _tool_settings,
)


def test_request_id_rejects_control_characters():
    # Control bytes echoed into the X-Request-ID response header would be rejected
    # by the HTTP stack at write time (an unhandled 500), so validation must catch them.
    fake_request = types.SimpleNamespace(headers={"x-request-id": "bad\x01id"})

    with pytest.raises(ValueError, match="visible ASCII"):
        _request_id_from_header(fake_request)


# --- Phase 1: enforcement-hole regressions ---------------------------------


def test_max_completion_tokens_is_capped_like_max_tokens():
    # The modern max_completion_tokens field must be enforced even when the legacy
    # max_tokens is absent, or the completion cap is bypassable by field name.
    app = create_app(_tool_settings(max_completion_tokens=16))
    fake = FakeRuntimeClient(response={"id": "x", "object": "chat.completion", "choices": []})
    app.state.runtime_client = fake
    client = TestClient(app)

    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "max_completion_tokens": 999},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "max_tokens_too_large"
    assert fake.calls == 0


def test_max_completion_tokens_within_cap_is_forwarded():
    app = create_app(_tool_settings(max_completion_tokens=64))
    fake = FakeRuntimeClient(response={"id": "x", "object": "chat.completion", "choices": []})
    app.state.runtime_client = fake
    client = TestClient(app)

    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "max_completion_tokens": 32},
    )

    assert resp.status_code == 200
    assert fake.payload["max_completion_tokens"] == 32


def test_n_completions_capped_by_admission():
    app = create_app(_tool_settings(max_completions_per_request=1))
    fake = FakeRuntimeClient(response={"id": "x", "object": "chat.completion", "choices": []})
    app.state.runtime_client = fake
    client = TestClient(app)

    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "n": 5},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "too_many_completions"
    assert fake.calls == 0


def test_budget_delta_multiplies_completions_and_meters_images():
    from app.budget import budget_delta

    settings = _tool_settings(max_completion_tokens=100, image_part_token_estimate=500)
    # ceil(5/4)=2 prompt tokens + 10 completion tokens.
    single = budget_delta(settings, {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 10})
    assert single.estimated_tokens == 2 + 10
    # n multiplies only the completion estimate: 2 + 10*3.
    multi = budget_delta(settings, {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 10, "n": 3})
    assert multi.estimated_tokens == 2 + 10 * 3
    # An image_url part adds the flat per-image estimate: ceil(2/4)=1 + 500 image + 10 completion.
    with_image = budget_delta(
        settings,
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hi"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
                    ],
                }
            ],
            "max_tokens": 10,
        },
    )
    assert with_image.estimated_tokens == 1 + 500 + 10


def test_oversized_image_part_is_rejected():
    app = create_app(_tool_settings(max_image_bytes=16))
    fake = FakeRuntimeClient(response={"id": "x", "object": "chat.completion", "choices": []})
    app.state.runtime_client = fake
    client = TestClient(app)
    oversized = "A" * 400  # ~300 decoded bytes, well over the 16-byte cap.

    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + oversized}}],
                }
            ]
        },
    )

    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "image_too_large"
    assert fake.calls == 0


class _UsageAwareStreamClient(FakeRuntimeClient):
    """Streams a usage event only when the caller asked for it (like vLLM)."""

    async def stream_chat_completions(self, payload, headers=None, backend=None):
        self.calls += 1
        self.payload = payload
        self.headers = headers or {}
        self.backend = backend
        yield b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        stream_options = payload.get("stream_options") or {}
        if stream_options.get("include_usage"):
            yield b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}\n\n'
        yield b"data: [DONE]\n\n"


def test_streaming_injects_include_usage_and_filters_it_when_not_requested(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    app = create_app(_tool_settings(allow_streaming=True))
    fake = _UsageAwareStreamClient()
    app.state.runtime_client = fake
    client = TestClient(app)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"X-Request-ID": "s-inject"},
        json={"stream": True, "messages": [{"role": "user", "content": "hello"}]},
    ) as response:
        body = response.read()

    # The gateway injected include_usage so the runtime emitted usage...
    assert fake.payload["stream_options"]["include_usage"] is True
    # ...usage was metered into the audit receipt...
    event = next(
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "ai_platform_ops_lab.audit" and '"s-inject"' in record.getMessage()
    )
    assert event["usage"]["total_tokens"] == 10
    # ...but the client, which did not request usage, does not see the usage event.
    assert b"usage" not in body
    assert b"hi" in body
    assert b"[DONE]" in body


def test_streaming_keeps_usage_event_when_client_requests_it():
    app = create_app(_tool_settings(allow_streaming=True))
    fake = _UsageAwareStreamClient()
    app.state.runtime_client = fake
    client = TestClient(app)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "hello"}],
        },
    ) as response:
        body = response.read()

    # A caller that explicitly asked for usage still receives the usage event.
    assert b'"usage"' in body
    assert b"[DONE]" in body


def test_streaming_strips_reasoning_deltas():
    app = create_app(_tool_settings(allow_streaming=True))
    fake = FakeRuntimeClient()
    fake.stream_chunks = [
        b'data: {"choices":[{"delta":{"content":"answer","reasoning":"secret chain"}}]}\n\n',
        b'data: {"choices":[{"delta":{"thinking":"more secret"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    app.state.runtime_client = fake
    client = TestClient(app)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "q"}]},
    ) as response:
        body = response.read()

    # The visible content survives; reasoning/thinking chain-of-thought does not.
    assert b"answer" in body
    assert b"reasoning" not in body
    assert b"secret chain" not in body
    assert b"thinking" not in body
    assert b"more secret" not in body
    assert b"[DONE]" in body


def test_batch_receipt_emitted_to_uvicorn_logger(caplog):
    # Batch receipts must reach pod logs (uvicorn.error) like inference_request events,
    # or a verifier sees the chain advance past events it cannot observe.
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    app = create_app(_tool_settings())
    app.state.runtime_client = FakeRuntimeClient(response={"id": "x", "object": "chat.completion", "choices": []})
    client = TestClient(app)

    resp = client.post(
        "/v1/batch-inference",
        json={"requests": [{"messages": [{"role": "user", "content": "hi"}]}]},
    )

    assert resp.status_code == 200
    batch_events = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "uvicorn.error" and '"batch_request"' in record.getMessage()
    ]
    assert batch_events
    assert batch_events[0]["event"] == "batch_request"


def test_rewrite_stream_segment_passes_plain_deltas_through_byte_identical():
    # The hot path (ordinary token deltas) must not be altered or re-serialized.
    segment = b'data: {"choices":[{"delta":{"content":"tok"}}]}\n\n'
    assert _rewrite_stream_segment(segment, drop_usage_only=True, strip_reasoning=True) == segment


# --- Phase 2: prompt-secret modes and cloud key patterns -------------------


def test_prompt_secret_block_mode_rejects_by_default():
    app = create_app(_tool_settings())
    fake = FakeRuntimeClient(response={"id": "x", "object": "chat.completion", "choices": []})
    app.state.runtime_client = fake
    client = TestClient(app)

    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "here is a key: AKIAIOSFODNN7EXAMPLE"}]},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "prompt_secret_detected"
    assert fake.calls == 0


def test_request_body_limit_rejects_before_runtime_parsing():
    app = create_app(_tool_settings(max_request_body_bytes=128))
    fake = FakeRuntimeClient(response={"id": "x", "object": "chat.completion", "choices": []})
    app.state.runtime_client = fake
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "x" * 256}]},
    )

    assert response.status_code == 413
    assert response.json()["detail"]["reason"] == "request_body_too_large"
    assert response.json()["detail"]["limit_bytes"] == 128
    assert fake.calls == 0


def test_prompt_secret_redact_mode_forwards_redacted_prompt():
    app = create_app(_tool_settings(prompt_secret_mode="redact"))
    fake = FakeRuntimeClient(response={"id": "x", "object": "chat.completion", "choices": []})
    app.state.runtime_client = fake
    client = TestClient(app)

    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"}]},
    )

    assert resp.status_code == 200
    assert resp.headers["X-Prompt-Guardrail"] == "redacted"
    # The runtime received the redacted prompt, not the raw credential.
    forwarded = fake.payload["messages"][0]["content"]
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in forwarded
    assert "[REDACTED:github_token]" in forwarded


def test_prompt_secret_block_mode_scans_tool_schema_and_call_arguments():
    app = create_app(_tool_settings())
    fake = FakeRuntimeClient(response={"id": "x", "object": "chat.completion", "choices": []})
    app.state.runtime_client = fake
    client = TestClient(app)

    schema_secret = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "use the tool"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "description": "credential ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
                    },
                }
            ],
        },
    )
    argument_secret = client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": '{"token":"ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"}',
                            },
                        }
                    ],
                }
            ]
        },
    )

    assert schema_secret.status_code == 400
    assert argument_secret.status_code == 400
    assert fake.calls == 0


def test_prompt_secret_redact_mode_redacts_tool_call_arguments():
    app = create_app(_tool_settings(prompt_secret_mode="redact"))
    fake = FakeRuntimeClient(response={"id": "x", "object": "chat.completion", "choices": []})
    app.state.runtime_client = fake
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": '{"token":"ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"}',
                            },
                        }
                    ],
                }
            ]
        },
    )

    assert response.status_code == 200
    arguments = fake.payload["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" not in arguments
    assert "[REDACTED:github_token]" in arguments


def test_payload_fingerprint_changes_when_only_tool_arguments_change():
    base = {
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}],
            }
        ]
    }
    changed = json.loads(json.dumps(base))
    changed["messages"][0]["tool_calls"][0]["function"]["arguments"] = '{"tenant":"other"}'

    first = payload_fingerprint(base)
    second = payload_fingerprint(changed)

    assert first["prompt_sha256"] != second["prompt_sha256"]
    assert first["request_sha256"] != second["request_sha256"]


def test_prompt_secret_flag_mode_allows_and_records():
    app = create_app(_tool_settings(prompt_secret_mode="flag"))
    fake = FakeRuntimeClient(response={"id": "x", "object": "chat.completion", "choices": []})
    app.state.runtime_client = fake
    client = TestClient(app)

    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"}]},
    )

    assert resp.status_code == 200
    assert resp.headers["X-Prompt-Guardrail"] == "flagged"
    # Flag mode forwards the prompt unchanged (detection is recorded, not enforced).
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345" in fake.payload["messages"][0]["content"]


def test_google_api_key_detected_by_default():
    app = create_app(_tool_settings())
    fake = FakeRuntimeClient(response={"id": "x", "object": "chat.completion", "choices": []})
    app.state.runtime_client = fake
    client = TestClient(app)

    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "key AIzaSyA1234567890abcdefghijklmnopqrstuv"}]},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "prompt_secret_detected"


def test_prompt_secret_redact_mode_redacts_content_part_text():
    app = create_app(_tool_settings(prompt_secret_mode="redact"))
    fake = FakeRuntimeClient(response={"id": "x", "object": "chat.completion", "choices": []})
    app.state.runtime_client = fake
    client = TestClient(app)

    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "secret AKIAIOSFODNN7EXAMPLE here"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
                    ],
                }
            ]
        },
    )

    assert resp.status_code == 200
    forwarded_parts = fake.payload["messages"][0]["content"]
    text_part = next(part for part in forwarded_parts if part.get("type") == "text")
    assert "AKIAIOSFODNN7EXAMPLE" not in text_part["text"]
    assert "[REDACTED:aws_access_key_id]" in text_part["text"]
    # The image part is preserved untouched.
    assert any(part.get("type") == "image_url" for part in forwarded_parts)


# --- Streaming framing, completion caps, and batch attribution ---


class _SplitTerminatorStreamClient(FakeRuntimeClient):
    """Flushes the final event's blank-line terminator in its own chunk."""

    async def stream_chat_completions(self, payload, headers=None, backend=None):
        self.calls += 1
        self.payload = payload
        yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: [DONE]\n'
        yield b"\n"


def test_streaming_preserves_terminator_split_across_chunks():
    app = create_app(_tool_settings(allow_streaming=True))
    app.state.runtime_client = _SplitTerminatorStreamClient()
    client = TestClient(app)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ) as response:
        body = response.read()

    # The final event keeps its blank-line terminator even when it arrived in its own chunk.
    assert body.endswith(b"data: [DONE]\n\n")


def test_both_completion_fields_are_capped():
    app = create_app(_tool_settings(max_completion_tokens=16))
    fake = FakeRuntimeClient(response={"id": "x", "object": "chat.completion", "choices": []})
    app.state.runtime_client = fake
    client = TestClient(app)

    # max_completion_tokens is within the cap but the legacy max_tokens is not; because both
    # are forwarded to the runtime, either exceeding the cap must be rejected.
    resp = client.post(
        "/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "max_completion_tokens": 8,
            "max_tokens": 1000000,
        },
    )

    assert resp.status_code == 400
    assert resp.json()["detail"]["reason"] == "max_tokens_too_large"
    assert fake.calls == 0


def test_rewrite_stream_segment_drops_usage_event_without_stray_blank():
    segment = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: {"choices":[],"usage":{"total_tokens":3}}\n\n'
    out = _rewrite_stream_segment(segment, drop_usage_only=True, strip_reasoning=True)
    assert b'"usage"' not in out
    assert b"hi" in out
    # Removing the usage event must not leave a triple-newline artifact.
    assert b"\n\n\n" not in out


def test_batch_item_records_prompt_guardrail_action(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    app = create_app(_tool_settings(prompt_secret_mode="flag"))
    app.state.runtime_client = FakeRuntimeClient(response={"id": "x", "object": "chat.completion", "choices": []})
    client = TestClient(app)

    resp = client.post(
        "/v1/batch-inference",
        json={
            "requests": [
                {"messages": [{"role": "user", "content": "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"}]},
                {"messages": [{"role": "user", "content": "a clean prompt"}]},
            ]
        },
    )

    assert resp.status_code == 200
    event = next(
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "ai_platform_ops_lab.audit" and '"batch_request"' in record.getMessage()
    )
    items = {entry["index"]: entry for entry in event["items"]}
    # The item carrying a secret is attributed individually; the clean item is not.
    assert items[0]["prompt_guardrail_action"] == "flagged"
    assert "prompt_guardrail_action" not in items[1]


# --- OpenAI-shaped error envelope (task 3.1) ---
