"""Tests for the response-path output guardrail, cost metric, and shared cache backend."""

import pytest
from app.cache import RedisResponseCache, ResponseCache, build_response_cache
from app.main import create_app
from app.settings import Settings
from fastapi.testclient import TestClient

from tests.gateway_support import FakeRuntimeClient

GITHUB_TOKEN = "ghp_" + "a" * 30
US_SSN = "123-45-6789"


class FakeRedis:
    """In-memory stand-in for a redis client supporting get/set with TTL."""

    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None):
        self.data[key] = value


def _settings(**overrides):
    base = {
        "runtime_backend": "vllm",
        "ollama_base_url": "http://ollama:11434",
        "vllm_base_url": "http://vllm:8000",
        "model_id": "default-model",
        "request_timeout_seconds": 5,
    }
    base.update(overrides)
    return Settings(**base)


def _chat_response(content):
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 1000},
    }


def _client(settings, response=None, stream_chunks=None):
    app = create_app(settings)
    app.state.runtime_client = FakeRuntimeClient(response=response, stream_chunks=stream_chunks)
    return TestClient(app), app


# --- output guardrail: redact / block / flag / disabled ---------------------------------


def test_output_guardrail_redacts_leaked_secret():
    client, _ = _client(
        _settings(output_guardrail_enabled=True, output_guardrail_mode="redact"),
        response=_chat_response(f"your token is {GITHUB_TOKEN} keep it safe"),
    )
    resp = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    content = resp.json()["choices"][0]["message"]["content"]
    assert GITHUB_TOKEN not in content
    assert "[REDACTED:github_token]" in content
    assert resp.headers["X-Output-Guardrail"] == "redacted"


def test_output_guardrail_blocks_response_with_pii():
    client, _ = _client(
        _settings(output_guardrail_enabled=True, output_guardrail_mode="block"),
        response=_chat_response(f"the ssn on file is {US_SSN}"),
    )
    resp = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    choice = resp.json()["choices"][0]
    assert choice["message"]["content"] == "[response withheld by output policy]"
    assert choice["finish_reason"] == "content_filter"
    assert resp.headers["X-Output-Guardrail"] == "blocked"


def test_output_guardrail_flag_mode_leaves_content_but_records():
    client, _ = _client(
        _settings(output_guardrail_enabled=True, output_guardrail_mode="flag"),
        response=_chat_response(f"token {GITHUB_TOKEN}"),
    )
    resp = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    assert GITHUB_TOKEN in resp.json()["choices"][0]["message"]["content"]
    assert resp.headers["X-Output-Guardrail"] == "flagged"


def test_output_guardrail_disabled_passes_content_through():
    client, _ = _client(
        _settings(output_guardrail_enabled=False),
        response=_chat_response(f"token {GITHUB_TOKEN}"),
    )
    resp = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    assert GITHUB_TOKEN in resp.json()["choices"][0]["message"]["content"]
    assert "X-Output-Guardrail" not in resp.headers


def test_output_guardrail_redacts_before_caching():
    # A redacted response must be what is cached, so the secret never persists in the cache.
    client, app = _client(
        _settings(
            output_guardrail_enabled=True,
            output_guardrail_mode="redact",
            response_cache_enabled=True,
        ),
        response=_chat_response(f"token {GITHUB_TOKEN}"),
    )
    body = {"messages": [{"role": "user", "content": "same"}]}
    first = client.post("/v1/chat/completions", json=body)
    second = client.post("/v1/chat/completions", json=body)
    assert first.headers.get("X-Cache") == "MISS"
    assert second.headers.get("X-Cache") == "HIT"
    assert app.state.runtime_client.calls == 1
    assert GITHUB_TOKEN not in second.json()["choices"][0]["message"]["content"]


def test_output_guardrail_flags_streamed_secret():
    chunks = [
        b'data: {"choices":[{"delta":{"content":"' + GITHUB_TOKEN.encode() + b'"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    client, _ = _client(
        _settings(output_guardrail_enabled=True, output_guardrail_mode="redact", allow_streaming=True),
        stream_chunks=chunks,
    )
    resp = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": True},
    )
    assert resp.status_code == 200
    _ = resp.text  # drain the stream so the end-of-stream scan runs
    metrics = client.get("/metrics").text
    assert "inference_gateway_output_guardrail_total" in metrics
    assert "flagged_stream" in metrics


# --- estimated cost metric -------------------------------------------------------------


