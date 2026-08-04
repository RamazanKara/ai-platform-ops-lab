"""Tests for audit chain continuity across process restarts.

A chain covers one process lifetime. Without a link between lifetimes, deleting a whole
replica's chain looks exactly like a pod restart, which is the one tampering the hash
chain could not see. These tests cover the head stores, the ``chain_start`` receipt that
names a predecessor, and the end-to-end property: a restarted gateway produces a chain of
chains that ``scripts/audit-verify.py`` accepts, and a truncated predecessor is caught.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path

import pytest
from app.audit import (
    AUDIT_GENESIS,
    ChainHead,
    FileChainStore,
    MemoryChainStore,
    RedisChainStore,
    advance_chain,
    build_chain_store,
    chain_start_event,
)
from app.main import create_app, open_audit_chain
from app.settings import Settings
from fastapi.testclient import TestClient

from tests.gateway_support import FakeRuntimeClient

ROOT = Path(__file__).resolve().parents[3]


def _load_verifier():
    path = ROOT / "scripts" / "audit-verify.py"
    spec = importlib.util.spec_from_file_location("audit_verify_continuity", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_verify_continuity"] = module
    spec.loader.exec_module(module)
    return module


def _settings(**overrides):
    base = {
        "runtime_backend": "ollama",
        "ollama_base_url": "http://ollama:11434",
        "vllm_base_url": "http://vllm:8000",
        "model_id": "default-model",
        "request_timeout_seconds": 5,
    }
    base.update(overrides)
    return Settings(**base)


def _chat_response():
    return {
        "id": "chatcmpl-1",
        "model": "default-model",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
    }


def _audit_lines(caplog):
    return [record.message for record in caplog.records if record.name == "ai_platform_ops_lab.audit"]


# --- head stores -----------------------------------------------------------------------


def test_memory_chain_store_keeps_no_history_across_processes():
    store = MemoryChainStore()
    assert store.load() is None

    store.save(ChainHead(chain_id="a", head="deadbeef", count=3))

    assert store.load() == ChainHead(chain_id="a", head="deadbeef", count=3)
    # A fresh instance stands in for a new process: memory keeps nothing.
    assert MemoryChainStore().load() is None


def test_file_chain_store_round_trips_through_a_new_instance(tmp_path):
    path = tmp_path / "nested" / "head.json"
    FileChainStore(str(path)).save(ChainHead(chain_id="gw:1", head="abc123", count=7))

    assert FileChainStore(str(path)).load() == ChainHead(chain_id="gw:1", head="abc123", count=7)


def test_file_chain_store_writes_atomically(tmp_path):
    path = tmp_path / "head.json"
    store = FileChainStore(str(path))
    store.save(ChainHead(chain_id="gw:1", head="first", count=1))
    store.save(ChainHead(chain_id="gw:1", head="second", count=2))

    assert store.load().head == "second"
    # No temp files left behind to be mistaken for the head.
    assert [child.name for child in tmp_path.iterdir()] == ["head.json"]


@pytest.mark.parametrize(
    "content",
    ["", "not json", "[]", '{"chain_id": "a"}', '{"chain_id": "a", "head": "b", "count": -1}'],
)
def test_file_chain_store_treats_a_damaged_head_as_absent(tmp_path, content):
    path = tmp_path / "head.json"
    path.write_text(content, encoding="utf-8")

    # A damaged head must read as "no predecessor" rather than crashing startup or, worse,
    # producing a chain_start that points at a head nobody can check.
    assert FileChainStore(str(path)).load() is None


def test_file_chain_store_treats_a_missing_file_as_absent(tmp_path):
    assert FileChainStore(str(tmp_path / "absent.json")).load() is None


class FakeRedis:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


def test_redis_chain_store_round_trips():
    settings = _settings(audit_chain_store_backend="redis")
    client = FakeRedis()
    RedisChainStore(settings, client=client).save(ChainHead(chain_id="gw:2", head="feed", count=4))

    assert RedisChainStore(settings, client=client).load() == ChainHead(chain_id="gw:2", head="feed", count=4)


def test_redis_chain_store_treats_absent_and_damaged_values_as_no_predecessor():
    settings = _settings(audit_chain_store_backend="redis")
    client = FakeRedis()
    store = RedisChainStore(settings, client=client)
    assert store.load() is None

    client.values[settings.audit_chain_store_key] = "{not json"
    assert store.load() is None


def test_build_chain_store_selects_the_configured_backend(tmp_path):
    assert build_chain_store(_settings()).backend == "memory"
    assert build_chain_store(_settings(audit_chain_store_backend="file")).backend == "file"


def test_unknown_chain_store_backend_is_rejected_at_startup():
    with pytest.raises(ValueError, match="audit_chain_store_backend"):
        _settings(audit_chain_store_backend="s3")


# --- chain_start receipt ---------------------------------------------------------------


def test_chain_start_event_states_the_absence_of_a_predecessor_explicitly():
    event = chain_start_event("gw:1", None)

    # Present-and-null, not omitted: a reader can tell "no predecessor" from "field lost".
    assert event["previous_chain_id"] is None
    assert event["previous_head"] is None
    assert event["previous_count"] is None


def test_chain_start_event_names_its_predecessor():
    event = chain_start_event("gw:2", ChainHead(chain_id="gw:1", head="abc", count=9))

    assert event["event"] == "chain_start"
    assert event["chain_id"] == "gw:2"
    assert (event["previous_chain_id"], event["previous_head"], event["previous_count"]) == ("gw:1", "abc", 9)


def test_open_audit_chain_emits_a_genesis_rooted_start_record(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    app = create_app(_settings())

    event = open_audit_chain(app)

    assert event["prev_hash"] == AUDIT_GENESIS
    assert app.state.audit_prev_hash == event["record_hash"]
    assert app.state.audit_chain_count == 1
    assert json.loads(_audit_lines(caplog)[-1])["event"] == "chain_start"


def test_open_audit_chain_covers_the_predecessor_claim_with_its_own_hash():
    app = create_app(_settings())
    app.state.chain_store.save(ChainHead(chain_id="previous:1", head="prevhead", count=5))

    event = open_audit_chain(app)

    # Recomputing the hash over the record minus its chain fields must reproduce it, so
    # the predecessor claim cannot be edited without breaking the chain it opens.
    payload = {key: value for key, value in event.items() if key not in ("prev_hash", "record_hash")}
    assert advance_chain(AUDIT_GENESIS, payload)[1] == event["record_hash"]
    assert event["previous_chain_id"] == "previous:1"


def test_open_audit_chain_survives_an_unreadable_head_store(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    app = create_app(_settings())

    class BrokenStore:
        def load(self):
            raise OSError("head store unreachable")

        def save(self, head):
            raise OSError("head store unreachable")

    app.state.chain_store = BrokenStore()

    event = open_audit_chain(app)

    # Serving must not depend on the head store; the chain simply starts unlinked.
    assert event["previous_chain_id"] is None
    assert app.state.audit_chain_count == 1


def test_chain_start_is_suppressed_when_auditing_is_disabled(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    app = create_app(_settings(audit_log_enabled=False))

    open_audit_chain(app)

    assert _audit_lines(caplog) == []


# --- end to end ------------------------------------------------------------------------


def _run_gateway_lifetime(settings, caplog, requests=2):
    """Start a gateway through its lifespan, serve requests, and return its audit lines."""
    app = create_app(settings)
    app.state.runtime_client = FakeRuntimeClient(response=_chat_response())
    start = len(_audit_lines(caplog))
    with TestClient(app) as client:
        for _ in range(requests):
            client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
    return _audit_lines(caplog)[start:]


def test_a_restarted_gateway_links_its_chain_to_the_previous_one(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    settings = _settings(
        audit_chain_store_backend="file",
        audit_chain_store_path=str(tmp_path / "head.json"),
    )

    first = _run_gateway_lifetime(settings, caplog)
    second = _run_gateway_lifetime(settings, caplog)

    first_start = json.loads(first[0])
    second_start = json.loads(second[0])
    assert first_start["previous_chain_id"] is None
    assert second_start["previous_chain_id"] == first_start["chain_id"]
    # The successor names a head the predecessor actually reached.
    first_hashes = [json.loads(line)["record_hash"] for line in first]
    assert second_start["previous_head"] in first_hashes


def test_the_chain_of_chains_verifies_end_to_end(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    settings = _settings(
        audit_chain_store_backend="file",
        audit_chain_store_path=str(tmp_path / "head.json"),
    )
    verifier = _load_verifier()

    lines = _run_gateway_lifetime(settings, caplog) + _run_gateway_lifetime(settings, caplog)

    chains = verifier.group_into_chains(verifier.deduplicate(verifier.extract_audit_events(lines)))
    assert len(chains) == 2
    assert all(verifier.verify_chain(chain).ok for chain in chains)
    problems, notes = verifier.verify_continuity(chains)
    assert problems == []
    assert notes == []


def test_deleting_a_predecessors_records_is_detected(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    settings = _settings(
        audit_chain_store_backend="file",
        audit_chain_store_path=str(tmp_path / "head.json"),
    )
    verifier = _load_verifier()

    first = _run_gateway_lifetime(settings, caplog)
    second = _run_gateway_lifetime(settings, caplog)

    # Drop the predecessor's trailing receipts, the move a hash chain alone cannot see.
    truncated = first[:1] + second
    chains = verifier.group_into_chains(verifier.deduplicate(verifier.extract_audit_events(truncated)))
    problems, _ = verifier.verify_continuity(chains)

    assert problems
    assert "truncated" in problems[0]


def test_a_missing_predecessor_is_a_note_by_default_and_a_failure_under_strict(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    settings = _settings(
        audit_chain_store_backend="file",
        audit_chain_store_path=str(tmp_path / "head.json"),
    )
    verifier = _load_verifier()

    _run_gateway_lifetime(settings, caplog)
    second = _run_gateway_lifetime(settings, caplog)

    # Rotated logs legitimately begin mid-history, so this must not fail by default.
    chains = verifier.group_into_chains(verifier.deduplicate(verifier.extract_audit_events(second)))
    problems, notes = verifier.verify_continuity(chains)

    assert problems == []
    assert notes and "not present" in notes[0]


def test_memory_backend_keeps_the_pre_continuity_behavior(caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    settings = _settings()

    first = _run_gateway_lifetime(settings, caplog)
    second = _run_gateway_lifetime(settings, caplog)

    # The default backend stores nothing across processes, so each chain starts unlinked
    # and says so rather than claiming a predecessor it cannot support.
    assert json.loads(first[0])["previous_chain_id"] is None
    assert json.loads(second[0])["previous_chain_id"] is None


def test_the_head_is_persisted_on_shutdown(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="ai_platform_ops_lab.audit")
    path = tmp_path / "head.json"
    settings = _settings(audit_chain_store_backend="file", audit_chain_store_path=str(path))

    lines = _run_gateway_lifetime(settings, caplog, requests=3)

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["count"] == len(lines)
    assert stored["head"] == json.loads(lines[-1])["record_hash"]
