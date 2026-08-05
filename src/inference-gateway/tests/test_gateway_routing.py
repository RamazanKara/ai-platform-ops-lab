import json
import logging

import httpx
from app import inference_api, streaming
from app.main import create_app
from app.policy import ModelRoute, ModelRoutingPolicy
from app.runtime_client import sanitize_chat_completion
from app.settings import Settings
from fastapi.testclient import TestClient

from tests.gateway_support import (
    FakeRuntimeClient,
    _tool_settings,
)


class _BackendAwareFake:
    """Fake runtime client that fails on configured backends and records call order."""

    def __init__(self, fail_backends, error):
        self.fail_backends = set(fail_backends)
        self.error = error
        self.backends_called = []

    async def chat_completions(self, payload, headers=None, backend=None):
        self.backends_called.append(backend)
        if backend in self.fail_backends:
            raise self.error
        return {
            "id": "x",
            "object": "chat.completion",
            "choices": [{"message": {"role": "assistant", "content": f"from {backend}"}}],
        }

    async def stream_chat_completions(self, payload, headers=None, backend=None):
        self.backends_called.append(backend)
        if backend in self.fail_backends:
            raise self.error
        # The yield below makes this an async generator; the failing path raises on
        # first iteration before reaching it.
        yield b'data: {"choices":[]}\n\n'

    async def health(self, backend=None):
        return {"status": "ok", "backend": backend}


def _status_error(status_code):
    request = httpx.Request("POST", "http://runtime/v1/chat/completions")
    return httpx.HTTPStatusError("upstream", request=request, response=httpx.Response(status_code, request=request))


def _fallback_app(allow_streaming=False):
    app = create_app(_tool_settings(allow_streaming=allow_streaming))
    app.state.model_routing_policy = ModelRoutingPolicy(
        routes=(
            ModelRoute("primary-model", "vllm", fallbacks=("backup-model",)),
            ModelRoute("backup-model", "ollama"),
        )
    )
    return app


def test_chat_completion_fails_over_to_fallback_runtime():
    app = _fallback_app()
    fake = _BackendAwareFake(fail_backends={"vllm"}, error=_status_error(503))
    app.state.runtime_client = fake
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json={"model": "primary-model", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "from ollama"
    assert fake.backends_called == ["vllm", "ollama"]


def test_chat_completion_does_not_fail_over_on_client_error():
    app = _fallback_app()
    fake = _BackendAwareFake(fail_backends={"vllm"}, error=_status_error(400))
    app.state.runtime_client = fake
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json={"model": "primary-model", "messages": [{"role": "user", "content": "hi"}]},
    )

    # A 4xx is a client error the fallback runtime would also reject: no failover.
    assert response.status_code == 502
    assert response.json()["detail"]["runtime_status"] == 400
    assert fake.backends_called == ["vllm"]