def test_estimated_cost_metric_emitted_when_priced():
    client, _ = _client(
        _settings(usd_per_1k_tokens=2.0),
        response=_chat_response("hello"),
    )
    resp = client.post(
        "/v1/chat/completions",
        headers={"X-Sandbox-ID": "cost-test"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    metrics = client.get("/metrics").text
    assert "inference_gateway_estimated_cost_usd_total" in metrics
    assert 'sandbox="cost-test"' in metrics


def test_estimated_cost_metric_absent_when_unpriced():
    client, _ = _client(_settings(usd_per_1k_tokens=0.0), response=_chat_response("hello"))
    resp = client.post(
        "/v1/chat/completions",
        headers={"X-Sandbox-ID": "free-test"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    metrics = client.get("/metrics").text
    cost = "inference_gateway_estimated_cost_usd_total"
    cost_lines = [line for line in metrics.splitlines() if line.startswith(cost)]
    assert all('sandbox="free-test"' not in line for line in cost_lines)


# --- output_findings / redact_output_text units ---------------------------------------


def test_output_findings_detects_patterns_and_terms():
    settings = _settings(blocked_content_terms=("projectfalcon",))
    patterns, terms = settings.output_findings(f"{GITHUB_TOKEN} and {US_SSN} for projectfalcon")
    assert "github_token" in patterns
    assert "us_ssn" in patterns
    assert "projectfalcon" in terms


def test_redact_output_text_replaces_matches():
    settings = _settings(blocked_content_terms=("projectfalcon",))
    redacted, matched = settings.redact_output_text(f"{GITHUB_TOKEN} for projectfalcon")
    assert GITHUB_TOKEN not in redacted
    assert "[REDACTED:github_token]" in redacted
    assert "projectfalcon" not in redacted.lower()
    assert "github_token" in matched
    assert "term:projectfalcon" in matched


# --- shared cache backend --------------------------------------------------------------


def test_build_response_cache_defaults_to_memory():
    assert isinstance(build_response_cache(_settings()), ResponseCache)


def test_redis_response_cache_roundtrip():
    fake = FakeRedis()
    cache = RedisResponseCache(_settings(response_cache_backend="redis"), client=fake)
    assert cache.get("missing") is None
    cache.set("k", {"object": "chat.completion", "n": 1})
    assert cache.get("k") == {"object": "chat.completion", "n": 1}
    # key is prefixed and scoped, not stored bare
    assert "missing" not in fake.data


def test_build_response_cache_redis_selects_redis_backend():
    cache = build_response_cache(_settings(response_cache_backend="redis", response_cache_redis_url="redis://x:6379/1"))
    assert isinstance(cache, RedisResponseCache)


# --- settings validation ---------------------------------------------------------------


def test_budget_backend_error_degrades_to_503():
    from app.budget import BudgetBackendError

    client, app = _client(
        _settings(sandbox_budget_enabled=True, sandbox_request_budget=10),
        response=_chat_response("hi"),
    )

    class BrokenTracker:
        backend = "redis"
        settings = None

        def snapshot(self, *args, **kwargs):
            raise BudgetBackendError("down")

        def reserve(self, *args, **kwargs):
            raise BudgetBackendError("down")

    app.state.budget_tracker = BrokenTracker()
    resp = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "budget_backend_unavailable"
    assert resp.headers["Retry-After"] == "5"


@pytest.mark.parametrize(
    "overrides",
    [
        {"output_guardrail_mode": "nope"},
        {"output_guardrail_patterns": ("bogus_pattern",)},
        {"response_cache_backend": "nope"},
        {"response_cache_redis_timeout_seconds": 0},
        {"response_cache_key_prefix": "   "},
    ],
)
def test_invalid_guardrail_and_cache_settings_raise(overrides):
    with pytest.raises(ValueError):
        _settings(**overrides)


def test_output_guardrail_applies_to_batch_items():
    # The guardrail is endpoint-independent: /v1/batch-inference must not be a bypass around
    # the redact policy that /v1/chat/completions enforces (OWASP LLM02:2025/LLM05:2025).
    client, _ = _client(
        _settings(output_guardrail_enabled=True, output_guardrail_mode="redact"),
        response=_chat_response(f"batch leak {GITHUB_TOKEN} end"),
    )

    resp = client.post(
        "/v1/batch-inference",
        json={"requests": [{"messages": [{"role": "user", "content": "hi"}]}]},
    )

    assert resp.status_code == 200
    content = resp.json()["results"][0]["response"]["choices"][0]["message"]["content"]
    assert GITHUB_TOKEN not in content
    assert "[REDACTED:github_token]" in content
    assert resp.headers["X-Output-Guardrail"] == "redacted"


def test_output_guardrail_redacts_generated_tool_arguments():
    response = {
        "id": "chatcmpl-tool",
        "object": "chat.completion",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "store", "arguments": f'{{"token":"{GITHUB_TOKEN}"}}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    client, _ = _client(
        _settings(output_guardrail_enabled=True, output_guardrail_mode="redact"),
        response=response,
    )

    result = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "call it"}]})

    assert result.status_code == 200
    arguments = result.json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
    assert GITHUB_TOKEN not in arguments
    assert "[REDACTED:github_token]" in arguments
    assert result.headers["X-Output-Guardrail"] == "redacted"


def test_output_guardrail_blocks_legacy_function_arguments():
    response = {
        "id": "chatcmpl-function",
        "object": "chat.completion",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "function_call": {"name": "store", "arguments": f'{{"token":"{GITHUB_TOKEN}"}}'},
                },
                "finish_reason": "function_call",
            }
        ],
    }
    client, _ = _client(
        _settings(output_guardrail_enabled=True, output_guardrail_mode="block"),
        response=response,
    )

    result = client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "call it"}]})

    assert result.status_code == 200
    choice = result.json()["choices"][0]
    assert choice["message"]["function_call"]["arguments"] == "[response withheld by output policy]"
    assert choice["finish_reason"] == "content_filter"
