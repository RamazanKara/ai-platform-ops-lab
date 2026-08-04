"""Tests for tamper-evident retrieval receipts on the RAG service.

Retrieval decides what tenant data an agent was shown and is the main path by which
untrusted content reaches a model, so its receipts carry the same hash chain as the
gateway's model calls and verify with the same operator tool. These tests cover the
chained receipt shape, continuity across a restart, and the end-to-end property that
``scripts/audit-verify.py`` accepts a RAG chain and rejects a tampered one.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path

from app.audit import AUDIT_GENESIS, ChainHead, advance_chain
from app.main import create_app, open_audit_chain
from app.settings import Settings
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[3]


def _load_verifier():
    path = ROOT / "scripts" / "audit-verify.py"
    spec = importlib.util.spec_from_file_location("audit_verify_rag", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_verify_rag"] = module
    spec.loader.exec_module(module)
    return module


def _write_doc(tmp_path, name="agents.md", content="# Coding Agents\nUse the gateway."):
    (tmp_path / name).write_text(content, encoding="utf-8")
    return tmp_path


def _audit_lines(caplog):
    return [record.message for record in caplog.records if record.name == "ai_platform_ops_lab.rag.audit"]


def _query(client, text="agents"):
    return client.post("/v1/rag/query", json={"query": text})


def test_retrieval_receipt_is_hash_chained(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.rag.audit")
    client = TestClient(create_app(Settings(document_dir=_write_doc(tmp_path))))

    _query(client)

    receipt = json.loads(_audit_lines(caplog)[-1])
    assert receipt["event"] == "rag_query"
    assert receipt["action_type"] == "retrieval"
    assert receipt["decision"] == "allowed"
    assert receipt["prev_hash"] == AUDIT_GENESIS
    assert receipt["record_hash"]
    # The hash covers the record minus its own chain fields.
    payload = {key: value for key, value in receipt.items() if key not in ("prev_hash", "record_hash")}
    assert advance_chain(AUDIT_GENESIS, payload)[1] == receipt["record_hash"]


def test_receipts_link_to_each_other_in_order(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.rag.audit")
    client = TestClient(create_app(Settings(document_dir=_write_doc(tmp_path))))

    _query(client, "agents")
    _query(client, "gateway")

    receipts = [json.loads(line) for line in _audit_lines(caplog)]
    assert receipts[1]["prev_hash"] == receipts[0]["record_hash"]


def test_a_denied_retrieval_is_recorded_as_a_denial(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.rag.audit")
    settings = Settings(document_dir=_write_doc(tmp_path), max_query_chars=8)
    client = TestClient(create_app(settings))

    response = _query(client, "a query well past the configured ceiling")

    assert response.status_code == 400
    receipt = json.loads(_audit_lines(caplog)[-1])
    assert receipt["decision"] == "denied"
    assert receipt["record_hash"]


def test_the_receipt_never_carries_the_raw_query(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.rag.audit")
    client = TestClient(create_app(Settings(document_dir=_write_doc(tmp_path))))

    _query(client, "commercially sensitive phrase")

    line = _audit_lines(caplog)[-1]
    assert "commercially sensitive phrase" not in line
    receipt = json.loads(line)
    assert receipt["query_chars"] == len("commercially sensitive phrase")
    assert receipt["query_sha256"]


def test_the_receipt_carries_correlation_ids_for_cross_service_tracing(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.rag.audit")
    client = TestClient(create_app(Settings(document_dir=_write_doc(tmp_path))))
    traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

    client.post(
        "/v1/rag/query",
        json={"query": "agents"},
        headers={"X-Request-ID": "req-correlate", "traceparent": traceparent},
    )

    receipt = json.loads(_audit_lines(caplog)[-1])
    # These are what let an auditor tie a retrieval to the completion it fed, across two
    # independently verifiable chains.
    assert receipt["request_id"] == "req-correlate"
    assert receipt["traceparent"] == traceparent


def test_auditing_disabled_emits_no_receipts(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.rag.audit")
    settings = Settings(document_dir=_write_doc(tmp_path), audit_log_enabled=False)
    client = TestClient(create_app(settings))

    _query(client)

    assert _audit_lines(caplog) == []


def _run_lifetime(settings, caplog, queries=2):
    app = create_app(settings)
    start = len(_audit_lines(caplog))
    with TestClient(app) as client:
        for _ in range(queries):
            _query(client)
    return _audit_lines(caplog)[start:]


def test_a_restarted_service_links_its_chain_to_the_previous_one(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.rag.audit")
    settings = Settings(
        document_dir=_write_doc(tmp_path),
        audit_chain_store_backend="file",
        audit_chain_store_path=str(tmp_path / "head.json"),
    )

    first = _run_lifetime(settings, caplog)
    second = _run_lifetime(settings, caplog)

    first_start = json.loads(first[0])
    second_start = json.loads(second[0])
    assert first_start["event"] == "chain_start"
    assert second_start["previous_chain_id"] == first_start["chain_id"]
    assert second_start["previous_head"] in [json.loads(line)["record_hash"] for line in first]


def test_the_operator_verifier_accepts_a_rag_chain(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.rag.audit")
    settings = Settings(
        document_dir=_write_doc(tmp_path),
        audit_chain_store_backend="file",
        audit_chain_store_path=str(tmp_path / "head.json"),
    )
    verifier = _load_verifier()

    lines = _run_lifetime(settings, caplog) + _run_lifetime(settings, caplog)

    chains = verifier.group_into_chains(verifier.deduplicate(verifier.extract_audit_events(lines)))
    assert len(chains) == 2
    assert all(verifier.verify_chain(chain).ok for chain in chains)
    assert verifier.verify_continuity(chains) == ([], [])


def test_the_operator_verifier_catches_a_tampered_retrieval_receipt(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.rag.audit")
    client = TestClient(create_app(Settings(document_dir=_write_doc(tmp_path))))
    verifier = _load_verifier()

    _query(client, "agents")
    _query(client, "gateway")
    lines = _audit_lines(caplog)

    # Rewrite which documents a retrieval returned, leaving its stored hash intact.
    tampered = json.loads(lines[0])
    tampered["result_ids"] = ["some-other-document"]
    rewritten = [json.dumps(tampered, sort_keys=True), *lines[1:]]

    chains = verifier.group_into_chains(verifier.deduplicate(verifier.extract_audit_events(rewritten)))
    result = verifier.verify_chain(chains[0])

    assert not result.ok
    assert result.reason == "record_hash_mismatch"


def test_open_audit_chain_survives_an_unreadable_head_store(tmp_path):
    app = create_app(Settings(document_dir=_write_doc(tmp_path)))

    class BrokenStore:
        def load(self):
            raise OSError("head store unreachable")

        def save(self, head):
            raise OSError("head store unreachable")

    app.state.chain_store = BrokenStore()

    event = open_audit_chain(app)

    assert event["previous_chain_id"] is None
    assert app.state.audit_chain_count == 1


def test_the_head_is_persisted_on_shutdown(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.rag.audit")
    path = tmp_path / "head.json"
    settings = Settings(
        document_dir=_write_doc(tmp_path),
        audit_chain_store_backend="file",
        audit_chain_store_path=str(path),
    )

    lines = _run_lifetime(settings, caplog, queries=3)

    stored = ChainHead(**json.loads(path.read_text(encoding="utf-8")))
    assert stored.count == len(lines)
    assert stored.head == json.loads(lines[-1])["record_hash"]