def test_streaming_fails_over_before_first_byte():
    app = _fallback_app(allow_streaming=True)
    fake = _BackendAwareFake(fail_backends={"vllm"}, error=_status_error(503))
    app.state.runtime_client = fake
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json={"stream": True, "model": "primary-model", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert fake.backends_called == ["vllm", "ollama"]


def test_usage_endpoint_reports_usage_and_estimated_cost():
    app = create_app(
        _tool_settings(
            sandbox_budget_enabled=True,
            sandbox_estimated_token_budget=1000,
            usd_per_1k_tokens=2.0,
        )
    )
    app.state.runtime_client = FakeRuntimeClient(response={"id": "x", "object": "chat.completion", "choices": []})
    client = TestClient(app)

    # Drive some usage, then read it back with the cost estimate.
    client.post(
        "/v1/chat/completions",
        headers={"X-Sandbox-ID": "team-a"},
        json={"messages": [{"role": "user", "content": "hello there"}], "max_tokens": 100},
    )
    usage = client.get("/v1/usage", headers={"X-Sandbox-ID": "team-a"})

    assert usage.status_code == 200
    body = usage.json()
    assert body["sandbox_id"] == "team-a"
    assert body["currency"] == "USD"
    assert body["usd_per_1k_tokens"] == 2.0
    tokens = body["usage"]["estimated_tokens"]
    assert tokens > 0
    assert body["estimated_cost"] == round((tokens / 1000.0) * 2.0, 6)


def test_canary_target_selects_by_roll():
    policy = ModelRoutingPolicy(
        routes=(
            ModelRoute("primary", "vllm", canary_model_id="canary", canary_weight=0.3),
            ModelRoute("canary", "ollama"),
        )
    )
    primary = policy.resolve("primary", "primary")
    assert policy.canary_target(primary, 0.1).model_id == "canary"  # roll < weight
    assert policy.canary_target(primary, 0.5).model_id == "primary"  # roll >= weight


def test_canary_weight_one_routes_all_traffic_to_canary():
    app = create_app(_tool_settings())
    app.state.model_routing_policy = ModelRoutingPolicy(
        routes=(
            ModelRoute("primary-model", "vllm", canary_model_id="canary-model", canary_weight=1.0),
            ModelRoute("canary-model", "ollama"),
        )
    )
    fake = FakeRuntimeClient(response={"id": "x", "object": "chat.completion", "choices": []})
    app.state.runtime_client = fake
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json={"model": "primary-model", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert fake.payload["model"] == "canary-model"
    assert fake.backend == "ollama"


def test_canary_weight_zero_routes_to_primary():
    app = create_app(_tool_settings())
    app.state.model_routing_policy = ModelRoutingPolicy(
        routes=(
            ModelRoute("primary-model", "vllm", canary_model_id="canary-model", canary_weight=0.0),
            ModelRoute("canary-model", "ollama"),
        )
    )
    fake = FakeRuntimeClient(response={"id": "x", "object": "chat.completion", "choices": []})
    app.state.runtime_client = fake
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json={"model": "primary-model", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert fake.payload["model"] == "primary-model"


def test_shadow_request_is_scheduled(monkeypatch):
    captured = {}

    def fake_schedule(client, shadow_route, payload, request):
        captured["shadow_model"] = shadow_route.model_id

    monkeypatch.setattr(inference_api, "_schedule_shadow", fake_schedule)
    app = create_app(_tool_settings())
    app.state.model_routing_policy = ModelRoutingPolicy(
        routes=(
            ModelRoute("primary-model", "vllm", shadow_model_id="shadow-model"),
            ModelRoute("shadow-model", "ollama"),
        )
    )
    app.state.runtime_client = FakeRuntimeClient(response={"id": "x", "object": "chat.completion", "choices": []})
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json={"model": "primary-model", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert captured["shadow_model"] == "shadow-model"


def test_batch_processes_multiple_requests():
    app = create_app(_tool_settings())
    app.state.runtime_client = FakeRuntimeClient(
        response={"id": "x", "object": "chat.completion", "choices": [{"message": {"content": "ok"}}]}
    )
    client = TestClient(app)

    response = client.post(
        "/v1/batch-inference",
        json={
            "requests": [
                {"messages": [{"role": "user", "content": "one"}]},
                {"messages": [{"role": "user", "content": "two"}]},
            ]
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "batch"
    assert body["count"] == 2
    statuses = sorted(item["status_code"] for item in body["results"])
    assert statuses == [200, 200]


def test_batch_rejects_oversized_batch():
    app = create_app(_tool_settings(max_batch_requests=1))
    app.state.runtime_client = FakeRuntimeClient(response={"id": "x", "object": "chat.completion", "choices": []})
    client = TestClient(app)

    response = client.post(
        "/v1/batch-inference",
        json={
            "requests": [
                {"messages": [{"role": "user", "content": "one"}]},
                {"messages": [{"role": "user", "content": "two"}]},
            ]
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "batch_too_large"


def test_batch_reports_per_item_errors_without_failing_batch():
    app = create_app(_tool_settings(allowed_models=("approved-model",)))
    app.state.runtime_client = FakeRuntimeClient(response={"id": "x", "object": "chat.completion", "choices": []})
    client = TestClient(app)

    response = client.post(
        "/v1/batch-inference",
        json={
            "requests": [
                {"model": "approved-model", "messages": [{"role": "user", "content": "ok"}]},
                {"model": "rogue-model", "messages": [{"role": "user", "content": "bad"}]},
            ]
        },
    )

    assert response.status_code == 200
    results = {item["index"]: item for item in response.json()["results"]}
    assert results[0]["status_code"] == 200
    assert results[1]["status_code"] == 400
    assert results[1]["error"]["reason"] == "model_not_allowed"


def test_chat_completion_metrics_use_endpoint_route_label():
    # Regression: the handler reused the `route` variable for both the Prometheus label
    # ("/v1/chat/completions") and the resolved ModelRoute, so a successful request
    # recorded the ModelRoute repr as the `route` label instead of the request path.
    settings = Settings(
        runtime_backend="vllm",
        ollama_base_url="http://ollama:11434",
        vllm_base_url="http://vllm:8000",
        model_id="default-model",
        request_timeout_seconds=5,
    )
    app = create_app(settings)
    app.state.runtime_client = FakeRuntimeClient(
        response={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
        }
    )
    client = TestClient(app)

    completion = client.post(
        "/v1/chat/completions",
        json={"model": "custom-model", "messages": [{"role": "user", "content": "hello"}]},
    )
    assert completion.status_code == 200

    metrics = client.get("/metrics").text
    assert 'route="/v1/chat/completions"' in metrics
    assert "ModelRoute(" not in metrics


def test_runtime_response_removes_reasoning_metadata():
    payload = {
        "id": "chatcmpl-test",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "hello",
                    "reasoning": "internal reasoning",
                    "reasoning_content": "internal reasoning",
                    "thinking": "internal reasoning",
                }
            }
        ],
    }

    response = sanitize_chat_completion(payload)

    assert response["choices"][0]["message"] == {
        "role": "assistant",
        "content": "hello",
    }
    assert "reasoning" in payload["choices"][0]["message"]


def test_streaming_chat_completion_is_passed_through_when_enabled():
    settings = Settings(
        runtime_backend="ollama",
        ollama_base_url="http://ollama:11434",
        vllm_base_url="http://vllm:8000",
        model_id="default-model",
        request_timeout_seconds=5,
        allow_streaming=True,
    )
    app = create_app(settings)
    fake = FakeRuntimeClient()
    fake.stream_chunks = [
        b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    app.state.runtime_client = fake
    client = TestClient(app)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        },
    ) as response:
        body = response.read()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert b"hel" in body
    assert b"[DONE]" in body
    assert fake.payload["stream"] is True
    assert fake.calls == 1


def test_streaming_chat_completion_records_usage_latency_and_audit(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    settings = Settings(
        runtime_backend="ollama",
        ollama_base_url="http://ollama:11434",
        vllm_base_url="http://vllm:8000",
        model_id="default-model",
        request_timeout_seconds=5,
        allow_streaming=True,
    )
    app = create_app(settings)
    fake = FakeRuntimeClient()
    fake.stream_chunks = [
        b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n',
        # Terminal usage chunk carries the token counts for the streamed request.
        b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}\n\n',
        b"data: [DONE]\n\n",
    ]
    app.state.runtime_client = fake
    client = TestClient(app)

    def _total_tokens() -> float:
        return sum(
            float(line.rsplit(" ", 1)[1])
            for line in client.get("/metrics").text.splitlines()
            if line.startswith("inference_gateway_tokens_total") and 'token_type="total_tokens"' in line
        )

    before_tokens = _total_tokens()

    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"X-Request-ID": "stream-audit-1"},
        json={"stream": True, "messages": [{"role": "user", "content": "hello"}]},
    ) as response:
        body = response.read()

    assert response.status_code == 200
    assert b"[DONE]" in body
    # Audit recording is deferred to true end-of-stream: parse the emitted JSON event.
    audit_event = next(
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "ai_platform_ops_lab.audit" and '"stream-audit-1"' in record.getMessage()
    )
    # The bug recorded status 200, ~0 latency, and no usage before bytes flowed; assert the fix.
    assert audit_event["status_code"] == 200
    assert audit_event["latency_ms"] > 0
    assert audit_event["usage"]["total_tokens"] == 10
    # Token usage is exported to Prometheus from the parsed terminal usage chunk (10 here).
    assert _total_tokens() == before_tokens + 10.0


def test_streaming_mid_stream_upstream_error_emits_terminal_event_and_records_502(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")

    class MidStreamFailingClient(FakeRuntimeClient):
        async def stream_chat_completions(self, payload, headers=None, backend=None):
            self.calls += 1
            self.payload = payload
            self.headers = headers or {}
            self.backend = backend
            yield b'data: {"choices":[{"delta":{"content":"par"}}]}\n\n'
            raise httpx.ReadError("upstream dropped mid-stream")

    settings = Settings(
        runtime_backend="ollama",
        ollama_base_url="http://ollama:11434",
        vllm_base_url="http://vllm:8000",
        model_id="default-model",
        request_timeout_seconds=5,
        allow_streaming=True,
    )
    app = create_app(settings)
    app.state.runtime_client = MidStreamFailingClient()
    client = TestClient(app)

    def _requests_502() -> float:
        # Sum across backend label-sets: more than one backend can have a 502 series
        # for this route (e.g. once cross-runtime fallback records a fallback backend),
        # so picking the first matching line would be ambiguous.
        total = 0.0
        for line in client.get("/metrics").text.splitlines():
            if (
                line.startswith("inference_gateway_requests_total")
                and 'route="/v1/chat/completions"' in line
                and 'status="502"' in line
            ):
                total += float(line.rsplit(" ", 1)[1])
        return total

    before_502 = _requests_502()

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "hello"}]},
    ) as response:
        body = response.read()

    # Headers were already sent (HTTP 200), so the failure is surfaced as a terminal SSE
    # error event and the recorded status maps to 502.
    assert response.status_code == 200
    assert b"par" in body
    assert b"upstream_error" in body
    assert "upstream dropped mid-stream" not in body.decode("utf-8")
    assert '"status_code": 502' in caplog.text
    # The deferred recording maps the mid-stream failure to a 502 request metric.
    assert _requests_502() == before_502 + 1.0


