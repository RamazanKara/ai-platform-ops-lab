"""Tests for Anthropic-shaped streaming on POST /v1/messages.

Two layers are covered. The translator tests drive
:class:`app.messages.AnthropicStreamTranslator` directly with recorded OpenAI chunk
streams and assert the exact Anthropic event sequence, which is where the protocol
fidelity lives. The endpoint tests then assert the streaming path is the same governed
path as chat: the streaming admission toggle applies, usage is forced on and metered,
the output guardrail still runs at end-of-stream, and a mid-stream runtime failure
terminates as an Anthropic ``error`` event rather than an OpenAI-shaped one.
"""

import json
import logging

import httpx
import pytest
from app.main import create_app
from app.messages import AnthropicStreamTranslator, iter_sse_data_objects
from app.settings import Settings
from fastapi.testclient import TestClient


class FakeStreamingRuntimeClient:
    """Yields canned SSE chunks and records the payload the gateway forwarded."""

    def __init__(self, stream_chunks=None, error=None, stream_error=None):
        self.stream_chunks = stream_chunks or []
        self.error = error
        self.stream_error = stream_error
        self.payload = None
        self.headers = None
        self.backend = None
        self.calls = 0

    async def chat_completions(self, payload, headers=None, backend=None):
        raise AssertionError("streaming test must not call the non-streaming path")

    async def stream_chat_completions(self, payload, headers=None, backend=None):
        self.calls += 1
        self.payload = payload
        self.headers = headers or {}
        self.backend = backend
        if self.error:
            raise self.error
        for chunk in self.stream_chunks:
            yield chunk
        if self.stream_error:
            raise self.stream_error

    async def health(self, backend=None):
        return {"status": "ok", "backend": backend}


def _settings(**overrides):
    base = {
        "runtime_backend": "ollama",
        "ollama_base_url": "http://ollama:11434",
        "vllm_base_url": "http://vllm:8000",
        "model_id": "default-model",
        "request_timeout_seconds": 5,
        "allow_streaming": True,
    }
    base.update(overrides)
    return Settings(**base)


def _sse_events(body: bytes) -> list[tuple[str, dict]]:
    """Parse a raw Anthropic SSE body into ``(event_name, decoded_data)`` pairs."""
    events: list[tuple[str, dict]] = []
    event_name = None
    for line in body.decode("utf-8").split("\n"):
        stripped = line.strip()
        if stripped.startswith("event:"):
            event_name = stripped[6:].strip()
        elif stripped.startswith("data:") and event_name is not None:
            events.append((event_name, json.loads(stripped[5:].strip())))
            event_name = None
    return events


def _translate(chunks: list[dict], *, request_model: str | None = None) -> list[tuple[str, dict]]:
    """Run chunks through the translator and return the parsed event sequence."""
    translator = AnthropicStreamTranslator(request_model=request_model)
    body = b"".join(translator.feed(chunk) for chunk in chunks)
    body += translator.finish()
    return _sse_events(body)


