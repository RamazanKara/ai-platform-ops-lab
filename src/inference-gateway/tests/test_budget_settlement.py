"""Tests for reconciling a budget reservation against measured token usage.

Admission charges the worst case a request could cost before the model runs. Settlement
swaps that estimate for what the runtime actually reported, so the window counter and the
``x-ratelimit-*`` headroom track real spend. These tests cover the arithmetic, both
tracker backends, and the endpoint behavior that has to hold around it: settlement runs
once per request on the streaming and non-streaming paths alike, it lands on the audit
receipt, and it can never fail a request that already succeeded or hand out free budget.
"""

import json
import logging

import pytest
from app.budget import (
    BudgetReservation,
    BudgetUsage,
    InMemorySandboxBudgetTracker,
    RedisSandboxBudgetTracker,
    actual_total_tokens,
    settlement_adjustment,
)
from app.main import create_app
from app.settings import Settings
from fastapi.testclient import TestClient

from tests.gateway_support import FakeRedisBudgetStore, FakeRuntimeClient


def _budget_settings(**overrides):
    base = {
        "runtime_backend": "ollama",
        "ollama_base_url": "http://ollama:11434",
        "vllm_base_url": "http://vllm:8000",
        "model_id": "default-model",
        "request_timeout_seconds": 5,
        "sandbox_budget_enabled": True,
        "sandbox_estimated_token_budget": 10000,
        "max_completion_tokens": 4096,
    }
    base.update(overrides)
    return Settings(**base)