def test_streaming_pre_first_byte_upstream_error_returns_502():
    class PreFirstByteFailingClient(FakeRuntimeClient):
        async def stream_chat_completions(self, payload, headers=None, backend=None):
            self.calls += 1
            self.payload = payload
            # Fail before yielding any bytes (e.g. response.raise_for_status()).
            raise httpx.HTTPStatusError(
                "runtime unavailable",
                request=httpx.Request("POST", "http://ollama:11434/v1/chat/completions"),
                response=httpx.Response(
                    503,
                    request=httpx.Request("POST", "http://ollama:11434/v1/chat/completions"),
                ),
            )
            yield b""  # pragma: no cover - unreachable, marks this an async generator

    settings = Settings(
        runtime_backend="ollama",
        ollama_base_url="http://ollama:11434",
        vllm_base_url="http://vllm:8000",
        model_id="default-model",
        request_timeout_seconds=5,
        allow_streaming=True,
    )
    app = create_app(settings)
    app.state.runtime_client = PreFirstByteFailingClient()
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json={"stream": True, "messages": [{"role": "user", "content": "hello"}]},
    )

    # No bytes were sent yet, so a clean 502 JSON error is returned instead of a 200.
    assert response.status_code == 502
    assert response.json()["detail"]["runtime_status"] == 503


