"""Agent-action receipts: the non-model half of what a coding agent did (ADR 0014).

The audit chain proved what an agent asked a model. It said nothing about what the agent
then did, because ``action_type`` only ever had one value. This module adds the rest of
the vocabulary and the validation behind ``POST /v1/receipts``, so a blocked egress
attempt or an executed tool lands on the same tamper-evident chain as the model call it
followed, and ``make audit-verify`` checks them together.

The boundary ADR 0014 draws is enforced here: this accepts and records claims. It is not
a control. A receipt saying an action was denied is a report that something else denied
it, and submitting one has never permitted anything.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.admission import AdmissionPolicyError
from app.settings import Settings

# A closed vocabulary, not free text: the crosswalk and evidence pack aggregate by action
# type, and a field anything can be written into is a field no report can group.
AGENT_ACTION_TYPES = frozenset(
    {
        "egress_denied",
        "egress_allowed",
        "tool_exec",
        "file_write",
        "credential_request",
        "workspace_lifecycle",
    }
)
RECEIPT_DECISIONS = frozenset({"allowed", "denied"})
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


class ReceiptRequest(BaseModel):
    """A single agent action reported by a workspace for the audit chain.

    Every free-text field is bounded and redacted before it is chained. A receipt is
    permanent, tamper-evident, and read by auditors, which makes it the last place a
    credential pasted into a command line should be allowed to come to rest. A producer
    that needs to commit to detail it cannot publish sends ``detail_sha256`` instead.
    """

    model_config = ConfigDict(extra="forbid")

    action_type: str
    decision: str
    # What the action was against: a host:port for egress, a command name for tool_exec, a
    # path for file_write. Not the full command line, which is what detail_sha256 is for.
    target: str | None = None
    tool: str | None = None
    reason: str | None = None
    detail_sha256: str | None = None
    # Ties this action to the model call that prompted it, so a reviewer can walk from a
    # completion to what the agent did with it.
    correlation_request_id: str | None = None
    sandbox_id: str | None = None


def validate_receipt(payload: ReceiptRequest, settings: Settings) -> None:
    """Reject a receipt that is outside the reviewed vocabulary or the size bounds."""
    if payload.action_type not in AGENT_ACTION_TYPES:
        raise AdmissionPolicyError(
            "unknown_action_type",
            f"action_type must be one of: {', '.join(sorted(AGENT_ACTION_TYPES))}",
        )
    if payload.decision not in RECEIPT_DECISIONS:
        raise AdmissionPolicyError("invalid_decision", "decision must be one of: allowed, denied")
    if payload.detail_sha256 is not None and not _SHA256_HEX.match(payload.detail_sha256):
        raise AdmissionPolicyError("invalid_detail_digest", "detail_sha256 must be a lowercase hex SHA-256 digest")
    limit = settings.agent_receipt_max_field_chars
    for name in ("target", "tool", "reason", "correlation_request_id"):
        value = getattr(payload, name)
        if value is not None and len(value) > limit:
            raise AdmissionPolicyError(
                "receipt_field_too_large",
                f"{name} has {len(value)} characters; limit is {limit}",
            )


def build_receipt_event(payload: ReceiptRequest, settings: Settings, *, sandbox_id: str) -> dict[str, Any]:
    """Build the chainable receipt for a validated agent action.

    Free-text fields run through the same output-guardrail redaction the completion path
    uses, so a credential in a reported command is replaced by its pattern name before it
    is committed to a chain nobody can rewrite.
    """
    event: dict[str, Any] = {
        "event": "agent_action",
        "action_type": payload.action_type,
        "decision": payload.decision,
        "sandbox_id": sandbox_id,
        "target": _redacted(payload.target, settings),
        "tool": _redacted(payload.tool, settings),
        "reason": _redacted(payload.reason, settings),
        "detail_sha256": payload.detail_sha256,
        "correlation_request_id": payload.correlation_request_id,
    }
    return event


def _redacted(value: str | None, settings: Settings) -> str | None:
    """Return the value with any recognized secret or blocked term substituted out."""
    if value is None:
        return None
    redacted, _ = settings.redact_output_text(value)
    return redacted
