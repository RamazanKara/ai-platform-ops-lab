"""The OpenAI Responses routes: create, retrieve, delete, and list input items.

``POST /v1/responses`` is a translation shell around the governed chat path, exactly like
``/v1/messages``. The optional server-side state (ADR 0012) lives here too: persistence
of stored responses, ``previous_response_id`` chaining, and the tenant-scoped retrieval
routes that only answer when the store is enabled.
"""

from __future__ import annotations

from time import time
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request

from app.governance import governed, reserve_budget, resolve_single_route
from app.guardrails import _apply_output_guardrail, _apply_prompt_secret_mode
from app.request_context import _runtime_headers
from app.response_store import StoredResponse
from app.responses import (
    ResponsesRequest,
    chat_completion_to_responses,
    responses_to_chat_payload,
)
from app.runtime_client import RuntimeClient
from app.settings import AdmissionPolicyError, Settings


def _assistant_reply_message(runtime_response: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a minimal stored assistant turn (role/content, tool_calls if any) for chaining."""
    if not isinstance(runtime_response, dict):
        return None
    choices = runtime_response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return None
    stored: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
    if message.get("tool_calls"):
        stored["tool_calls"] = message["tool_calls"]
    return stored


def _persist_response(
    response_store: Any,
    request: Request,
    payload: ResponsesRequest,
    payload_dict: dict[str, Any],
    runtime_response: dict[str, Any] | None,
    responses_body: dict[str, Any],
) -> dict[str, Any]:
    """Persist a stored Responses object under a stable id and return the body carrying that id.

    Stores the running conversation (the governed messages sent to the runtime plus the assistant
    reply) for ``previous_response_id`` chaining and the turn's input items for the input-items
    endpoint. Content is raw (ADR 0012): tenant-scoped and TTL-bounded by the store.
    """
    response_id = f"resp_{uuid4().hex}"
    responses_body = dict(responses_body)
    responses_body["id"] = response_id
    responses_body["previous_response_id"] = payload.previous_response_id
    if payload.metadata is not None:
        responses_body["metadata"] = payload.metadata
    conversation = list(payload_dict.get("messages", []))
    assistant_message = _assistant_reply_message(runtime_response)
    if assistant_message is not None:
        conversation.append(assistant_message)
    raw_input = payload.input
    input_items = (
        raw_input
        if isinstance(raw_input, list)
        else [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": str(raw_input)}]}]
    )
    response_store.create(
        StoredResponse(
            id=response_id,
            tenant=request.state.sandbox_id,
            created_at=int(responses_body.get("created_at") or time()),
            model=str(responses_body.get("model") or ""),
            body=responses_body,
            input_items=input_items,
            messages=conversation,
            previous_response_id=payload.previous_response_id,
        )
    )
    return responses_body


def _responses_disabled() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"message": "server-side response state is not enabled", "reason": "stateful_not_supported"},
    )


def _response_not_found(response_id: str) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"message": f"no stored response with id '{response_id}'", "reason": "response_not_found"},
    )


def _require_stored_response(request: Request, response_id: str) -> StoredResponse:
    """Return the tenant-scoped stored response, or raise 404 when disabled/absent."""
    store = getattr(request.app.state, "response_store", None)
    if store is None:
        raise _responses_disabled()
    record = store.get(request.state.sandbox_id, response_id)
    if record is None:
        raise _response_not_found(response_id)
    return record


def register_responses_routes(app: FastAPI, settings: Settings) -> None:
    """Register the Responses endpoints (stateless create plus the stored-response reads)."""

    @app.post(
        "/v1/responses",
        tags=["inference"],
        summary="Create a private OpenAI Responses API response",
        description=(
            "OpenAI Responses API endpoint (stateless subset). The request/response are "
            "translated to and from the internal OpenAI chat shape and routed through the "
            "SAME governance path as POST /v1/chat/completions: model allowlist, admission "
            "limits (including the max_output_tokens cap), prompt secret policy, sandbox "
            "budget, output guardrail, and audit. This subset is STATELESS: server-side "
            "conversation state is not implemented, so store=true or previous_response_id is "
            "rejected with stateful_not_supported rather than silently ignored. Streaming is "
            "not supported on this endpoint in this release; send stream=false or use POST "
            "/v1/chat/completions for OpenAI-shaped streaming."
        ),
        operation_id="createResponse",
    )
    async def responses_endpoint(request: Request, payload: ResponsesRequest) -> dict[str, Any]:
        # Translate the Responses request into the internal OpenAI chat payload up front so
        # the whole governance path (admission, prompt-secret modes, budget, audit) operates
        # on the translated messages exactly as it does for chat - not a second, weaker path.
        payload_dict = responses_to_chat_payload(payload)
        request_model = payload.model
        async with governed(request, settings, route="/v1/responses", payload=payload_dict) as call:
            # Server-side response state (ADR 0012) is opt-in. When no store is configured, reject
            # store / previous_response_id rather than silently dropping them and returning a
            # response the caller wrongly believes was remembered (the stateless subset).
            response_store = getattr(request.app.state, "response_store", None)
            if (payload.store or payload.previous_response_id is not None) and response_store is None:
                raise AdmissionPolicyError(
                    "stateful_not_supported",
                    "server-side response state is not enabled on this gateway "
                    "(RESPONSES_STORE_ENABLED); store / previous_response_id are unavailable",
                )
            if payload.previous_response_id is not None and response_store is not None:
                prior = response_store.get(request.state.sandbox_id, payload.previous_response_id)
                if prior is None:
                    raise AdmissionPolicyError(
                        "previous_response_not_found",
                        f"no stored response with id '{payload.previous_response_id}' for this sandbox",
                    )
                # Prepend the prior conversation so the stateless runtime sees the full history.
                # The rebind must reach the rail too: the receipt fingerprints what was
                # actually sent, which is now this payload, not the one the rail was opened with.
                payload_dict = responses_to_chat_payload(payload, base_messages=prior.messages)
                call.payload = payload_dict
            effective, model_route = resolve_single_route(request, settings, payload_dict)
            call.backend = model_route.backend
            # Streaming translation to the Responses SSE event sequence is not wired through
            # the metering/guardrail machinery yet; reject it explicitly (mirroring
            # /v1/completions & /v1/messages' streaming_not_supported) rather than silently
            # forwarding an unmetered or non-Responses-shaped stream.
            if payload.stream:
                raise AdmissionPolicyError(
                    "streaming_not_supported",
                    "streaming is not supported on /v1/responses; use /v1/chat/completions",
                )
            # Same admission as chat, on the translated messages: enforces the max_tokens
            # cap (from max_output_tokens), message/prompt-size limits, tool checks, and the
            # prompt-secret BLOCK mode. A missing/empty input translates to no messages and is
            # rejected here by the shared missing_messages check.
            effective.validate_admission(payload_dict)
            # Redact/flag prompt secrets (non-block modes) on the translated messages before
            # the payload is reserved or sent, so a redacted credential is never forwarded.
            prompt_action = _apply_prompt_secret_mode(effective, payload_dict, call.route)
            if prompt_action:
                request.state.prompt_guardrail_action = prompt_action
            await reserve_budget(request, effective, payload_dict)
            client: RuntimeClient = request.app.state.runtime_client
            call.runtime_response = await client.chat_completions(
                payload_dict,
                headers=_runtime_headers(request),
                backend=call.backend,
            )
            # The output guardrail is endpoint-independent: /v1/responses must not be a bypass
            # around the redact/block policy the chat path enforces (OWASP LLM02:2025/LLM05:2025). It
            # runs on the OpenAI-shaped completion before translation to the Responses shape.
            _apply_output_guardrail(call.runtime_response, settings, call.route, request)
            responses_body = chat_completion_to_responses(call.runtime_response, request_model=request_model)
            # Persist when the caller asked to store it (and the store is enabled), so it can be
            # retrieved and chained via previous_response_id (ADR 0012).
            if payload.store and response_store is not None:
                responses_body = _persist_response(
                    response_store, request, payload, payload_dict, call.runtime_response, responses_body
                )
            return responses_body

    @app.get(
        "/v1/responses/{response_id}",
        tags=["inference"],
        summary="Retrieve a stored response (requires the response store)",
        operation_id="getResponse",
    )
    async def get_response(request: Request, response_id: str) -> dict[str, Any]:
        return _require_stored_response(request, response_id).body

    @app.delete(
        "/v1/responses/{response_id}",
        tags=["inference"],
        summary="Delete a stored response",
        operation_id="deleteResponse",
    )
    async def delete_response(request: Request, response_id: str) -> dict[str, Any]:
        store = getattr(request.app.state, "response_store", None)
        if store is None:
            raise _responses_disabled()
        if not store.delete(request.state.sandbox_id, response_id):
            raise _response_not_found(response_id)
        return {"id": response_id, "object": "response.deleted", "deleted": True}

    @app.get(
        "/v1/responses/{response_id}/input_items",
        tags=["inference"],
        summary="List the input items of a stored response",
        operation_id="listResponseInputItems",
    )
    async def response_input_items(request: Request, response_id: str) -> dict[str, Any]:
        return {"object": "list", "data": _require_stored_response(request, response_id).input_items}