def _chunk(delta: dict, *, finish_reason=None, chunk_id="chatcmpl-1", model="default-model"):
    return {
        "id": chunk_id,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def test_translator_emits_the_anthropic_text_event_sequence():
    events = _translate(
        [
            _chunk({"role": "assistant", "content": "hel"}),
            _chunk({"content": "lo"}),
            _chunk({}, finish_reason="stop"),
        ]
    )

    assert [name for name, _ in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    start = events[0][1]["message"]
    assert start["id"] == "chatcmpl-1"
    assert start["type"] == "message"
    assert start["role"] == "assistant"
    assert start["model"] == "default-model"
    assert start["content"] == []
    assert events[1][1] == {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    }
    assert [event[1]["delta"]["text"] for event in events if event[0] == "content_block_delta"] == ["hel", "lo"]
    assert events[4][1] == {"type": "content_block_stop", "index": 0}
    assert events[5][1]["delta"] == {"stop_reason": "end_turn", "stop_sequence": None}
    assert events[6][1] == {"type": "message_stop"}


def test_translator_falls_back_to_the_request_model_when_the_chunk_omits_it():
    events = _translate([{"choices": [{"delta": {"content": "hi"}}]}], request_model="claude-shaped")

    assert events[0][1]["message"]["model"] == "claude-shaped"
    assert events[0][1]["message"]["id"].startswith("msg_")


def test_translator_emits_tool_use_blocks_with_input_json_deltas():
    events = _translate(
        [
            _chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_abc",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": ""},
                        }
                    ]
                }
            ),
            _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '{"city":'}}]}),
            _chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"Berlin"}'}}]}),
            _chunk({}, finish_reason="tool_calls"),
        ]
    )

    assert [name for name, _ in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert events[1][1]["content_block"] == {
        "type": "tool_use",
        "id": "call_abc",
        "name": "get_weather",
        "input": {},
    }
    partials = [event[1]["delta"]["partial_json"] for event in events if event[0] == "content_block_delta"]
    assert "".join(partials) == '{"city":"Berlin"}'
    assert all(event[1]["delta"]["type"] == "input_json_delta" for event in events if event[0] == "content_block_delta")
    assert events[5][1]["delta"]["stop_reason"] == "tool_use"


def test_translator_closes_the_text_block_before_opening_a_tool_block():
    events = _translate(
        [
            _chunk({"content": "let me look"}),
            _chunk(
                {
                    "tool_calls": [
                        {"index": 0, "id": "call_1", "function": {"name": "search", "arguments": "{}"}},
                    ]
                }
            ),
            _chunk({}, finish_reason="tool_calls"),
        ]
    )

    assert [name for name, _ in events] == [
        "message_start",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "content_block_start",
        "content_block_delta",
        "content_block_stop",
        "message_delta",
        "message_stop",
    ]
    assert events[1][1]["index"] == 0
    assert events[3][1]["index"] == 0
    assert events[4][1]["index"] == 1
    assert events[4][1]["content_block"]["type"] == "tool_use"
    assert events[6][1]["index"] == 1


def test_translator_indexes_parallel_tool_calls_separately():
    events = _translate(
        [
            _chunk({"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "a", "arguments": "{}"}}]}),
            _chunk({"tool_calls": [{"index": 1, "id": "call_2", "function": {"name": "b", "arguments": "{}"}}]}),
            _chunk({}, finish_reason="tool_calls"),
        ]
    )

    starts = [event for name, event in events if name == "content_block_start"]
    assert [start["index"] for start in starts] == [0, 1]
    assert [start["content_block"]["id"] for start in starts] == ["call_1", "call_2"]


def test_translator_reports_runtime_usage_on_the_terminal_message_delta():
    events = _translate(
        [
            _chunk({"content": "hi"}),
            {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}},
        ]
    )

    message_delta = next(event for name, event in events if name == "message_delta")
    assert message_delta["usage"] == {"input_tokens": 11, "output_tokens": 4}
    # message_start cannot know the counts yet, so it carries zeros by contract.
    assert events[0][1]["message"]["usage"] == {"input_tokens": 0, "output_tokens": 0}


def test_translator_defaults_usage_to_zero_output_tokens_when_the_runtime_reports_none():
    events = _translate([_chunk({"content": "hi"})])

    message_delta = next(event for name, event in events if name == "message_delta")
    assert message_delta["usage"] == {"output_tokens": 0}


@pytest.mark.parametrize(
    ("finish_reason", "stop_reason"),
    [
        ("stop", "end_turn"),
        ("length", "max_tokens"),
        ("tool_calls", "tool_use"),
        ("content_filter", "end_turn"),
        ("function_call", "tool_use"),
        (None, "end_turn"),
        ("something_new", "end_turn"),
    ],
)
def test_translator_maps_every_finish_reason_to_an_anthropic_stop_reason(finish_reason, stop_reason):
    events = _translate([_chunk({"content": "hi"}, finish_reason=finish_reason)])

    message_delta = next(event for name, event in events if name == "message_delta")
    assert message_delta["delta"]["stop_reason"] == stop_reason


def test_translator_emits_a_well_formed_message_for_an_empty_stream():
    events = _translate([])

    assert [name for name, _ in events] == ["message_start", "message_delta", "message_stop"]


def test_translator_finish_is_idempotent():
    translator = AnthropicStreamTranslator()
    translator.feed(_chunk({"content": "hi"}))
    first = translator.finish()
    second = translator.finish()

    assert b"message_stop" in first
    assert second == b""


def test_translator_ignores_chunks_fed_after_finish():
    translator = AnthropicStreamTranslator()
    translator.feed(_chunk({"content": "hi"}))
    translator.finish()

    assert translator.feed(_chunk({"content": "late"})) == b""


def test_translator_drops_reasoning_deltas_structurally():
    # Reasoning fields have no path into the Anthropic events: the translator only reads
    # delta.content and delta.tool_calls, so chain-of-thought cannot leak by omission.
    events = _translate(
        [
            _chunk({"reasoning": "secret plan", "reasoning_content": "more", "thinking": "and more"}),
            _chunk({"content": "answer"}),
        ]
    )

    body = json.dumps(events)
    assert "secret plan" not in body
    assert "thinking" not in body
    assert [event["delta"]["text"] for name, event in events if name == "content_block_delta"] == ["answer"]


def test_translator_accumulates_decoded_text_for_the_guardrail_scan():
    translator = AnthropicStreamTranslator()
    translator.feed(_chunk({"content": 'a "quoted" value\n'}))
    translator.feed(_chunk({"content": "second"}))

    # The scan text is the decoded assistant text, not its JSON escaping.
    assert translator.scanned_text == 'a "quoted" value\nsecond'


def test_translator_bounds_the_scanned_text():
    translator = AnthropicStreamTranslator()
    for _ in range(10):
        translator.feed(_chunk({"content": "x" * 50000}))

    assert len(translator.scanned_text) < AnthropicStreamTranslator.SCAN_LIMIT + 50000


def test_translator_flattens_content_part_arrays():
    events = _translate([_chunk({"content": [{"type": "text", "text": "part "}, {"type": "text", "text": "two"}]})])

    assert [event["delta"]["text"] for name, event in events if name == "content_block_delta"] == ["part two"]


def test_iter_sse_data_objects_skips_done_blank_and_undecodable_lines():
    segment = (
        b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
        b"data: not json\n\n"
        b"\n"
        b": comment line\n"
        b'data: {"choices":[{"delta":{"content":"b"}}]}\n\n'
        b"data: [DONE]\n\n"
    )

    parsed = list(iter_sse_data_objects(segment))

    assert [obj["choices"][0]["delta"]["content"] for obj in parsed] == ["a", "b"]


def _stream_messages(client, body):
    with client.stream("POST", "/v1/messages", json=body) as response:
        return response.status_code, response.headers, response.read()


def test_messages_streaming_returns_the_anthropic_event_stream():
    app = create_app(_settings())
    fake = FakeStreamingRuntimeClient(
        stream_chunks=[
            b'data: {"id":"chatcmpl-9","model":"default-model","choices":[{"delta":{"content":"hel"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"lo"},"finish_reason":"stop"}]}\n\n',
            b"data: [DONE]\n\n",
        ]
    )
    app.state.runtime_client = fake
    client = TestClient(app)

    status_code, headers, body = _stream_messages(
        client,
        {"stream": True, "max_tokens": 64, "messages": [{"role": "user", "content": "hello"}]},
    )

    assert status_code == 200
    assert headers["content-type"].startswith("text/event-stream")
    names = [name for name, _ in _sse_events(body)]
    assert names[0] == "message_start"
    assert names[-1] == "message_stop"
    # The OpenAI framing must not leak through to an Anthropic client.
    assert b"[DONE]" not in body
    assert b"chat.completion.chunk" not in body


def test_messages_streaming_forces_usage_reporting_on_the_runtime_request():
    app = create_app(_settings())
    fake = FakeStreamingRuntimeClient(stream_chunks=[b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'])
    app.state.runtime_client = fake
    client = TestClient(app)

    _stream_messages(client, {"stream": True, "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]})

    assert fake.payload["stream"] is True
    assert fake.payload["stream_options"] == {"include_usage": True}
    assert fake.calls == 1


def test_messages_streaming_is_rejected_when_the_streaming_toggle_is_off():
    # The shared ALLOW_STREAMING admission rule governs /v1/messages, so this endpoint
    # does not carry its own answer to whether streaming is permitted.
    app = create_app(_settings(allow_streaming=False))
    app.state.runtime_client = FakeStreamingRuntimeClient()
    client = TestClient(app)

    response = client.post(
        "/v1/messages",
        json={"stream": True, "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "streaming_disabled"


def test_messages_streaming_records_usage_latency_and_audit(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    app = create_app(_settings())
    fake = FakeStreamingRuntimeClient(
        stream_chunks=[
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}\n\n',
            b"data: [DONE]\n\n",
        ]
    )
    app.state.runtime_client = fake
    client = TestClient(app)

    _, _, body = _stream_messages(
        client,
        {"stream": True, "max_tokens": 64, "messages": [{"role": "user", "content": "hello"}]},
    )

    message_delta = next(event for name, event in _sse_events(body) if name == "message_delta")
    assert message_delta["usage"] == {"input_tokens": 7, "output_tokens": 3}

    records = [json.loads(record.message) for record in caplog.records if record.name == "ai_platform_ops_lab.audit"]
    receipt = records[-1]
    assert receipt["status_code"] == 200
    assert receipt["usage"] == {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    assert receipt["record_hash"]


def test_messages_streaming_emits_an_anthropic_error_event_on_a_mid_stream_failure(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    app = create_app(_settings())
    fake = FakeStreamingRuntimeClient(
        stream_chunks=[b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'],
        stream_error=httpx.ReadError("connection reset"),
    )
    app.state.runtime_client = fake
    client = TestClient(app)

    _, _, body = _stream_messages(
        client,
        {"stream": True, "max_tokens": 64, "messages": [{"role": "user", "content": "hello"}]},
    )

    events = _sse_events(body)
    assert events[-1][0] == "error"
    assert events[-1][1]["type"] == "error"
    assert events[-1][1]["error"]["type"] == "api_error"

    records = [json.loads(record.message) for record in caplog.records if record.name == "ai_platform_ops_lab.audit"]
    assert records[-1]["status_code"] == 502
    assert records[-1]["decision"] == "denied"


def test_messages_streaming_reassembles_events_split_across_chunk_boundaries():
    app = create_app(_settings())
    fake = FakeStreamingRuntimeClient(
        stream_chunks=[
            b'data: {"choices":[{"delta":{"con',
            b'tent":"split"}}]}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1}}\n\n',
        ]
    )
    app.state.runtime_client = fake
    client = TestClient(app)

    _, _, body = _stream_messages(
        client,
        {"stream": True, "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]},
    )

    events = _sse_events(body)
    deltas = [event["delta"]["text"] for name, event in events if name == "content_block_delta"]
    assert deltas == ["split"]
    message_delta = next(event for name, event in events if name == "message_delta")
    assert message_delta["usage"] == {"input_tokens": 2, "output_tokens": 1}


def test_messages_streaming_flags_the_output_guardrail_at_end_of_stream():
    app = create_app(
        _settings(
            output_guardrail_enabled=True,
            blocked_content_terms=("forbidden",),
            output_guardrail_mode="flag",
        )
    )
    fake = FakeStreamingRuntimeClient(
        stream_chunks=[b'data: {"choices":[{"delta":{"content":"a forbidden answer"}}]}\n\n'],
    )
    app.state.runtime_client = fake
    client = TestClient(app)

    status_code, _, body = _stream_messages(
        client,
        {"stream": True, "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]},
    )

    # Committed bytes cannot be redacted mid-stream, so the streamed text still reaches
    # the caller; the guardrail's job here is to flag it on the metric.
    assert status_code == 200
    assert b"forbidden" in body
    metrics = client.get("/metrics").text
    flagged = [
        line
        for line in metrics.splitlines()
        if line.startswith("inference_gateway_output_guardrail_total{")
        and "flagged_stream" in line
        and "/v1/messages" in line
    ]
    assert flagged, "streamed /v1/messages guardrail finding was not counted"


def test_messages_streaming_still_enforces_admission_before_the_stream_opens():
    app = create_app(_settings(max_completion_tokens=16))
    fake = FakeStreamingRuntimeClient()
    app.state.runtime_client = fake
    client = TestClient(app)

    response = client.post(
        "/v1/messages",
        json={"stream": True, "max_tokens": 4096, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "max_tokens_too_large"
    assert fake.calls == 0
