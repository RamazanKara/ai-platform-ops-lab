"""OpenAI-compatible inference routes: chat, completions, embeddings, moderations, batch.

Every handler here runs on the shared governance rail (``app.governance``): one model
policy, one budget reservation, one failure taxonomy, one receipt per request. The
handlers own only what makes their endpoint distinct - chat's failover chain and SSE
stream, completions' prompt-shaped budget synthesis, embeddings' input metering, the
moderations classifier, and the synchronous batch fan-out.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from time import perf_counter, time
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.audit import AUDIT_LOGGER, chain_audit_event, payload_fingerprint
from app.budget import BudgetBackendError, SandboxBudgetTracker
from app.cache import cache_key
from app.governance import (
    admission_status,
    effective_settings,
    governed,
    record_estimated_cost,
    record_stream_end,
    record_token_usage,
    reserve_budget,
    resolve_single_route,
    route_settings,
    settle_and_audit,
)
from app.guardrails import _apply_output_guardrail, _apply_prompt_secret_mode
from app.metrics import (
    CACHE_LOOKUPS,
    CANARY_ROUTED,
    LATENCY,
    REQUESTS,
    RUNTIME_FALLBACKS,
    SANDBOX_REQUESTS,
)
from app.metrics import (
    sandbox_label as _sandbox_label,
)
from app.policy import ModelRoutingPolicy, SandboxPolicySet
from app.request_context import _runtime_headers
from app.runtime_client import RuntimeClient
from app.runtime_routing import _is_failover_worthy, _open_stream_with_fallback, _schedule_shadow
from app.schemas import (
    BatchRequest,
    ChatCompletionRequest,
    CompletionRequest,
    EmbeddingsRequest,
    ModerationRequest,
)
from app.settings import AdmissionPolicyError, Settings, completion_prompt_texts, moderate_text
from app.streaming import (
    _rewrite_stream_segment,
    _terminal_stream_error_event,
    _usage_from_sse_chunk,
)


def register_inference_routes(app: FastAPI, settings: Settings) -> None:
    """Register the OpenAI-compatible inference endpoints on the app."""

    @app.post(
        "/v1/chat/completions",
        tags=["inference"],
        summary="Create a private chat completion",
        operation_id="createChatCompletion",
    )
    async def chat_completions(request: Request, payload: ChatCompletionRequest) -> dict[str, Any]:
        payload_dict = payload.model_dump(exclude_none=True)
        async with governed(request, settings, route="/v1/chat/completions", payload=payload_dict) as call:
            policy: ModelRoutingPolicy = request.app.state.model_routing_policy
            sandbox_policies: SandboxPolicySet = request.app.state.sandbox_policy_set
            effective = effective_settings(request, sandbox_policies, settings)
            # Chat resolves a failover *chain* (primary plus fallbacks) rather than the
            # single route every other endpoint uses, so it keeps its own prologue instead
            # of resolve_single_route.
            try:
                chain = policy.resolve_chain(payload_dict.get("model"), effective.model_id)
            except ValueError as exc:
                raise AdmissionPolicyError("model_not_allowed", str(exc)) from exc
            # Progressive delivery: resolve the shadow target from the originally-resolved
            # primary, then apply weighted canary selection (which may swap chain[0]).
            primary_route = chain[0]
            shadow_route = None if payload_dict.get("stream") else policy.shadow_target(primary_route)
            canary = policy.canary_target(primary_route, random.random())
            if canary.model_id != primary_route.model_id:
                CANARY_ROUTED.labels(primary_route.model_id, canary.model_id).inc()
                chain = [canary, *chain[1:]]
            model_route = chain[0]
            call.backend = model_route.backend
            payload_dict["model"] = model_route.model_id
            effective = route_settings(effective, model_route)
            effective.validate_admission(payload_dict)
            # Redact/flag prompt secrets (non-block modes) before the payload is cached,
            # reserved, or sent - so a redacted credential is never persisted or forwarded.
            prompt_action = _apply_prompt_secret_mode(effective, payload_dict, call.route)
            if prompt_action:
                request.state.prompt_guardrail_action = prompt_action
            # Exact-match per-sandbox cache (non-streaming only). A hit returns the prior
            # response without a runtime call or budget reservation.
            cache_enabled = settings.response_cache_enabled and not payload_dict.get("stream")
            cache_id = ""
            if cache_enabled:
                cache_id = cache_key(request.state.sandbox_id, payload_dict)
                cached = await asyncio.to_thread(request.app.state.response_cache.get, cache_id)
                if cached is not None:
                    CACHE_LOOKUPS.labels("hit").inc()
                    request.state.cache_status = "HIT"
                    call.cache_hit = True
                    # Bind for the rail's audit receipt, then return.
                    call.runtime_response = cached
                    return cached
                CACHE_LOOKUPS.labels("miss").inc()
                request.state.cache_status = "MISS"
            await reserve_budget(request, effective, payload_dict)
            client: RuntimeClient = request.app.state.runtime_client
            if payload_dict.get("stream"):
                # Force the runtime to emit a terminal usage event so streamed traffic is
                # metered even when the caller forgot ``stream_options.include_usage``.
                # If the caller did not ask for usage, the induced event is filtered back
                # out below so the client-facing stream is unchanged.
                existing_stream_options = payload_dict.get("stream_options")
                has_stream_options = isinstance(existing_stream_options, dict)
                client_wants_usage = has_stream_options and bool(existing_stream_options.get("include_usage"))
                merged_stream_options = dict(existing_stream_options) if has_stream_options else {}
                merged_stream_options["include_usage"] = True
                payload_dict["stream_options"] = merged_stream_options
                # Open the stream with cross-runtime fallback: a pre-first-byte failure
                # on the primary route retries the next route in the chain. Once a byte
                # is yielded the response is committed and cannot fail over.
                stream, stream_backend, used_model, first_chunk = await _open_stream_with_fallback(
                    client, chain, payload_dict, request
                )
                call.backend = stream_backend
                payload_dict["model"] = used_model
                call.stream_owns_recording = True

                async def stream_body() -> Any:
                    stream_status = "200"
                    stream_status_code = 200
                    stream_error: str | None = None
                    usage: dict[str, Any] | None = None
                    # The streamed bytes are already committed to the wire, so the output
                    # guardrail cannot redact/block them mid-stream; instead accumulate a
                    # bounded copy and detect+flag at end-of-stream (enforce via non-stream).
                    scan_enabled = settings.output_guardrail_enabled
                    scanned = bytearray()
                    # Rewrite complete SSE segments before forwarding: drop the induced
                    # usage-only event when the caller did not ask for usage, and strip
                    # reasoning/thinking deltas (matching the non-streaming redaction).
                    drop_usage_only = not client_wants_usage
                    # SSE events can split across network chunks; carry the trailing
                    # partial line so the terminal usage object is parsed - and so no
                    # rewrite ever sees half a ``data:`` line - even when it straddles a
                    # chunk boundary. Bounded so a pathological never-terminated line
                    # cannot grow memory.
                    pending = b""
                    try:
                        chunk = first_chunk
                        while chunk is not None:
                            buffered = pending + chunk
                            # rpartition: everything up to and including the last newline
                            # is complete; with no newline the whole buffer stays pending.
                            complete_lines, newline, pending = buffered.rpartition(b"\n")
                            pending = pending[-65536:]
                            # segment is every byte up to and including the last newline;
                            # guarding on `segment` (not `complete_lines`) keeps a lone
                            # trailing "\n" - e.g. an event terminator flushed in its own
                            # chunk - from being silently dropped.
                            segment = complete_lines + newline
                            if segment:
                                parsed_usage = _usage_from_sse_chunk(segment)
                                if parsed_usage is not None:
                                    usage = parsed_usage
                                segment = _rewrite_stream_segment(
                                    segment, drop_usage_only=drop_usage_only, strip_reasoning=True
                                )
                                if scan_enabled and len(scanned) < 262144:
                                    scanned.extend(segment)
                                if segment:
                                    yield segment
                            try:
                                chunk = await stream.__anext__()
                            except StopAsyncIteration:
                                break
                        if pending:
                            parsed_usage = _usage_from_sse_chunk(pending)
                            if parsed_usage is not None:
                                usage = parsed_usage
                            tail = _rewrite_stream_segment(
                                pending, drop_usage_only=drop_usage_only, strip_reasoning=True
                            )
                            if scan_enabled and len(scanned) < 262144:
                                scanned.extend(tail)
                            if tail:
                                yield tail
                    except httpx.HTTPError as exc:
                        # Upstream failed after headers were sent: emit a terminal SSE
                        # error event and map the recorded status to 502.
                        stream_status = "502"
                        stream_status_code = 502
                        stream_error = "runtime stream failed"
                        AUDIT_LOGGER.debug("runtime stream error: %s", type(exc).__name__)
                        yield _terminal_stream_error_event(stream_backend, request)
                    finally:
                        # True end of stream: record metrics, token usage, and audit now.
                        await record_stream_end(
                            request,
                            settings,
                            route=call.route,
                            backend=stream_backend,
                            start=call.start,
                            status=stream_status,
                            status_code=stream_status_code,
                            usage=usage,
                            error=stream_error,
                            payload=payload_dict,
                            guardrail_text=scanned.decode("utf-8", "ignore") if scan_enabled else "",
                        )

                # FastAPI streams this Response object directly; the dict[str, Any] return
                # annotation describes the JSON path and drives the OpenAPI response schema.
                return StreamingResponse(stream_body(), media_type="text/event-stream")  # type: ignore[return-value]
            # Non-streaming: try each route in the chain, failing over to the next on a
            # retryable/connection error or open circuit. call.backend and the payload model
            # are updated to the route that actually served, so metrics and audit reflect it.
            last_exc: httpx.HTTPError | None = None
            for index, candidate in enumerate(chain):
                attempt = dict(payload_dict)
                attempt["model"] = candidate.model_id
                call.backend = candidate.backend
                try:
                    call.runtime_response = await client.chat_completions(
                        attempt,
                        headers=_runtime_headers(request),
                        backend=candidate.backend,
                    )
                    payload_dict["model"] = candidate.model_id
                    break
                except httpx.HTTPError as exc:
                    last_exc = exc
                    if _is_failover_worthy(exc) and index + 1 < len(chain):
                        RUNTIME_FALLBACKS.labels(candidate.backend, chain[index + 1].backend).inc()
                        continue
                    raise
            if call.runtime_response is None:
                raise last_exc or RuntimeError("no runtime route available")
            # Inspect the completion before it is cached or returned: redact/block leaked
            # credentials, PII, or denied content (OWASP LLM02:2025/LLM05:2025). Applied pre-cache so
            # a secret is never persisted in the response cache.
            _apply_output_guardrail(call.runtime_response, settings, call.route, request)
            if cache_enabled:
                await asyncio.to_thread(request.app.state.response_cache.set, cache_id, call.runtime_response)
            if shadow_route is not None:
                _schedule_shadow(client, shadow_route, payload_dict, request)
            return call.runtime_response

    @app.post(
        "/v1/completions",
        tags=["inference"],
        summary="Create a private legacy text completion",
        description=(
            "OpenAI-compatible legacy text-completion endpoint (prompt-based, pre-chat). "
            "Routed through the same governance path as chat: model allowlist, admission "
            "limits, prompt secret policy, sandbox budget, output guardrail, and audit. "
            "Streaming is not supported on this endpoint in this release; send stream=false "
            "or use POST /v1/chat/completions for streaming."
        ),
        operation_id="createCompletion",
    )
    async def completions(request: Request, payload: CompletionRequest) -> dict[str, Any]:
        payload_dict = payload.model_dump(exclude_none=True)
        async with governed(request, settings, route="/v1/completions", payload=payload_dict) as call:
            effective, model_route = resolve_single_route(request, settings, payload_dict)
            call.backend = model_route.backend
            # Legacy completions do not use the chat SSE usage/guardrail machinery. Reject
            # streaming explicitly rather than forwarding an unmetered stream.
            if payload_dict.get("stream"):
                raise AdmissionPolicyError(
                    "streaming_not_supported",
                    "streaming is not supported on /v1/completions; use /v1/chat/completions",
                )
            effective.validate_completion_admission(payload_dict)
            prompt_action = _apply_prompt_secret_mode(effective, payload_dict, call.route)
            if prompt_action:
                request.state.prompt_guardrail_action = prompt_action
            # Budget the prompt like chat by synthesizing a messages-shaped payload from the
            # prompt text. Carry the completion-cap fields and n through verbatim (do NOT
            # hardcode max_tokens=0, which is the embeddings "no completion" signal) so
            # budget_delta applies the same cap fallback and n multiplication chat gets -
            # otherwise a caller omitting max_tokens, or using max_completion_tokens or n,
            # would be charged for the prompt only while the runtime generates far more.
            prompt_texts = completion_prompt_texts(payload_dict.get("prompt"))
            budget_payload: dict[str, Any] = {"messages": [{"content": text} for text in prompt_texts]}
            for cap_field in ("max_tokens", "max_completion_tokens", "n"):
                value = payload_dict.get(cap_field)
                if value is not None:
                    budget_payload[cap_field] = value
            await reserve_budget(request, effective, budget_payload)
            client: RuntimeClient = request.app.state.runtime_client
            call.runtime_response = await client.completions(
                payload_dict,
                headers=_runtime_headers(request),
                backend=call.backend,
            )
            _apply_output_guardrail(call.runtime_response, settings, call.route, request, legacy_completion=True)
            return call.runtime_response

    @app.post(
        "/v1/embeddings",
        tags=["inference"],
        summary="Create private embeddings",
        operation_id="createEmbeddings",
    )
    async def embeddings(request: Request, payload: EmbeddingsRequest) -> dict[str, Any]:
        payload_dict = payload.model_dump(exclude_none=True)
        async with governed(request, settings, route="/v1/embeddings", payload=payload_dict) as call:
            effective, model_route = resolve_single_route(request, settings, payload_dict)
            call.backend = model_route.backend
            effective.validate_embedding_admission(payload_dict)
            prompt_action = _apply_prompt_secret_mode(effective, payload_dict, call.route)
            if prompt_action:
                request.state.prompt_guardrail_action = prompt_action
            # Count embedding inputs against the sandbox budget the same way prompts are.
            raw_input = payload_dict.get("input")
            texts = raw_input if isinstance(raw_input, list) else [raw_input]
            budget_payload = {"messages": [{"content": str(text)} for text in texts], "max_tokens": 0}
            await reserve_budget(request, effective, budget_payload)
            client: RuntimeClient = request.app.state.runtime_client
            call.runtime_response = await client.embeddings(
                payload_dict,
                headers=_runtime_headers(request),
                backend=call.backend,
            )
            return call.runtime_response

    @app.post(
        "/v1/moderations",
        tags=["inference"],
        summary="Classify content against the gateway policy",
        operation_id="createModeration",
    )
    async def moderations(request: Request, payload: ModerationRequest) -> dict[str, Any]:
        route = "/v1/moderations"
        start = perf_counter()
        status = "200"
        status_code = 200
        error = None
        payload_dict = payload.model_dump(exclude_none=True)
        try:
            raw_input = payload_dict.get("input")
            texts = raw_input if isinstance(raw_input, list) else [raw_input]
            texts = [str(item) for item in texts if item is not None]
            if not texts:
                raise AdmissionPolicyError("missing_input", "moderations request must include non-empty input")
            # Same admission ceiling as chat/embeddings: without it this is the one
            # endpoint where an arbitrarily large body reaches every classifier regex.
            total_chars = sum(len(text) for text in texts)
            if total_chars > settings.max_prompt_chars:
                raise AdmissionPolicyError(
                    "input_too_large",
                    f"moderations input has {total_chars} characters; limit is {settings.max_prompt_chars}",
                )
            results = [moderate_text(text, settings) for text in texts]
            return {
                "id": f"modr-{request.state.request_id}",
                "model": payload_dict.get("model") or "platform-content-policy",
                # Honesty marker: these categories are the governance taxonomy
                # (credential/pii/blocked_terms), NOT OpenAI's harm taxonomy. The field
                # lets a client tell the two response shapes apart.
                "taxonomy": "governance",
                "results": results,
            }
        except AdmissionPolicyError as exc:
            status_code = 400
            status = "400"
            error = str(exc)
            raise HTTPException(
                status_code=400,
                detail={
                    "message": error,
                    "reason": exc.reason,
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
            ) from exc
        finally:
            REQUESTS.labels(route, settings.runtime_backend, status).inc()
            SANDBOX_REQUESTS.labels(_sandbox_label(request.state.sandbox_id), settings.runtime_backend, status).inc()
            latency_seconds = perf_counter() - start
            LATENCY.labels(route, settings.runtime_backend).observe(latency_seconds)
            await settle_and_audit(
                settings,
                request,
                payload_dict,
                status_code=status_code,
                latency_seconds=latency_seconds,
                backend=settings.runtime_backend,
                error=error,
            )

    async def _run_batch(request: Request, payload: BatchRequest, route: str) -> dict[str, Any]:
        start = perf_counter()
        status = "200"
        status_code = 200
        error = None
        # Per-item audit fingerprints (redacted counts + prompt hash, same fields as
        # single requests) so batch traffic is attributable item-by-item, not just as
        # an opaque batch_size. Defined before the try so the audit finally always
        # sees it, including on whole-batch rejections.
        audit_items: list[dict[str, Any]] = []
        try:
            if len(payload.requests) > settings.max_batch_requests:
                raise AdmissionPolicyError(
                    "batch_too_large",
                    f"batch has {len(payload.requests)} requests; limit is {settings.max_batch_requests}",
                )
            policy: ModelRoutingPolicy = request.app.state.model_routing_policy
            sandbox_policies: SandboxPolicySet = request.app.state.sandbox_policy_set
            effective = effective_settings(request, sandbox_policies, settings)
            tracker: SandboxBudgetTracker = request.app.state.budget_tracker
            client: RuntimeClient = request.app.state.runtime_client
            # Bound per-batch fan-out so one batch cannot saturate the upstream pool.
            semaphore = asyncio.Semaphore(min(8, max(1, len(payload.requests))))

            def _audit_item(
                index: int, status_code: int, item_dict: dict[str, Any], prompt_guardrail_action: str | None = None
            ) -> None:
                entry: dict[str, Any] = {
                    "index": index,
                    "status_code": status_code,
                    "model": item_dict.get("model") or effective.model_id,
                }
                if prompt_guardrail_action is not None:
                    entry["prompt_guardrail_action"] = prompt_guardrail_action
                entry.update(payload_fingerprint(item_dict))
                audit_items.append(entry)

            async def _process(index: int, item: ChatCompletionRequest) -> dict[str, Any]:
                item_dict = item.model_dump(exclude_none=True)
                item_dict.pop("stream", None)
                item_prompt_action: str | None = None
                async with semaphore:
                    try:
                        try:
                            model_route = policy.resolve(item_dict.get("model"), effective.model_id)
                        except ValueError as exc:
                            raise AdmissionPolicyError("model_not_allowed", str(exc)) from exc
                        item_dict["model"] = model_route.model_id
                        effective.validate_admission(item_dict)
                        # Attribute the redact/flag action to this item's audit receipt rather
                        # than the one shared per-request field (concurrent items would race it).
                        item_prompt_action = _apply_prompt_secret_mode(effective, item_dict, route)
                        await asyncio.to_thread(tracker.reserve, request.state.sandbox_id, item_dict, effective)
                        response = await client.chat_completions(
                            item_dict, headers=_runtime_headers(request), backend=model_route.backend
                        )
                        # The output guardrail is an endpoint-independent control: a batch
                        # item must not be a bypass around the redact/block policy that the
                        # single-request path enforces (OWASP LLM02:2025/LLM05:2025).
                        _apply_output_guardrail(response, settings, route, request)
                        record_token_usage(model_route.backend, response)
                        record_estimated_cost(
                            settings,
                            request.state.sandbox_id,
                            model_route.backend,
                            response.get("usage") if isinstance(response, dict) else None,
                        )
                        _audit_item(index, 200, item_dict, item_prompt_action)
                        return {"index": index, "status_code": 200, "response": response}
                    except AdmissionPolicyError as exc:
                        item_code, _ = admission_status(exc.reason, settings)
                        _audit_item(index, item_code, item_dict)
                        return {
                            "index": index,
                            "status_code": item_code,
                            "error": {"reason": exc.reason, "message": str(exc)},
                        }
                    except httpx.HTTPStatusError as exc:
                        _audit_item(index, 502, item_dict, item_prompt_action)
                        return {
                            "index": index,
                            "status_code": 502,
                            "error": {
                                "message": "runtime returned an error",
                                "runtime_status": exc.response.status_code,
                            },
                        }
                    except BudgetBackendError:
                        _audit_item(index, 503, item_dict, item_prompt_action)
                        return {
                            "index": index,
                            "status_code": 503,
                            "error": {"reason": "budget_backend_unavailable", "message": "budget backend unavailable"},
                        }
                    except (httpx.HTTPError, ValueError):
                        _audit_item(index, 502, item_dict, item_prompt_action)
                        return {"index": index, "status_code": 502, "error": {"message": "runtime request failed"}}

            results = await asyncio.gather(*[_process(index, item) for index, item in enumerate(payload.requests)])
            return {"object": "batch", "count": len(results), "results": list(results)}
        except AdmissionPolicyError as exc:
            status_code, headers = admission_status(exc.reason, settings)
            status = str(status_code)
            error = str(exc)
            raise HTTPException(
                status_code=status_code,
                detail={
                    "message": error,
                    "reason": exc.reason,
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
                headers=headers,
            ) from exc
        finally:
            REQUESTS.labels(route, settings.runtime_backend, status).inc()
            SANDBOX_REQUESTS.labels(_sandbox_label(request.state.sandbox_id), settings.runtime_backend, status).inc()
            latency_seconds = perf_counter() - start
            LATENCY.labels(route, settings.runtime_backend).observe(latency_seconds)
            if settings.audit_log_enabled:
                event = {
                    "event": "batch_request",
                    # Per-process chain identity (hash-covered); see _write_audit_log.
                    "chain_id": getattr(request.app.state, "audit_chain_id", None),
                    "action_type": "model_call",
                    "decision": "allowed" if status_code < 400 else "denied",
                    "request_id": request.state.request_id,
                    "traceparent": request.state.traceparent,
                    "sandbox_id": request.state.sandbox_id,
                    "principal": getattr(request.state, "principal", None),
                    "batch_size": len(payload.requests),
                    "items": sorted(audit_items, key=lambda entry: entry["index"]),
                    "status_code": status_code,
                    "latency_ms": round(latency_seconds * 1000, 2),
                    "ts": time(),
                    "error": error,
                }
                chain_audit_event(request, event)
                # Emit to both the audit logger and uvicorn.error so batch receipts reach
                # pod logs (kubectl logs / Loki) exactly like inference_request events; a
                # receipt that advanced the chain but was invisible would read as a hole
                # to an operator verifying the chain against the logs.
                batch_line = json.dumps(event, sort_keys=True)
                AUDIT_LOGGER.info(batch_line)
                logging.getLogger("uvicorn.error").info(batch_line)

    _BATCH_DESCRIPTION = (
        "Synchronous, size-bounded (MAX_BATCH_REQUESTS) fan-out that runs every item "
        "concurrently and returns per-item results inline. This is NOT the OpenAI "
        "asynchronous file-batch API: there is no batch id, status polling, result-file "
        "retrieval, or cancellation. Use it for small offline batches that fit within the "
        "request timeout and gateway concurrency limit."
    )

    @app.post(
        "/v1/batch-inference",
        tags=["inference"],
        summary="Process a batch of chat completions synchronously",
        description=_BATCH_DESCRIPTION,
        operation_id="createBatchInference",
    )
    async def batch_inference(request: Request, payload: BatchRequest) -> dict[str, Any]:
        return await _run_batch(request, payload, "/v1/batch-inference")
