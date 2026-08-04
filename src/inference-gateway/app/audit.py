"""Redacted request fingerprints and tamper-evident audit-chain primitives.

A chain covers one process lifetime: every receipt is hash-linked to its predecessor, so
an edit, insertion, deletion, or reordering *within* that lifetime is detectable. On its
own that leaves a gap at the seam, because a restarting process began a fresh chain at
genesis with nothing tying it to the one before, and a whole chain that disappeared was
indistinguishable from a pod that simply restarted.

:class:`ChainStore` closes the seam. The head is persisted out of process, and the first
record of each new chain is a ``chain_start`` receipt naming its predecessor and that
predecessor's head. The chains then form their own chain, so a missing lifetime is as
visible as a missing record, and ``scripts/audit-verify.py`` checks that linkage.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from fastapi import Request

from app.settings import Settings, message_prompt_chars

AUDIT_GENESIS = hashlib.sha256(b"genesis").hexdigest()


def chain_audit_event(request: Request, event: dict[str, Any]) -> None:
    """Hash-link an audit event into the current gateway process chain."""
    state = request.app.state
    previous = getattr(state, "audit_prev_hash", AUDIT_GENESIS)
    event["prev_hash"], event["record_hash"] = advance_chain(previous, event)
    state.audit_prev_hash = event["record_hash"]
    state.audit_chain_count = getattr(state, "audit_chain_count", 0) + 1


def advance_chain(previous: str, event: dict[str, Any]) -> tuple[str, str]:
    """Return ``(prev_hash, record_hash)`` for an event appended after ``previous``.

    ``record_hash = SHA-256(prev_hash || canonical(event))`` over the event *before* the
    chain fields are stamped on, where canonical is compact key-sorted JSON. Kept as a
    standalone function so the startup path can chain a ``chain_start`` record without a
    request, and so tests can build a chain the verifier accepts.
    """
    canonical = json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return previous, hashlib.sha256(previous.encode("ascii") + canonical).hexdigest()


@dataclass(frozen=True)
class ChainHead:
    """The persisted tip of one process's audit chain."""

    chain_id: str
    head: str
    count: int


class ChainStore(Protocol):
    """Durable store for the audit chain head, read at startup and written as it advances."""

    def load(self) -> ChainHead | None: ...

    def save(self, head: ChainHead) -> None: ...


class MemoryChainStore:
    """Process-local head store: keeps the API uniform but provides no continuity.

    The default, because continuity needs storage that outlives the pod and the kit does
    not get to assume the operator has provisioned any. A gateway running on this backend
    emits ``chain_start`` records with no predecessor, which is honest: there is nothing
    to link to.
    """

    backend = "memory"

    def __init__(self) -> None:
        self._head: ChainHead | None = None

    def load(self) -> ChainHead | None:
        return self._head

    def save(self, head: ChainHead) -> None:
        self._head = head


class FileChainStore:
    """Head store backed by a JSON file, for a gateway with a mounted volume.

    Writes are atomic (temp file plus rename) so a crash mid-write leaves the previous
    head intact rather than a truncated file that would read as a missing predecessor.
    """

    backend = "file"

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def load(self) -> ChainHead | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return _head_from_mapping(data)

    def save(self, head: ChainHead) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(asdict(head), stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
        except OSError:
            Path(temp_name).unlink(missing_ok=True)
            raise


class RedisChainStore:
    """Head store backed by Redis, for a gateway that already depends on it."""

    backend = "redis"

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.key = settings.audit_chain_store_key
        if client is None:
            try:
                import redis
            except ImportError as exc:  # pragma: no cover - redis ships in the gateway image
                raise RuntimeError("redis package is required when AUDIT_CHAIN_STORE_BACKEND=redis") from exc
            client = redis.Redis.from_url(
                settings.audit_chain_store_redis_url,
                decode_responses=True,
                socket_timeout=settings.audit_chain_store_timeout_seconds,
                socket_connect_timeout=settings.audit_chain_store_timeout_seconds,
            )
        self.client = client

    def load(self) -> ChainHead | None:
        raw = self.client.get(self.key)
        if not raw:
            return None
        try:
            return _head_from_mapping(json.loads(raw))
        except ValueError:
            return None

    def save(self, head: ChainHead) -> None:
        self.client.set(self.key, json.dumps(asdict(head), sort_keys=True))


def _head_from_mapping(data: Any) -> ChainHead | None:
    """Rebuild a ChainHead from stored JSON, returning None for anything malformed."""
    if not isinstance(data, dict):
        return None
    chain_id = data.get("chain_id")
    head = data.get("head")
    count = data.get("count")
    if not isinstance(chain_id, str) or not chain_id or not isinstance(head, str) or not head:
        return None
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    return ChainHead(chain_id=chain_id, head=head, count=count)


def build_chain_store(settings: Settings) -> ChainStore:
    """Return the configured audit-chain head store."""
    if settings.audit_chain_store_backend == "file":
        return FileChainStore(settings.audit_chain_store_path)
    if settings.audit_chain_store_backend == "redis":
        return RedisChainStore(settings)
    return MemoryChainStore()


def chain_start_event(chain_id: str, previous: ChainHead | None) -> dict[str, Any]:
    """Build the ``chain_start`` receipt that opens a chain and names its predecessor.

    Emitted as the first record of every chain and hash-linked like any other, so the
    claim about the predecessor is itself covered by the chain it opens. ``previous`` is
    None for a first-ever start or a store that keeps no history, and the record says so
    explicitly rather than omitting the fields.
    """
    return {
        "event": "chain_start",
        "chain_id": chain_id,
        "previous_chain_id": previous.chain_id if previous else None,
        "previous_head": previous.head if previous else None,
        "previous_count": previous.count if previous else None,
    }


def payload_fingerprint(payload: dict[str, Any]) -> dict[str, Any]:
    """Summarize a payload into redacted counts and complete canonical hashes."""
    messages = payload.get("messages") or []
    raw_text_field = payload.get("input")
    if raw_text_field is None:
        raw_text_field = payload.get("prompt")
    if not messages and raw_text_field is not None:
        texts = [str(item) for item in (raw_text_field if isinstance(raw_text_field, list) else [raw_text_field])]
        canonical = json.dumps(texts, sort_keys=True, separators=(",", ":"))
        result = {
            "input_count": len(texts),
            "prompt_chars": sum(len(text) for text in texts),
            "prompt_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        }
        request_canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        result["request_sha256"] = hashlib.sha256(request_canonical.encode("utf-8")).hexdigest()
        return result

    canonical_messages = []
    roles = []
    tool_call_count = 0
    for message in messages:
        role = str(message.get("role", "unknown"))
        roles.append(role)
        canonical_messages.append(message)
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            tool_call_count += len(tool_calls)
    canonical_prompt: dict[str, Any] = {"messages": canonical_messages}
    for field in ("tools", "functions", "tool_choice", "function_call", "response_format"):
        if field in payload:
            canonical_prompt[field] = payload[field]
    canonical = json.dumps(canonical_prompt, sort_keys=True, separators=(",", ":"), default=str)
    request_canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    fingerprint: dict[str, Any] = {
        "message_count": len(messages),
        "message_roles": roles,
        "prompt_chars": message_prompt_chars(messages),
        "prompt_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "request_sha256": hashlib.sha256(request_canonical.encode("utf-8")).hexdigest(),
    }
    if payload.get("tools"):
        fingerprint["tool_count"] = len(payload["tools"])
    if tool_call_count:
        fingerprint["tool_call_count"] = tool_call_count
    return fingerprint