def test_usage_from_sse_chunk_skips_parsing_delta_chunks_without_usage(monkeypatch):
    def _fail(*args, **kwargs):
        raise AssertionError("chunks without a usage member must not be JSON-parsed")

    monkeypatch.setattr(streaming.json, "loads", _fail)

    chunk = b'data: {"choices":[{"delta":{"content":"hel"}}]}\n\ndata: [DONE]\n\n'
    assert streaming._usage_from_sse_chunk(chunk) is None


def test_usage_from_sse_chunk_extracts_terminal_usage_object():
    chunk = b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}\n\n'
    assert streaming._usage_from_sse_chunk(chunk) == {
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "total_tokens": 10,
    }


def test_usage_from_sse_chunk_ignores_null_usage():
    # Interim events in some runtimes carry `"usage": null`; the literal is present,
    # so the line is parsed and rejected by the isinstance check, same as before.
    chunk = b'data: {"choices":[{"delta":{"content":"x"}}],"usage": null}\n\n'
    assert streaming._usage_from_sse_chunk(chunk) is None


def test_usage_from_sse_chunk_finds_usage_in_multi_event_chunk():
    chunk = (
        b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'
        b'data: {"choices":[],"usage":{"total_tokens":4}}\n\n'
        b"data: [DONE]\n\n"
    )
    assert streaming._usage_from_sse_chunk(chunk) == {"total_tokens": 4}