def _chat_response(usage=None, content="hello"):
    body = {
        "id": "chatcmpl-1",
        "model": "default-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def _receipts(caplog):
    return [json.loads(record.message) for record in caplog.records if record.name == "ai_platform_ops_lab.audit"]


# --- arithmetic ------------------------------------------------------------------------


def test_actual_total_tokens_prefers_the_reported_total():
    assert actual_total_tokens({"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 9}) == 9


def test_actual_total_tokens_falls_back_to_the_prompt_completion_pair():
    assert actual_total_tokens({"prompt_tokens": 5, "completion_tokens": 3}) == 8
    assert actual_total_tokens({"completion_tokens": 3}) == 3


def test_actual_total_tokens_returns_none_when_the_runtime_reported_nothing():
    assert actual_total_tokens(None) is None
    assert actual_total_tokens({}) is None
    assert actual_total_tokens({"prompt_tokens": None}) is None
    assert actual_total_tokens({"prompt_tokens": True}) is None
    assert actual_total_tokens({"prompt_tokens": -4}) is None
    assert actual_total_tokens("not a mapping") is None


def test_settlement_adjustment_refunds_the_unused_reservation():
    assert settlement_adjustment(reserved=1000, actual=120, current=1000) == (880, 0)


def test_settlement_adjustment_charges_an_overrun():
    assert settlement_adjustment(reserved=100, actual=180, current=100) == (0, 80)


def test_settlement_adjustment_never_refunds_more_than_the_counter_holds():
    # The window rolled over between reserve and settle: the refund belongs to a counter
    # that no longer exists, so it is bounded by what is actually there.
    assert settlement_adjustment(reserved=1000, actual=100, current=30) == (30, 0)
    assert settlement_adjustment(reserved=1000, actual=100, current=0) == (0, 0)


# --- in-memory tracker -----------------------------------------------------------------


def _reserve(tracker, settings, sandbox="sandbox-a", max_tokens=1000, content="hi"):
    payload = {"messages": [{"role": "user", "content": content}], "max_tokens": max_tokens}
    return tracker.reserve(sandbox, payload, settings)


def test_in_memory_settle_returns_the_unused_reservation_to_the_window():
    settings = _budget_settings()
    tracker = InMemorySandboxBudgetTracker(settings)
    reservation = _reserve(tracker, settings)
    reserved = reservation.estimated_tokens

    settlement = tracker.settle(reservation, {"prompt_tokens": 4, "completion_tokens": 11, "total_tokens": 15})

    assert settlement.reserved_estimated_tokens == reserved
    assert settlement.actual_tokens == 15
    assert settlement.refunded_tokens == reserved - 15
    assert settlement.overrun_tokens == 0
    assert settlement.settled_estimated_tokens == 15
    assert tracker.snapshot("sandbox-a", settings)["usage"]["estimated_tokens"] == 15


def test_in_memory_settle_charges_an_overrun_beyond_the_reservation():
    settings = _budget_settings()
    tracker = InMemorySandboxBudgetTracker(settings)
    reservation = _reserve(tracker, settings, max_tokens=10)
    reserved = reservation.estimated_tokens

    settlement = tracker.settle(reservation, {"total_tokens": reserved + 25})

    assert settlement.refunded_tokens == 0
    assert settlement.overrun_tokens == 25
    assert tracker.snapshot("sandbox-a", settings)["usage"]["estimated_tokens"] == reserved + 25


def test_in_memory_settle_leaves_the_reservation_when_usage_is_unreported():
    settings = _budget_settings()
    tracker = InMemorySandboxBudgetTracker(settings)
    reservation = _reserve(tracker, settings)
    reserved = reservation.estimated_tokens

    assert tracker.settle(reservation, None) is None
    assert tracker.snapshot("sandbox-a", settings)["usage"]["estimated_tokens"] == reserved


def test_in_memory_settle_does_not_resurrect_a_rolled_over_window():
    settings = _budget_settings()
    tracker = InMemorySandboxBudgetTracker(settings)
    reservation = BudgetReservation(
        sandbox_id="never-reserved",
        request_count=1,
        prompt_chars=10,
        estimated_tokens=500,
        usage=BudgetUsage(),
        backend="memory",
    )

    assert tracker.settle(reservation, {"total_tokens": 5}) is None
    assert tracker.snapshot("never-reserved", settings)["usage"]["estimated_tokens"] == 0


def test_in_memory_settle_leaves_other_budget_dimensions_untouched():
    # Only the token estimate is a guess. The request count is exactly one and the prompt
    # characters were counted from the bytes actually sent, so neither is settled.
    settings = _budget_settings()
    tracker = InMemorySandboxBudgetTracker(settings)
    reservation = _reserve(tracker, settings, content="a longer prompt body")
    before = tracker.snapshot("sandbox-a", settings)["usage"]

    tracker.settle(reservation, {"total_tokens": 3})
    after = tracker.snapshot("sandbox-a", settings)["usage"]

    assert after["requests"] == before["requests"]
    assert after["prompt_chars"] == before["prompt_chars"]
    assert after["estimated_tokens"] == 3


def test_in_memory_settlement_frees_headroom_for_later_requests():
    # The point of settling: a caller that reserves the cap but generates almost nothing
    # must not be locked out of its own window.
    settings = _budget_settings(sandbox_estimated_token_budget=2500)
    tracker = InMemorySandboxBudgetTracker(settings)
    for _ in range(3):
        reservation = _reserve(tracker, settings, max_tokens=1000)
        tracker.settle(reservation, {"total_tokens": 20})

    assert tracker.snapshot("sandbox-a", settings)["usage"]["estimated_tokens"] == 60


# --- Redis tracker ---------------------------------------------------------------------


def test_redis_settle_matches_the_in_memory_arithmetic():
    settings = _budget_settings(sandbox_budget_backend="redis")
    store = FakeRedisBudgetStore()
    tracker = RedisSandboxBudgetTracker(settings, client=store)
    reservation = _reserve(tracker, settings)
    reserved = reservation.estimated_tokens

    settlement = tracker.settle(reservation, {"total_tokens": 12})

    assert settlement.refunded_tokens == reserved - 12
    assert settlement.settled_estimated_tokens == 12
    assert tracker.snapshot("sandbox-a", settings)["usage"]["estimated_tokens"] == 12


def test_redis_settle_skips_an_expired_window_without_recreating_the_key():
    settings = _budget_settings(sandbox_budget_backend="redis")
    store = FakeRedisBudgetStore()
    tracker = RedisSandboxBudgetTracker(settings, client=store)
    reservation = _reserve(tracker, settings)
    store.data.clear()  # the window expired between reserve and settle

    assert tracker.settle(reservation, {"total_tokens": 12}) is None
    assert store.data == {}


# --- endpoint behavior -----------------------------------------------------------------


def _client(settings, response=None, stream_chunks=None):
    app = create_app(settings)
    fake = FakeRuntimeClient(response=response)
    if stream_chunks is not None:
        fake.stream_chunks = stream_chunks
    app.state.runtime_client = fake
    return TestClient(app), app, fake


def test_chat_completion_settles_the_reservation_and_records_it_on_the_receipt(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    settings = _budget_settings()
    client, app, _ = _client(
        settings,
        response=_chat_response(usage={"prompt_tokens": 6, "completion_tokens": 9, "total_tokens": 15}),
    )

    response = client.post(
        "/v1/chat/completions",
        headers={"X-Sandbox-ID": "settle-me"},
        json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 2000},
    )

    assert response.status_code == 200
    receipt = _receipts(caplog)[-1]
    settlement = receipt["budget_settlement"]
    assert settlement["actual_tokens"] == 15
    assert settlement["reserved_estimated_tokens"] > 2000
    assert settlement["refunded_tokens"] == settlement["reserved_estimated_tokens"] - 15
    assert settlement["settled_estimated_tokens"] == 15
    assert app.state.budget_tracker.snapshot("settle-me", settings)["usage"]["estimated_tokens"] == 15


def test_settled_budget_is_reflected_in_the_next_requests_headroom_headers():
    settings = _budget_settings(sandbox_estimated_token_budget=5000)
    client, _, _ = _client(
        settings,
        response=_chat_response(usage={"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}),
    )
    body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 2000}

    first = client.post("/v1/chat/completions", headers={"X-Sandbox-ID": "headroom"}, json=body)
    second = client.post("/v1/chat/completions", headers={"X-Sandbox-ID": "headroom"}, json=body)

    # Without settlement the second request would see roughly 2000 tokens already spent.
    assert int(first.headers["x-ratelimit-remaining-tokens"]) < 3100
    assert int(second.headers["x-ratelimit-remaining-tokens"]) > 2900


def test_repeated_capped_requests_are_not_locked_out_by_their_own_reservations():
    # Four requests each reserving the 1000-token cap would exceed a 2500-token window on
    # the third; settling each to its real 10-token cost keeps the caller inside it.
    settings = _budget_settings(sandbox_estimated_token_budget=2500, max_completion_tokens=1000)
    client, _, _ = _client(
        settings,
        response=_chat_response(usage={"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}),
    )
    body = {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 1000}

    statuses = [
        client.post("/v1/chat/completions", headers={"X-Sandbox-ID": "repeat"}, json=body).status_code for _ in range(4)
    ]

    assert statuses == [200, 200, 200, 200]


def test_reservation_stands_when_the_runtime_reports_no_usage(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    settings = _budget_settings()
    client, app, _ = _client(settings, response=_chat_response(usage=None))

    client.post(
        "/v1/chat/completions",
        headers={"X-Sandbox-ID": "no-usage"},
        json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 500},
    )

    receipt = _receipts(caplog)[-1]
    assert receipt["budget_settlement"] is None
    assert app.state.budget_tracker.snapshot("no-usage", settings)["usage"]["estimated_tokens"] > 500


def test_streaming_settles_once_at_end_of_stream(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    settings = _budget_settings(allow_streaming=True)
    client, app, _ = _client(
        settings,
        stream_chunks=[
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":8,"total_tokens":12}}\n\n',
            b"data: [DONE]\n\n",
        ],
    )

    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"X-Sandbox-ID": "stream-settle"},
        json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 900, "stream": True},
    ) as response:
        response.read()

    receipt = _receipts(caplog)[-1]
    assert receipt["budget_settlement"]["actual_tokens"] == 12
    assert app.state.budget_tracker.snapshot("stream-settle", settings)["usage"]["estimated_tokens"] == 12


def test_anthropic_streaming_settles_the_reservation(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    settings = _budget_settings(allow_streaming=True)
    client, app, _ = _client(
        settings,
        stream_chunks=[
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":4,"total_tokens":7}}\n\n',
        ],
    )

    with client.stream(
        "POST",
        "/v1/messages",
        headers={"X-Sandbox-ID": "anthropic-settle"},
        json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 900, "stream": True},
    ) as response:
        response.read()

    receipt = _receipts(caplog)[-1]
    assert receipt["budget_settlement"]["actual_tokens"] == 7
    assert app.state.budget_tracker.snapshot("anthropic-settle", settings)["usage"]["estimated_tokens"] == 7


def test_settlement_failure_does_not_fail_a_request_that_already_succeeded(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    settings = _budget_settings()
    client, app, _ = _client(
        settings,
        response=_chat_response(usage={"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}),
    )
    tracker = app.state.budget_tracker

    def _broken_settle(reservation, usage):
        raise OSError("budget backend down")

    tracker.settle = _broken_settle

    response = client.post(
        "/v1/chat/completions",
        headers={"X-Sandbox-ID": "broken-settle"},
        json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 500},
    )

    # The response is committed; a settlement failure leaves the conservative reservation
    # in place rather than turning a served call into an error.
    assert response.status_code == 200
    assert _receipts(caplog)[-1]["budget_settlement"] is None


def test_cache_hit_settles_nothing_because_it_reserved_nothing(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    settings = _budget_settings(response_cache_enabled=True)
    client, app, fake = _client(
        settings,
        response=_chat_response(usage={"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}),
    )
    body = {"messages": [{"role": "user", "content": "cache me"}], "max_tokens": 400}

    client.post("/v1/chat/completions", headers={"X-Sandbox-ID": "cached"}, json=body)
    after_first = app.state.budget_tracker.snapshot("cached", settings)["usage"]["estimated_tokens"]
    second = client.post("/v1/chat/completions", headers={"X-Sandbox-ID": "cached"}, json=body)

    assert second.headers.get("X-Cache") == "HIT"
    assert fake.calls == 1
    assert _receipts(caplog)[-1]["budget_settlement"] is None
    assert app.state.budget_tracker.snapshot("cached", settings)["usage"]["estimated_tokens"] == after_first


def test_usage_endpoint_reports_settled_tokens_and_cost():
    # /v1/usage is the chargeback data layer. Before settlement it reported the reserved
    # ceiling, so the cost it quoted was the caller's max_tokens, not their spend.
    settings = _budget_settings(usd_per_1k_tokens=2.0)
    client, _, _ = _client(
        settings,
        response=_chat_response(usage={"prompt_tokens": 40, "completion_tokens": 60, "total_tokens": 100}),
    )

    client.post(
        "/v1/chat/completions",
        headers={"X-Sandbox-ID": "billing"},
        json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 3000},
    )
    usage = client.get("/v1/usage", headers={"X-Sandbox-ID": "billing"}).json()

    assert usage["usage"]["estimated_tokens"] == 100
    assert usage["estimated_cost"] == 0.2


def test_settlement_counters_are_exported():
    settings = _budget_settings()
    client, _, _ = _client(
        settings,
        response=_chat_response(usage={"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}),
    )

    client.post(
        "/v1/chat/completions",
        headers={"X-Sandbox-ID": "metric"},
        json={"messages": [{"role": "user", "content": "hi"}], "max_tokens": 700},
    )
    metrics = client.get("/metrics").text

    assert "inference_gateway_budget_settlements_total" in metrics
    assert "inference_gateway_budget_settled_tokens_total" in metrics
    assert "refund" in metrics


# --- per-model token calibration --------------------------------------------------------


def _routing_policy(tmp_path, chars_per_token):
    path = tmp_path / "routing.yaml"
    path.write_text(
        "apiVersion: platform.ai/v1alpha1\n"
        "kind: ModelRoutingPolicy\n"
        "metadata:\n"
        "  name: test\n"
        "spec:\n"
        "  models:\n"
        "    - id: default-model\n"
        "      backend: ollama\n"
        f"      estimatedCharsPerToken: {chars_per_token}\n",
        encoding="utf-8",
    )
    return path


def test_a_models_calibrated_divisor_is_used_for_its_budget_estimate(tmp_path, caplog):
    # 400 prompt characters at 4 chars/token is 100 tokens; at 2 it is 200. The reserved
    # estimate must follow the model's own calibration, not the global default.
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    settings = _budget_settings(
        model_routing_policy_path=_routing_policy(tmp_path, 2),
        budget_estimated_chars_per_token=4,
    )
    client, _, _ = _client(settings, response=_chat_response(usage=None))

    client.post(
        "/v1/chat/completions",
        headers={"X-Sandbox-ID": "calibrated"},
        json={"messages": [{"role": "user", "content": "x" * 400}], "max_tokens": 10},
    )

    reserved = _receipts(caplog)[-1]["budget"]["estimated_tokens"]
    assert reserved == 200 + 10


def test_the_gateway_default_applies_when_a_model_declares_no_calibration(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    settings = _budget_settings(
        model_routing_policy_path=_routing_policy(tmp_path, 0),
        budget_estimated_chars_per_token=4,
    )
    client, _, _ = _client(settings, response=_chat_response(usage=None))

    client.post(
        "/v1/chat/completions",
        headers={"X-Sandbox-ID": "uncalibrated"},
        json={"messages": [{"role": "user", "content": "x" * 400}], "max_tokens": 10},
    )

    reserved = _receipts(caplog)[-1]["budget"]["estimated_tokens"]
    assert reserved == 100 + 10


def test_a_negative_calibration_is_rejected_when_the_policy_loads(tmp_path):
    path = _routing_policy(tmp_path, -1)

    with pytest.raises(ValueError, match="estimatedCharsPerToken"):
        create_app(_budget_settings(model_routing_policy_path=path))
