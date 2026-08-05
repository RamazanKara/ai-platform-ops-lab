"""The native Anthropic Messages route (``POST /v1/messages``).

The endpoint is a translation shell around the same governed chat path chat uses:
``app.messages`` converts the Anthropic request to the internal OpenAI chat shape on the
way in and the completion (or chunk stream) back to Anthropic events on the way out,
so Anthropic-SDK agents get native streaming without a second, weaker governance path.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from app.audit import AUDIT_LOGGER
from app.governance import governed, record_stream_end, reserve_budget, resolve_single_route
from app.guardrails import _apply_output_guardrail, _apply_prompt_secret_mode
from app.messages import (
    AnthropicStreamTranslator,
    MessagesRequest,
    anthropic_stream_error_event,
    anthropic_to_chat_payload,
    chat_completion_to_anthropic,
    iter_sse_data_objects,
)
from app.request_context import _runtime_headers
from app.runtime_client import RuntimeClient
from app.runtime_routing import _open_stream_with_fallback
from app.settings import Settings


def register_messages_routes(app: FastAPI, settings: Settings) -> None:
    """Register the Anthropic Messages endpoint on the app."""

    @app.post(
        "/v1/messages",
        tags=["inference"],
        summary="Create a private Anthropic-style message",
        description=(
            "Native Anthropic Messages API endpoint. The request/response are translated "
            "to and from the internal OpenAI chat shape and routed through the SAME "
            "governance path as POST /v1/chat/completions: model allowlist, admission "
            "limits (including the max_tokens cap), prompt secret policy, sandbox budget, "
            "output guardrail, and audit. Anthropic requires max_tokens; a request omitting "
            "it is rejected. Streaming is supported: stream=true returns the Anthropic "
            "event sequence (message_start, content_block_start/delta/stop, message_delta, "
            "message_stop) translated from the same governed chat stream, subject to the "
            "gateway's streaming admission toggle."
        ),
        operation_id="createMessage",
    )
    async def messages_endpoint(request: Request, payload: MessagesRequest) -> dict[str, Any]:
        # Translate the Anthropic request into the internal OpenAI chat payload up front so
        # the whole governance path (admission, prompt-secret modes, budget, audit) operates
        # on the translated messages exactly as it does for chat - not a second, weaker path.
        payload_dict = anthropic_to_chat_payload(payload)
        request_model = payload.model
        async with governed(request, settings, route="/v1/messages", payload=payload_dict) as call:
            effective, model_route = resolve_single_route(request, settings, payload_dict)
            call.backend = model_route.backend
            if payload.stream:
                # Mark the translated payload as streaming BEFORE admission so the shared
                # streaming toggle (ALLOW_STREAMING) governs /v1/messages exactly as it
                # governs chat, rather than this endpoint carrying its own answer. Usage is
                # forced on so the terminal message_delta can report real token counts even
                # when the caller never asked for usage.
                payload_dict["stream"] = True
                payload_dict["stream_options"] = {"include_usage": True}
            # Same admission as chat, on the translated messages: enforces the max_tokens
            # cap, message/prompt-size limits, tool checks, and prompt-secret BLOCK mode.
            effective.validate_admission(payload_dict)
            # Redact/flag prompt secrets (non-block modes) on the translated messages before
            # the payload is reserved or sent, so a redacted credential is never forwarded.
            prompt_action = _apply_prompt_secret_mode(effective, payload_dict, call.route)
            if prompt_action:
                request.state.prompt_guardrail_action = prompt_action
            await reserve_budget(request, effective, payload_dict)
            client: RuntimeClient = request.app.state.runtime_client
            if payload_dict.get("stream"):
                # Single-route chain: /v1/messages resolves one model rather than a fallback
                # chain, so this reuses the chat opener without inventing failover behavior
                # the non-streaming path on this endpoint does not have.
                stream, stream_backend, used_model, first_chunk = await _open_stream_with_fallback(
                    client, [model_route], payload_dict, request
                )
                call.backend = stream_backend
                payload_dict["model"] = used_model
                call.stream_owns_recording = True
                translator = AnthropicStreamTranslator(request_model=request_model)

                async def stream_body() -> Any:
                    stream_status = "200"
                    stream_status_code = 200
                    stream_error: str | None = None
                    # SSE events can split across network chunks; carry the trailing partial
                    # line so no chunk object is ever parsed from half a ``data:`` line.
                    # Bounded so a never-terminated line cannot grow memory.
                    pending = b""
                    try:
                        chunk = first_chunk
                        while chunk is not None:
                            buffered = pending + chunk
                            complete_lines, newline, pending = buffered.rpartition(b"\n")
                            pending = pending[-65536:]
                            segment = complete_lines + newline
                            if segment:
                                for parsed in iter_sse_data_objects(segment):
                                    events = translator.feed(parsed)
                                    if events:
                                        yield events
                            try:
                                chunk = await stream.__anext__()
                            except StopAsyncIteration:
                                break
                        if pending:
                            for parsed in iter_sse_data_objects(pending):
                                events = translator.feed(parsed)
                                if events:
                                    yield events
                        yield translator.finish()
                    except httpx.HTTPError as exc:
                        # Upstream failed after headers were sent. An Anthropic client parses
                        # named events, so the failure is reported as a terminal ``error``
                        # event rather than the OpenAI-shaped error object.
                        stream_status = "502"
                        stream_status_code = 502
                        stream_error = "runtime stream failed"
                        AUDIT_LOGGER.debug("runtime stream error: %s", type(exc).__name__)
                        yield anthropic_stream_error_event(
                            "runtime stream failed",
                            request_id=request.state.request_id,
                            sandbox_id=request.state.sandbox_id,
                        )
                    finally:
                        # True end of stream. The translator kept the decoded assistant text,
                        # so the guardrail scan sees real text rather than its JSON escaping.
                        await record_stream_end(
                            request,
                            settings,
                            route=call.route,
                            backend=stream_backend,
                            start=call.start,
                            status=stream_status,
                            status_code=stream_status_code,
                            usage=translator.usage,
                            error=stream_error,
                            payload=payload_dict,
                            guardrail_text=translator.scanned_text,
                        )

                # FastAPI streams this Response object directly; the dict[str, Any] return
                # annotation describes the JSON path and drives the OpenAPI response schema.
                return StreamingResponse(stream_body(), media_type="text/event-stream")  # type: ignore[return-value]
            call.runtime_response = await client.chat_completions(
                payload_dict,
                headers=_runtime_headers(request),
                backend=call.backend,
            )
            # The output guardrail is endpoint-independent: /v1/messages must not be a bypass
            # around the redact/block policy the chat path enforces (OWASP LLM02:2025/LLM05:2025). It
            # runs on the OpenAI-shaped completion before translation back to Anthropic.
            _apply_output_guardrail(call.runtime_response, settings, call.route, request)
            # Translate the governed OpenAI completion back into an Anthropic Message; the
            # rail reads call.runtime_response (the OpenAI shape) for token-usage metrics
            # and the audit fingerprint.
            return chat_completion_to_anthropic(call.runtime_response, request_model=request_model)