def test_chat_completion_uses_default_model_when_model_is_omitted():
    settings = Settings(
        runtime_backend="ollama",
        ollama_base_url="http://ollama:11434",
        vllm_base_url="http://vllm:8000",
        model_id="default-model",
        request_timeout_seconds=5,
    )
    app = create_app(settings)
    fake = FakeRuntimeClient(
        response={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "choices": [{"message": {"role": "assistant", "content": "hello from Ollama"}}],
        }
    )
    app.state.runtime_client = fake
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 200
    assert fake.payload["messages"][0]["role"] == "user"
    assert fake.payload["model"] == "default-model"


def test_chat_completion_propagates_trace_headers_and_returns_request_context():
    settings = Settings(
        runtime_backend="ollama",
        ollama_base_url="http://ollama:11434",
        vllm_base_url="http://vllm:8000",
        model_id="default-model",
        request_timeout_seconds=5,
    )
    app = create_app(settings)
    fake = FakeRuntimeClient(
        response={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "choices": [{"message": {"role": "assistant", "content": "traceable response"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
    )
    app.state.runtime_client = fake
    client = TestClient(app)
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

    response = client.post(
        "/v1/chat/completions",
        headers={
            "X-Request-ID": "req-123",
            "X-Sandbox-ID": "team-a-lab",
            "traceparent": traceparent,
        },
        json={"messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req-123"
    assert response.headers["X-Sandbox-ID"] == "team-a-lab"
    assert response.headers["traceparent"] == traceparent
    assert fake.headers["X-Request-ID"] == "req-123"
    assert fake.headers["X-Sandbox-ID"] == "team-a-lab"
    assert fake.headers["traceparent"] == traceparent


def test_chat_completion_rejects_invalid_sandbox_id():
    client = TestClient(
        create_app(
            Settings(
                runtime_backend="ollama",
                ollama_base_url="http://ollama:11434",
                vllm_base_url="http://vllm:8000",
                model_id="default-model",
                request_timeout_seconds=5,
            )
        )
    )

    response = client.post(
        "/v1/chat/completions",
        headers={"X-Sandbox-ID": "bad sandbox"},
        json={"messages": [{"role": "user", "content": "hello"}]},
    )

    assert response.status_code == 400
    assert "sandbox id" in response.json()["detail"]
