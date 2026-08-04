"""Tests for the agent-action receipt intake (POST /v1/receipts, ADR 0014).

The chain used to carry one action type, ``model_call``, so it proved what an agent asked
a model and nothing about what the agent then did. These tests cover the rest: the closed
action vocabulary, the tenant binding that stops a workspace writing another's history,
the redaction that keeps a credential out of a record nobody can rewrite, and the property
that matters most, which is that a recorded action links into the same chain as the model
calls around it and verifies with the same operator tool.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path

import pytest
from app.audit import advance_chain
from app.main import create_app
from app.receipts import AGENT_ACTION_TYPES
from app.settings import Settings
from fastapi.testclient import TestClient

from tests.gateway_support import FakeRuntimeClient

ROOT = Path(__file__).resolve().parents[3]


def _load_verifier():
    path = ROOT / "scripts" / "audit-verify.py"
    spec = importlib.util.spec_from_file_location("audit_verify_receipts", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_verify_receipts"] = module
    spec.loader.exec_module(module)
    return module


def _settings(**overrides):
    base = {
        "runtime_backend": "ollama",
        "ollama_base_url": "http://ollama:11434",
        "vllm_base_url": "http://vllm:8000",
        "model_id": "default-model",
        "request_timeout_seconds": 5,
        "agent_receipts_enabled": True,
    }
    base.update(overrides)
    return Settings(**base)


def _client(settings=None):
    app = create_app(settings or _settings())
    app.state.runtime_client = FakeRuntimeClient(
        response={
            "id": "chatcmpl-1",
            "model": "default-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    )
    return TestClient(app), app


def _receipts(caplog):
    return [json.loads(record.message) for record in caplog.records if record.name == "ai_platform_ops_lab.audit"]


def _egress_denied(**overrides):
    body = {
        "action_type": "egress_denied",
        "decision": "denied",
        "target": "api.github.com:443",
        "reason": "no catalog entry",
    }
    body.update(overrides)
    return body


def test_a_denied_egress_attempt_becomes_a_chained_receipt(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    client, _ = _client()

    response = client.post("/v1/receipts", headers={"X-Sandbox-ID": "agent-lab"}, json=_egress_denied())

    assert response.status_code == 200
    assert response.json()["recorded"] is True
    receipt = _receipts(caplog)[-1]
    assert receipt["event"] == "agent_action"
    assert receipt["action_type"] == "egress_denied"
    assert receipt["decision"] == "denied"
    assert receipt["target"] == "api.github.com:443"
    assert receipt["sandbox_id"] == "agent-lab"
    assert receipt["record_hash"] == response.json()["record_hash"]


def test_the_receipt_hash_covers_the_recorded_action(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    client, _ = _client()

    client.post("/v1/receipts", json=_egress_denied())

    receipt = _receipts(caplog)[-1]
    payload = {key: value for key, value in receipt.items() if key not in ("prev_hash", "record_hash")}
    assert advance_chain(receipt["prev_hash"], payload)[1] == receipt["record_hash"]


def test_agent_actions_share_one_chain_with_model_calls(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    client, _ = _client()

    client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    client.post("/v1/receipts", json=_egress_denied())
    client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})

    receipts = _receipts(caplog)
    assert [r["event"] for r in receipts] == ["inference_request", "agent_action", "inference_request"]
    # One chain, one sequence: the action is linked between the two model calls.
    assert receipts[1]["prev_hash"] == receipts[0]["record_hash"]
    assert receipts[2]["prev_hash"] == receipts[1]["record_hash"]


def test_the_operator_verifier_accepts_a_mixed_chain(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    client, _ = _client()
    verifier = _load_verifier()

    client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    client.post("/v1/receipts", json=_egress_denied())
    client.post("/v1/receipts", json={"action_type": "tool_exec", "decision": "allowed", "tool": "pytest"})

    lines = [record.message for record in caplog.records if record.name == "ai_platform_ops_lab.audit"]
    chains = verifier.group_into_chains(verifier.deduplicate(verifier.extract_audit_events(lines)))
    result = verifier.verify_chain(chains[0])

    assert result.ok
    assert result.count == 3


def test_deleting_an_action_receipt_breaks_the_chain(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    client, _ = _client()
    verifier = _load_verifier()

    client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    client.post("/v1/receipts", json=_egress_denied())
    client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})

    lines = [record.message for record in caplog.records if record.name == "ai_platform_ops_lab.audit"]
    # Remove the inconvenient record: what the chain exists to make impossible.
    without_action = [line for line in lines if '"agent_action"' not in line]
    chains = verifier.group_into_chains(verifier.deduplicate(verifier.extract_audit_events(without_action)))
    result = verifier.verify_chain(chains[0])

    assert not result.ok
    assert result.reason == "broken_link_or_reordered"


@pytest.mark.parametrize("action_type", sorted(AGENT_ACTION_TYPES))
def test_every_action_type_in_the_vocabulary_is_accepted(action_type):
    client, _ = _client()

    response = client.post("/v1/receipts", json={"action_type": action_type, "decision": "allowed"})

    assert response.status_code == 200


def test_an_action_type_outside_the_vocabulary_is_rejected():
    client, _ = _client()

    response = client.post("/v1/receipts", json={"action_type": "did_something", "decision": "allowed"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_action_type"


def test_an_invalid_decision_is_rejected():
    client, _ = _client()

    response = client.post("/v1/receipts", json={"action_type": "tool_exec", "decision": "maybe"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_decision"


def test_an_unknown_field_is_rejected():
    # extra="forbid": a producer sending a field the taxonomy does not define should learn
    # that now, not have it silently dropped from a permanent record.
    client, _ = _client()

    response = client.post(
        "/v1/receipts",
        json={"action_type": "tool_exec", "decision": "allowed", "stdout": "everything the tool printed"},
    )

    assert response.status_code == 422


def test_an_oversized_field_is_rejected():
    client, _ = _client(_settings(agent_receipt_max_field_chars=32))

    response = client.post(
        "/v1/receipts",
        json={"action_type": "tool_exec", "decision": "allowed", "tool": "x" * 64},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "receipt_field_too_large"


def test_a_malformed_detail_digest_is_rejected():
    client, _ = _client()

    response = client.post(
        "/v1/receipts",
        json={"action_type": "tool_exec", "decision": "allowed", "detail_sha256": "not-a-digest"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_detail_digest"


def test_a_valid_detail_digest_is_recorded(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    client, _ = _client()
    digest = "a" * 64

    client.post("/v1/receipts", json={"action_type": "file_write", "decision": "allowed", "detail_sha256": digest})

    assert _receipts(caplog)[-1]["detail_sha256"] == digest


def test_a_credential_in_a_reported_command_is_redacted_before_chaining(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    client, _ = _client(_settings(output_guardrail_enabled=True))
    token = "ghp_0123456789abcdefghijABCDEFGHIJ012345"

    client.post(
        "/v1/receipts",
        json={"action_type": "tool_exec", "decision": "allowed", "reason": f"ran curl -H 'token: {token}'"},
    )

    receipt = _receipts(caplog)[-1]
    # A receipt is permanent and read by auditors; it is the last place a leaked
    # credential should come to rest.
    assert token not in json.dumps(receipt)
    assert "[REDACTED:github_token]" in receipt["reason"]


def test_control_characters_cannot_reach_the_audit_stream(caplog):
    # The receipt intake is the first endpoint where a caller-supplied string reaches the
    # audit stream, and that stream is machine-parsed evidence. A forged second record must
    # not be constructible from a field value.
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    client, _ = _client()
    forged = '{"event":"agent_action","decision":"allowed"}'

    client.post(
        "/v1/receipts",
        json={"action_type": "tool_exec", "decision": "allowed", "reason": f"real\n{forged}\r\nmore"},
    )

    lines = [record.message for record in caplog.records if record.name == "ai_platform_ops_lab.audit"]
    assert len(lines) == 1
    receipt = json.loads(lines[0])
    assert "\n" not in receipt["reason"]
    assert "\r" not in receipt["reason"]
    assert receipt["reason"].startswith("real ")


@pytest.mark.parametrize("value", ["with space", "with\ttab", "with\nnewline", "nonascii-é"])
def test_a_correlation_id_outside_the_request_id_contract_is_rejected(value):
    client, _ = _client()

    response = client.post(
        "/v1/receipts",
        json={"action_type": "tool_exec", "decision": "allowed", "correlation_request_id": value},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_correlation_request_id"


def test_a_receipt_cannot_be_filed_against_another_sandbox():
    client, _ = _client()

    response = client.post(
        "/v1/receipts",
        headers={"X-Sandbox-ID": "mine"},
        json=_egress_denied(sandbox_id="someone-elses"),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "sandbox_identity_mismatch"


def test_a_matching_sandbox_id_in_the_body_is_accepted():
    client, _ = _client()

    response = client.post(
        "/v1/receipts",
        headers={"X-Sandbox-ID": "mine"},
        json=_egress_denied(sandbox_id="mine"),
    )

    assert response.status_code == 200


def test_the_intake_is_absent_unless_enabled():
    client, _ = _client(_settings(agent_receipts_enabled=False))

    response = client.post("/v1/receipts", json=_egress_denied())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "receipts_not_enabled"


def test_the_intake_requires_authentication():
    settings = _settings(
        api_key_auth_enabled=True,
        api_key_sha256s=("0" * 64,),
    )
    client, _ = _client(settings)

    response = client.post("/v1/receipts", json=_egress_denied())

    assert response.status_code == 401


def test_a_receipt_can_be_correlated_with_the_model_call_that_prompted_it(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    client, _ = _client()

    completion = client.post(
        "/v1/chat/completions",
        headers={"X-Request-ID": "req-abc"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    client.post(
        "/v1/receipts",
        json={"action_type": "tool_exec", "decision": "allowed", "correlation_request_id": "req-abc"},
    )

    assert completion.headers["X-Request-ID"] == "req-abc"
    receipts = _receipts(caplog)
    assert receipts[0]["request_id"] == "req-abc"
    assert receipts[-1]["correlation_request_id"] == "req-abc"


def test_receipt_counters_are_exported():
    client, _ = _client()

    client.post("/v1/receipts", json=_egress_denied())
    metrics = client.get("/metrics").text

    assert "inference_gateway_agent_receipts_total" in metrics
    assert "egress_denied" in metrics
