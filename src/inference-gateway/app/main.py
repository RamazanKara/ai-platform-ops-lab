"""OpenAI-compatible inference gateway with auth, admission, budgets, and runtime routing."""

import asyncio
import json
import logging
import os
import random
from dataclasses import replace
from time import perf_counter, time
from typing import Any
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.audit import (
    AUDIT_GENESIS,
    ChainHead,
    advance_chain,
    build_chain_store,
    chain_audit_event,
    chain_start_event,
    payload_fingerprint,
)
from app.batch_api import register_batch_routes
from app.batchstore import build_batch_store
from app.body_limit import RequestBodyLimitMiddleware
from app.budget import (
    BudgetBackendError,
    BudgetReservation,
    SandboxBudgetTracker,
    build_sandbox_budget_tracker,
)
from app.cache import build_response_cache, cache_key
from app.guardrails import _apply_output_guardrail, _apply_prompt_secret_mode
from app.jwt_auth import JwksUnavailableError, JwtVerifier
from app.key_records import KeyRecordSet, key_record_effective_budget_updates
from app.messages import (
    AnthropicStreamTranslator,
    MessagesRequest,
    anthropic_stream_error_event,
    anthropic_to_chat_payload,
    chat_completion_to_anthropic,
    iter_sse_data_objects,
)
from app.metrics import (
    ADMISSION_REJECTIONS,
    AGENT_RECEIPTS,
    AUDIT_CHAIN_PERSIST,
    BUDGET_SETTLED_TOKENS,
    BUDGET_SETTLEMENTS,
    CACHE_LOOKUPS,
    CANARY_ROUTED,
    ESTIMATED_COST,
    INFLIGHT,
    LATENCY,
    OUTPUT_GUARDRAIL,
    RATE_LIMIT_FAIL_OPEN,
    REQUESTS,
    RUNTIME_FALLBACKS,
    SANDBOX_BUDGET_LIMIT,
    SANDBOX_BUDGET_USAGE,
    SANDBOX_REQUESTS,
    TOKEN_USAGE,
)
from app.metrics import (
    sandbox_label as _sandbox_label,
)
from app.objectstore import build_object_store
from app.policy import ModelRoutingPolicy, SandboxPolicySet
from app.ratelimit import build_rate_limiter
from app.receipts import ReceiptRequest, build_receipt_event, validate_receipt
from app.request_context import (
    ApiKeyOutcome,
    _api_key_principal,
    _auth_failure_response,
    _auth_required,
    _bound_sandbox_id,
    _error_envelope,
    _install_openapi_contract,
    _jwks_unavailable_response,
    _jwt_principal,
    _overloaded_response,
    _rate_limit_backend_unavailable_response,
    _rate_limited_response,
    _request_id_from_header,
    _resolve_api_key,
    _runtime_headers,
    _sandbox_binding_response,
    _traceparent_from_header,
    _valid_jwt,
)
from app.response_store import StoredResponse, build_response_store
from app.responses import (
    ResponsesRequest,
    chat_completion_to_responses,
    responses_to_chat_payload,
)
from app.runtime_client import RuntimeClient
from app.runtime_routing import _is_failover_worthy, _open_stream_with_fallback, _schedule_shadow
from app.schemas import (
    BatchRequest,
    ChatCompletionRequest,
    CompletionRequest,
    EmbeddingsRequest,
    ModerationRequest,
)
from app.settings import (
    AdmissionPolicyError,
    Settings,
    completion_prompt_texts,
    moderate_text,
    validate_sandbox_id,
)
from app.streaming import (
    _rewrite_stream_segment,
    _terminal_stream_error_event,
    _usage_from_sse_chunk,
)
from app.tracing import configure_tracing, trace_request

_chain_audit_event = chain_audit_event
_payload_fingerprint = payload_fingerprint

AUDIT_LOGGER = logging.getLogger("ai_platform_ops_lab.audit")
SERVICE_VERSION = "0.28.0"
OPENAPI_DESCRIPTION = (
    "OpenAI-compatible private inference gateway with sandbox traceability, "
    "admission controls, budget enforcement, redacted audit events, and "
    "runtime routing to Ollama or vLLM."
)
OPENAPI_TAGS = [
    {"name": "health", "description": "Readiness and liveness checks."},
    {"name": "observability", "description": "Prometheus metrics endpoints."},
    {"name": "sandbox", "description": "Sandbox-scoped budget and trace controls."},
    {"name": "inference", "description": "OpenAI-compatible inference endpoints."},
]


# OpenAI-shaped error taxonomy: map the HTTP status the gateway returns to the
# ``error.type`` string OpenAI SDKs branch on (e.g. ``openai.RateLimitError`` keys off


def _record_token_usage(backend: str, runtime_response: dict[str, Any] | None) -> None:
    usage = (runtime_response or {}).get("usage")
    if not isinstance(usage, dict):
        return
    for token_type in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(token_type)
        if isinstance(value, (int, float)) and value >= 0:
            TOKEN_USAGE.labels(backend, token_type).inc(value)


def _record_estimated_cost(settings: Settings, sandbox_id: str, backend: str, usage: dict[str, Any] | None) -> None:
    """Increment the estimated-cost counter from runtime token usage.

    Exposes the same USD_PER_1K_TOKENS cost model used by ``/v1/usage`` as a Prometheus
    series so per-sandbox/backend spend is visualizable (FinOps/chargeback) rather than
    only readable as an ad-hoc JSON field. A zero rate leaves the cost model off.
    """
    if settings.usd_per_1k_tokens <= 0 or not isinstance(usage, dict):
        return
    total_tokens = usage.get("total_tokens")
    if not isinstance(total_tokens, (int, float)) or total_tokens < 0:
        return
    cost = (total_tokens / 1000.0) * settings.usd_per_1k_tokens
    if cost > 0:
        ESTIMATED_COST.labels(_sandbox_label(sandbox_id), backend).inc(cost)


def _record_budget_reservation(reservation: BudgetReservation | None, settings: Settings) -> None:
    if reservation is None:
        return
    limits = {
        "requests": settings.sandbox_request_budget,
        "prompt_chars": settings.sandbox_prompt_char_budget,
        "estimated_tokens": settings.sandbox_estimated_token_budget,
    }
    usage = {
        "requests": reservation.usage.requests,
        "prompt_chars": reservation.usage.prompt_chars,
        "estimated_tokens": reservation.usage.estimated_tokens,
    }
    for budget_type, value in usage.items():
        SANDBOX_BUDGET_USAGE.labels(_sandbox_label(reservation.sandbox_id), budget_type).set(value)
        SANDBOX_BUDGET_LIMIT.labels(_sandbox_label(reservation.sandbox_id), budget_type).set(limits[budget_type])


def _emit_audit_record(event: dict[str, Any]) -> None:
    """Write one already-chained receipt to the audit logger and the operator log."""
    line = json.dumps(event, sort_keys=True)
    AUDIT_LOGGER.info(line)
    logging.getLogger("uvicorn.error").info(line)


def _persist_audit_head(app: FastAPI) -> None:
    """Write the current chain head to the durable store, best effort.

    A head that cannot be persisted costs continuity for the *next* process, not this
    one, so a failure is counted and logged rather than raised: refusing to serve because
    a head store is unreachable would trade an evidentiary nicety for an outage.
    """
    state = app.state
    if state.audit_chain_count == getattr(state, "audit_head_persisted_count", None):
        return
    try:
        state.chain_store.save(
            ChainHead(
                chain_id=state.audit_chain_id,
                head=state.audit_prev_hash,
                count=state.audit_chain_count,
            )
        )
    except Exception:
        AUDIT_CHAIN_PERSIST.labels("error").inc()
        logging.getLogger("uvicorn.error").exception("audit chain head could not be persisted")
        return
    state.audit_head_persisted_count = state.audit_chain_count
    AUDIT_CHAIN_PERSIST.labels("ok").inc()


def open_audit_chain(app: FastAPI) -> dict[str, Any]:
    """Open this process's chain with a ``chain_start`` receipt naming its predecessor.

    This is the record that turns a set of independent per-process chains into a chain of
    chains. It is hash-linked like any other receipt, so the claim it makes about the
    previous chain's head is itself covered by the chain it opens, and a lifetime that was
    deleted wholesale leaves a successor pointing at a predecessor that is not there.
    """
    state = app.state
    try:
        previous = state.chain_store.load()
    except Exception:
        # An unreadable head store must not stop the gateway from serving; the chain
        # simply starts without a predecessor, which the record states plainly.
        logging.getLogger("uvicorn.error").exception("audit chain head could not be read")
        previous = None
    event = chain_start_event(state.audit_chain_id, previous)
    event["ts"] = time()
    event["prev_hash"], event["record_hash"] = advance_chain(AUDIT_GENESIS, event)
    state.audit_prev_hash = event["record_hash"]
    state.audit_chain_count = 1
    if state.settings.audit_log_enabled:
        _emit_audit_record(event)
    _persist_audit_head(app)
    return event


def _route_settings(settings: Settings, model_route: Any) -> Settings:
    """Apply a route's calibrated chars-per-token to the settings used for budgeting.

    Falls back to the gateway default when the route declares none, so a catalog that has
    not been calibrated yet behaves exactly as before rather than silently changing what
    every tenant is charged.
    """
    per_token = getattr(model_route, "estimated_chars_per_token", 0)
    if not per_token or per_token == settings.budget_estimated_chars_per_token:
        return settings
    return replace(settings, budget_estimated_chars_per_token=per_token)


async def _settle_budget(request: Request, runtime_response: dict[str, Any] | None) -> None:
    """Reconcile this request's budget reservation against the runtime's measured usage.

    Admission has to charge before the model runs, so it charges the worst case (the full
    requested completion cap). This is where that estimate is corrected to what the call
    actually consumed, so the window counter, the ``x-ratelimit-*`` headroom, and the
    usage and chargeback figures derived from them track real spend rather than the
    ceiling the caller happened to ask for.

    Two deliberate asymmetries. A settlement never fails a request that already succeeded:
    the response is committed, and a budget backend that broke afterwards must not turn a
    served call into an error. And a runtime that reported no usage leaves the reservation
    standing rather than refunding it, so a request that burns runtime capacity and then
    fails late is still charged; over-charging is recoverable, free capacity is not.
    """
    reservation = getattr(request.state, "budget_reservation_object", None)
    if reservation is None or getattr(request.state, "budget_settled", False):
        return
    # Guard before the await: the streaming and non-streaming paths both reach here, and
    # a second settlement would refund the same reservation twice.
    request.state.budget_settled = True
    tracker: SandboxBudgetTracker = request.app.state.budget_tracker
    try:
        settlement = await asyncio.to_thread(tracker.settle, reservation, (runtime_response or {}).get("usage"))
    except BudgetBackendError:
        BUDGET_SETTLEMENTS.labels("backend_unavailable").inc()
        return
    except Exception:
        # Deliberately broad: settlement runs after the response is committed, so letting
        # anything escape would turn a call the client already received into a 500. The
        # unsettled reservation stands, which can only over-charge. The traceback goes to
        # the operator log, never to the audit logger, whose every line is a parseable receipt.
        BUDGET_SETTLEMENTS.labels("error").inc()
        logging.getLogger("uvicorn.error").exception("budget settlement failed")
        return
    if settlement is None:
        BUDGET_SETTLEMENTS.labels("unreported").inc()
        return
    BUDGET_SETTLEMENTS.labels("settled").inc()
    sandbox_label = _sandbox_label(settlement.sandbox_id)
    if settlement.refunded_tokens:
        BUDGET_SETTLED_TOKENS.labels(sandbox_label, "refund").inc(settlement.refunded_tokens)
    if settlement.overrun_tokens:
        BUDGET_SETTLED_TOKENS.labels(sandbox_label, "overrun").inc(settlement.overrun_tokens)
    request.state.budget_settlement = settlement.audit_dict()
    SANDBOX_BUDGET_USAGE.labels(sandbox_label, "estimated_tokens").set(settlement.settled_estimated_tokens)


async def _settle_and_audit(
    settings: Settings,
    request: Request,
    payload: dict[str, Any],
    *,
    status_code: int,
    latency_seconds: float,
    backend: str,
    runtime_response: dict[str, Any] | None = None,
    runtime_status_code: int | None = None,
    error: str | None = None,
) -> None:
    """Settle the sandbox budget against real usage, then write the receipt recording both.

    Every governed endpoint already writes exactly one receipt per request, on the
    streaming and non-streaming paths alike, so hanging settlement off that single point
    keeps the correction and its evidence from ever diverging.
    """
    await _settle_budget(request, runtime_response)
    _write_audit_log(
        settings,
        request,
        payload,
        status_code=status_code,
        latency_seconds=latency_seconds,
        backend=backend,
        runtime_response=runtime_response,
        runtime_status_code=runtime_status_code,
        error=error,
    )


def _budget_headers(reservation: BudgetReservation | None, settings: Settings) -> dict[str, str]:
    """Build OpenAI-style ``x-ratelimit-*`` response headers from a budget reservation.

    Mirrors the OpenAI API's budget headers so agent frameworks that parse them can
    pace themselves against the sandbox budget. Remaining values are floored at zero;
    a limit of zero means unlimited and emits no headers for that dimension.
    """
    if reservation is None:
        return {}
    headers: dict[str, str] = {}
    if settings.sandbox_request_budget > 0:
        headers["x-ratelimit-limit-requests"] = str(settings.sandbox_request_budget)
        headers["x-ratelimit-remaining-requests"] = str(
            max(settings.sandbox_request_budget - reservation.usage.requests, 0)
        )
    if settings.sandbox_estimated_token_budget > 0:
        headers["x-ratelimit-limit-tokens"] = str(settings.sandbox_estimated_token_budget)
        headers["x-ratelimit-remaining-tokens"] = str(
            max(settings.sandbox_estimated_token_budget - reservation.usage.estimated_tokens, 0)
        )
    return headers


def _admission_status(reason: str, settings: Settings) -> tuple[int, dict[str, str] | None]:
    if reason.startswith("sandbox_") and reason.endswith("_exceeded"):
        headers = None
        if settings.sandbox_budget_window_seconds > 0:
            headers = {"Retry-After": str(settings.sandbox_budget_window_seconds)}
        return 429, headers
    return 400, None


def _write_audit_log(
    settings: Settings,
    request: Request,
    payload: dict[str, Any],
    status_code: int,
    latency_seconds: float,
    backend: str,
    runtime_response: dict[str, Any] | None = None,
    runtime_status_code: int | None = None,
    error: str | None = None,
) -> None:
    if not settings.audit_log_enabled:
        return
    # Agent-action receipt semantics (ADR 0009): every sandbox-bound request is an
    # action with an explicit decision, so the chain doubles as a receipt stream.
    # Denials (admission, budget, guardrail block) carry decision=denied with the
    # reason in `error`; the guardrail outcome is recorded even when allowed.
    event = {
        "event": "inference_request",
        # Per-process chain identity (hash-covered): lets the verifier group records into
        # independent per-replica chains and anchor each head. Pre-v0.23.0 events lack it.
        "chain_id": getattr(request.app.state, "audit_chain_id", None),
        "action_type": "model_call",
        "decision": "allowed" if status_code < 400 else "denied",
        "guardrail_action": getattr(request.state, "output_guardrail_action", None),
        "prompt_guardrail_action": getattr(request.state, "prompt_guardrail_action", None),
        "request_id": request.state.request_id,
        "traceparent": request.state.traceparent,
        "sandbox_id": request.state.sandbox_id,
        "principal": getattr(request.state, "principal", None),
        "backend": backend,
        "model": payload.get("model") or settings.model_id,
        "status_code": status_code,
        "runtime_status_code": runtime_status_code,
        "latency_ms": round(latency_seconds * 1000, 2),
        # Chain-covered wall-clock timestamp: keeping WHEN inside the hash chain means
        # rewriting event times is as detectable as rewriting the events themselves.
        "ts": time(),
        "usage": (runtime_response or {}).get("usage"),
        "error": error,
        "budget": getattr(request.state, "budget_reservation", None),
        # What the reservation was corrected to once the runtime reported real usage.
        # Absent when the runtime reported no usage, so a receipt never implies a
        # reconciliation that did not happen.
        "budget_settlement": getattr(request.state, "budget_settlement", None),
    }
    event.update(_payload_fingerprint(payload))
    _chain_audit_event(request, event)
    line = json.dumps(event, sort_keys=True)
    AUDIT_LOGGER.info(line)
    logging.getLogger("uvicorn.error").info(line)


def _effective_settings(request: Request, policy_set: SandboxPolicySet, settings: Settings) -> Settings:
    """Return the request's effective settings: sandbox-policy overrides, then per-key budgets.

    Composes the two override sources onto the base settings for the (already bound)
    sandbox: the SandboxPolicySet's per-sandbox admission/budget overrides first, then
    any per-key budget overrides carried by a matched API-key record. The per-key budget
    takes precedence for the three budget dimensions so a key's issued allowance is what
    the request is metered against - and what /v1/usage and /v1/sandbox/budget report.
    """
    effective = policy_set.effective_settings(settings, request.state.sandbox_id)
    key_updates = getattr(request.state, "key_budget_updates", None)
    if key_updates:
        effective = replace(effective, **key_updates)
    return effective


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


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or Settings.from_env()
    app = FastAPI(
        title="Private AI Platform Kit Inference Gateway",
        version=SERVICE_VERSION,
        description=OPENAPI_DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
        docs_url="/docs",
        redoc_url=None,
    )
    # Bound JSON bodies before Pydantic or endpoint code parses them. The Files
    # endpoint gets its own upload ceiling plus multipart framing overhead.
    app.add_middleware(
        RequestBodyLimitMiddleware,
        max_bytes=resolved.max_request_body_bytes,
        path_limits={"/v1/files": resolved.batch_max_file_bytes + 65536},
    )
    app.state.settings = resolved
    app.state.runtime_client = RuntimeClient(resolved)
    app.router.add_event_handler("shutdown", app.state.runtime_client.aclose)
    app.state.budget_tracker = build_sandbox_budget_tracker(resolved)
    app.state.rate_limiter = build_rate_limiter(resolved)
    app.state.inflight = 0
    app.state.audit_prev_hash = AUDIT_GENESIS
    app.state.audit_chain_count = 0
    # Per-process audit chain identity: pod identity, process start time, and a random
    # suffix. It is stamped onto every audit event (hash-covered) so the operator verifier
    # can group records into independent per-replica chains and anchor each head, instead
    # of guessing chain boundaries from genesis restarts in interleaved multi-replica logs.
    # The suffix is load-bearing, not decoration: a pod that restarts twice inside one
    # second (a crashloop, the case where the log matters most) would otherwise mint the
    # same id twice and the verifier would splice two unrelated chains into one and report
    # the seam as tampering.
    app.state.audit_chain_id = f"{os.getenv('HOSTNAME', 'gateway')}:{int(time())}:{uuid4().hex[:8]}"
    app.state.chain_store = build_chain_store(resolved)
    app.state.background_tasks = set()

    async def _audit_chain_startup() -> None:
        open_audit_chain(app)

        async def _persist_loop() -> None:
            # Coalesced rather than per-record: a head store write on the request path
            # would add a network hop to every governed call. The cost of the interval is
            # that a hard crash can leave the persisted head a few records behind the true
            # tail, which the verifier tolerates by checking the successor's named head
            # appears in the predecessor rather than requiring it to be the last record.
            while True:
                await asyncio.sleep(resolved.audit_chain_persist_interval_seconds)
                await asyncio.to_thread(_persist_audit_head, app)

        task = asyncio.ensure_future(_persist_loop())
        app.state.audit_persist_task = task
        app.state.background_tasks.add(task)
        task.add_done_callback(app.state.background_tasks.discard)

    async def _audit_chain_shutdown() -> None:
        task = getattr(app.state, "audit_persist_task", None)
        if task is not None:
            task.cancel()
        await asyncio.to_thread(_persist_audit_head, app)

    app.router.add_event_handler("startup", _audit_chain_startup)
    app.router.add_event_handler("shutdown", _audit_chain_shutdown)
    app.state.response_cache = build_response_cache(resolved)
    app.state.model_routing_policy = (
        ModelRoutingPolicy.from_path(resolved.model_routing_policy_path, resolved)
        if resolved.model_routing_policy_path
        else ModelRoutingPolicy.default(resolved)
    )
    app.state.sandbox_policy_set = SandboxPolicySet.from_path(resolved.sandbox_policy_path)
    # Optional richer API-key records (scopes/expiry/sandbox binding/budget). Fails closed:
    # a malformed key store raises here and stops startup rather than silently disabling
    # per-key controls. No records file -> an empty set, preserving flat-hash behavior.
    app.state.key_record_set = KeyRecordSet.from_path(resolved.api_key_records_path)
    app.state.jwt_verifier = JwtVerifier(resolved)
    # Async batch subsystem (ADR 0011): build the blob object store and the file/batch record
    # store + queue only when enabled, then register the /v1/files and /v1/batches routes. The
    # routes are always registered (stable OpenAPI) but 404 unless BATCH_API_ENABLED is set.
    app.state.object_store = build_object_store(resolved) if resolved.batch_api_enabled else None
    app.state.batch_store = build_batch_store(resolved) if resolved.batch_api_enabled else None
    register_batch_routes(app, resolved)
    # Server-side Responses state (ADR 0012): built only when enabled; the /v1/responses handler
    # rejects store / previous_response_id when it is None (the stateless subset).
    app.state.response_store = build_response_store(resolved) if resolved.responses_store_enabled else None
    # Opt-in read-only admin console (ADR 0013): the gateway serves the bundled static page at
    # /console, same-origin so its /v1 fetches need no CORS. Off by default.
    if resolved.admin_console_enabled:
        app.mount(
            "/console",
            StaticFiles(directory=os.path.join(os.path.dirname(__file__), "console"), html=True),
            name="console",
        )
    tracing = configure_tracing(resolved)
    app.state.tracer = tracing[0] if tracing else None
    app.state.tracer_provider = tracing[1] if tracing else None
    if app.state.tracer_provider is not None:
        # Flush buffered spans on termination: BatchSpanProcessor otherwise drops its
        # queued tail every time a pod stops, losing the last requests' traces.
        app.router.add_event_handler("shutdown", app.state.tracer_provider.shutdown)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        try:
            request.state.request_id = _request_id_from_header(request)
            request.state.sandbox_id = validate_sandbox_id(
                request.headers.get("x-sandbox-id", resolved.default_sandbox_id)
            )
            request.state.traceparent = _traceparent_from_header(request)
            request.state.principal = None
            # Per-key budget overrides (from a matched API-key record) folded into the
            # request's effective settings; empty for flat keys and unauthenticated paths.
            request.state.key_budget_updates = {}
        except ValueError as exc:
            # A request header failed validation. The message is a controlled, developer-authored
            # validation string describing the malformed header (e.g. the sandbox-id charset rule);
            # it carries no stack trace or server internals, so returning it as an actionable 400 is
            # correct API behavior, not information disclosure. Also logged for server-side triage.
            reason = str(exc)
            logging.getLogger("uvicorn.error").warning("rejected malformed request header: %s", reason)
            return JSONResponse(status_code=400, content={"detail": reason})

        async def dispatch() -> Response:
            if (resolved.api_key_auth_enabled or resolved.jwt_auth_enabled) and _auth_required(request.url.path):
                api_key_outcome = (
                    _resolve_api_key(request, resolved, request.app.state.key_record_set)
                    if resolved.api_key_auth_enabled
                    else ApiKeyOutcome(valid=False, record=None, expired=False)
                )
                jwt_claims: dict[str, Any] | None = None
                if not api_key_outcome.valid and resolved.jwt_auth_enabled:
                    try:
                        jwt_claims = await _valid_jwt(request, request.app.state.jwt_verifier)
                    except JwksUnavailableError:
                        # Issuer JWKS is unreachable: this is a 503 (retry later),
                        # not a 401 token rejection.
                        return _jwks_unavailable_response(request)
                if not api_key_outcome.valid and jwt_claims is None:
                    # A presented key that matched a record but is expired is a distinct,
                    # more actionable rejection than an unrecognized key - never fall
                    # through to accepting it as unbound.
                    reason = "api_key_expired" if api_key_outcome.expired else "invalid_or_missing_api_key"
                    return _auth_failure_response(request, reason)
                # Propagate the authenticated principal so the audit trail records who
                # called, not just the (client-asserted) sandbox header.
                if api_key_outcome.valid:
                    record = api_key_outcome.record
                    request.state.principal = _api_key_principal(request, resolved, record)
                    if record is not None:
                        # A record with a sandbox binding is enforced exactly like the JWT
                        # tenant claim: a mismatched X-Sandbox-ID is rejected; a missing one
                        # adopts the bound sandbox. This closes the cross-tenant read on
                        # /v1/usage and /v1/sandbox/budget for API-key callers.
                        if record.sandbox is not None:
                            explicit = request.headers.get("x-sandbox-id")
                            if explicit is not None and validate_sandbox_id(explicit) != record.sandbox:
                                return _sandbox_binding_response(request, "sandbox_identity_mismatch")
                            request.state.sandbox_id = record.sandbox
                        # Fold per-key budget overrides into the request's effective settings
                        # via the same mechanism the sandbox policy set uses.
                        if record.has_budget_override():
                            request.state.key_budget_updates = key_record_effective_budget_updates(record)
                elif jwt_claims is not None:
                    request.state.principal = _jwt_principal(jwt_claims)
                    # Bind the sandbox to the verified tenant claim when configured,
                    # so per-sandbox budget/policy/attribution cannot be spoofed via
                    # the X-Sandbox-ID header.
                    if resolved.jwt_tenant_claim:
                        try:
                            bound = _bound_sandbox_id(jwt_claims, resolved.jwt_tenant_claim)
                        except ValueError:
                            return _sandbox_binding_response(request, "sandbox_claim_invalid")
                        explicit = request.headers.get("x-sandbox-id")
                        if explicit is not None and validate_sandbox_id(explicit) != bound:
                            return _sandbox_binding_response(request, "sandbox_identity_mismatch")
                        request.state.sandbox_id = bound

            # Short-window per-sandbox throttle (distinct from the cumulative budget):
            # bounds burst abuse. Checked after sandbox binding so the limit applies to
            # the authenticated tenant, not the spoofable header. The check runs on a
            # worker thread because the Redis client is synchronous; a slow (not down)
            # Redis must not stall the whole event loop.
            if resolved.rate_limit_enabled and _auth_required(request.url.path):
                try:
                    allowed, retry_after = await asyncio.to_thread(
                        request.app.state.rate_limiter.check, request.state.sandbox_id
                    )
                except BudgetBackendError:
                    # Deliberate availability-vs-enforcement tradeoff. Fail closed by default
                    # (503, matching the budget tracker); when RATE_LIMIT_FAIL_OPEN is set,
                    # admit the request instead so a Redis outage does not take down all
                    # traffic - recorded via a warning log and a metric so the degraded
                    # window is visible. Budgets are unaffected and stay fail-closed.
                    if not resolved.rate_limit_fail_open:
                        return _rate_limit_backend_unavailable_response(request)
                    RATE_LIMIT_FAIL_OPEN.labels(_sandbox_label(request.state.sandbox_id)).inc()
                    logging.getLogger("uvicorn.error").warning(
                        "rate limit backend unavailable; failing open (RATE_LIMIT_FAIL_OPEN=1) "
                        "for sandbox %s request %s",
                        request.state.sandbox_id,
                        request.state.request_id,
                    )
                else:
                    if not allowed:
                        return _rate_limited_response(request, retry_after)

            # Bounded concurrency with fast-fail load shedding: the check + increment is
            # synchronous (no await between), so it is atomic on the event loop. Excess
            # load is rejected with 503 rather than queued behind the httpx pool.
            limited = resolved.max_concurrent_requests > 0 and _auth_required(request.url.path)
            if limited:
                if request.app.state.inflight >= resolved.max_concurrent_requests:
                    return _overloaded_response(request)
                request.app.state.inflight += 1
                INFLIGHT.set(request.app.state.inflight)
            try:
                response = await call_next(request)
            except BaseException:
                if limited:
                    request.app.state.inflight -= 1
                    INFLIGHT.set(request.app.state.inflight)
                raise
            if limited:
                # Hold the concurrency slot until the response BODY completes, not just
                # the headers: for a streaming response the expensive runtime work happens
                # while the body is on the wire, so releasing at headers time would let
                # unbounded concurrent streams pile up behind a "bounded" gateway.
                body_iterator = getattr(response, "body_iterator", None)
                if body_iterator is None:
                    request.app.state.inflight -= 1
                    INFLIGHT.set(request.app.state.inflight)
                else:

                    async def _release_when_body_done(iterator: Any = body_iterator) -> Any:
                        try:
                            async for chunk in iterator:
                                yield chunk
                        finally:
                            request.app.state.inflight -= 1
                            INFLIGHT.set(request.app.state.inflight)

                    response.body_iterator = _release_when_body_done()
            response.headers["X-Request-ID"] = request.state.request_id
            response.headers["X-Sandbox-ID"] = request.state.sandbox_id
            if request.state.traceparent:
                response.headers["traceparent"] = request.state.traceparent
            if getattr(request.state, "cache_status", None):
                response.headers["X-Cache"] = request.state.cache_status
            for header, value in getattr(request.state, "budget_headers", {}).items():
                response.headers[header] = value
            if getattr(request.state, "output_guardrail_action", None):
                response.headers["X-Output-Guardrail"] = request.state.output_guardrail_action
            if getattr(request.state, "prompt_guardrail_action", None):
                response.headers["X-Prompt-Guardrail"] = request.state.prompt_guardrail_action
            return response

        tracer = request.app.state.tracer
        if tracer is None:
            return await dispatch()
        return await trace_request(tracer, request, dispatch)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Reshape every raised HTTPException - and FastAPI's own 404/405 - into the
        # OpenAI-style error envelope while preserving the handler's headers (WWW-Authenticate,
        # Retry-After, ...). Pydantic 422 validation errors go through FastAPI's separate
        # RequestValidationError handler and are intentionally left in their default shape.
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_envelope(exc.status_code, exc.detail),
            headers=getattr(exc, "headers", None),
        )

    @app.get(
        "/healthz",
        tags=["health"],
        summary="Check gateway readiness",
        operation_id="getGatewayHealth",
    )
    async def healthz() -> dict[str, str]:
        REQUESTS.labels("/healthz", resolved.runtime_backend, "200").inc()
        return {
            "status": "ok",
            "backend": resolved.runtime_backend,
            "model": resolved.model_id,
        }

    @app.get(
        "/readyz",
        tags=["health"],
        summary="Check gateway and configured runtime readiness",
        operation_id="getGatewayReadiness",
    )
    async def readyz(response: Response) -> dict[str, Any]:
        client: RuntimeClient = app.state.runtime_client
        policy: ModelRoutingPolicy = app.state.model_routing_policy
        backends = sorted({route.backend for route in policy.routes} or {resolved.runtime_backend})
        runtime_status: dict[str, Any] = {}
        healthy_backends: set[str] = set()
        for backend in backends:
            try:
                runtime_health = await client.health(backend)
                healthy_backends.add(backend)
                runtime_status[backend] = {
                    "status": "ok",
                    "detail": runtime_health.get("status", "ok"),
                }
            except Exception:
                runtime_status[backend] = {"status": "unavailable"}
        # A model remains available when any route in its declared failover chain
        # is healthy. Do not evict a gateway pod merely because its primary is down
        # while the exact request path would successfully fail over.
        model_status: dict[str, Any] = {}
        for route in policy.routes:
            chain = policy.resolve_chain(route.model_id, resolved.model_id)
            ready_via = next((candidate.backend for candidate in chain if candidate.backend in healthy_backends), None)
            model_status[route.model_id] = {
                "status": "ok" if ready_via is not None else "unavailable",
                "ready_via": ready_via,
            }
        dependencies: dict[str, dict[str, str]] = {}

        async def redis_dependency(name: str, dependency: Any) -> bool:
            client_obj = getattr(dependency, "client", None)
            if client_obj is None:
                dependencies[name] = {"status": "ok", "backend": "memory"}
                return True
            try:
                reachable = bool(await asyncio.to_thread(client_obj.ping))
            except Exception:
                reachable = False
            dependencies[name] = {"status": "ok" if reachable else "unavailable", "backend": "redis"}
            return reachable

        dependency_ready = True
        if resolved.sandbox_budget_enabled:
            dependency_ready &= await redis_dependency("budget_store", app.state.budget_tracker)
        if resolved.responses_store_enabled:
            dependency_ready &= await redis_dependency("response_store", app.state.response_store)
        if resolved.batch_api_enabled:
            dependency_ready &= await redis_dependency("batch_store", app.state.batch_store)
            object_store = app.state.object_store
            object_probe = getattr(object_store, "ready", None)
            if callable(object_probe):
                try:
                    object_ready = bool(await asyncio.to_thread(object_probe))
                except Exception:
                    object_ready = False
                dependencies["object_store"] = {
                    "status": "ok" if object_ready else "unavailable",
                    "backend": "s3",
                }
                dependency_ready &= object_ready
            else:
                dependencies["object_store"] = {"status": "ok", "backend": "filesystem"}
        models_ready = bool(model_status) and all(status["status"] == "ok" for status in model_status.values())
        ready = models_ready and dependency_ready
        response.status_code = 200 if ready else 503
        REQUESTS.labels("/readyz", resolved.runtime_backend, str(response.status_code)).inc()
        return {
            "status": "ready" if ready else "not_ready",
            "models": policy.model_ids(),
            "model_status": model_status,
            "runtimes": runtime_status,
            "dependencies": dependencies,
        }

    @app.get(
        "/metrics",
        tags=["observability"],
        summary="Export Prometheus metrics",
        operation_id="getGatewayMetrics",
    )
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get(
        "/v1/sandbox/budget",
        tags=["sandbox"],
        summary="Get sandbox budget usage",
        operation_id="getSandboxBudget",
    )
    async def sandbox_budget(request: Request) -> dict[str, Any]:
        # request.state.sandbox_id is the caller's bound sandbox when a principal is bound
        # (JWT tenant claim or an API-key record with a sandbox): the auth middleware rejects
        # a mismatched X-Sandbox-ID and rebinds a missing one upstream, so a bound caller can
        # only ever read its own budget here. In header-trusted mode (no binding) the header is
        # honored as-is - the documented insecure default.
        tracker: SandboxBudgetTracker = request.app.state.budget_tracker
        policy_set: SandboxPolicySet = request.app.state.sandbox_policy_set
        effective = _effective_settings(request, policy_set, resolved)
        try:
            return await asyncio.to_thread(tracker.snapshot, request.state.sandbox_id, effective)
        except BudgetBackendError as exc:
            raise HTTPException(
                status_code=503,
                detail={"message": "sandbox budget backend is unavailable", "reason": "budget_backend_unavailable"},
                headers={"Retry-After": "5"},
            ) from exc

    @app.get(
        "/v1/usage",
        tags=["sandbox"],
        summary="Get sandbox usage and estimated cost",
        operation_id="getSandboxUsage",
    )
    async def sandbox_usage(request: Request) -> dict[str, Any]:
        # Per-sandbox usage plus an estimated monetary cost (the data layer an admin/usage
        # console renders). USD_PER_1K_TOKENS of 0 leaves the cost model off (cost = 0).
        # The token counts here are settled: each request's worst-case admission estimate
        # was reconciled against the runtime's reported usage, so the cost figure tracks
        # what was consumed rather than what callers reserved.
        # Like /v1/sandbox/budget, this reflects only request.state.sandbox_id, which the auth
        # middleware forces to the caller's bound sandbox for a bound principal (JWT tenant
        # claim or an API-key record with a sandbox) - a bound caller cannot read another
        # tenant's usage via X-Sandbox-ID. Header-trusted mode (no binding) is unchanged.
        tracker: SandboxBudgetTracker = request.app.state.budget_tracker
        policy_set: SandboxPolicySet = request.app.state.sandbox_policy_set
        effective = _effective_settings(request, policy_set, resolved)
        try:
            snapshot = await asyncio.to_thread(tracker.snapshot, request.state.sandbox_id, effective)
        except BudgetBackendError as exc:
            raise HTTPException(
                status_code=503,
                detail={"message": "sandbox budget backend is unavailable", "reason": "budget_backend_unavailable"},
                headers={"Retry-After": "5"},
            ) from exc
        usage = snapshot.get("usage") or {}
        estimated_tokens = usage.get("estimated_tokens", 0) if isinstance(usage, dict) else 0
        estimated_cost = round((estimated_tokens / 1000.0) * resolved.usd_per_1k_tokens, 6)
        return {
            "sandbox_id": request.state.sandbox_id,
            "usage": usage,
            "limits": snapshot.get("limits"),
            "estimated_cost": estimated_cost,
            "currency": resolved.cost_currency,
            "usd_per_1k_tokens": resolved.usd_per_1k_tokens,
        }

    @app.get(
        "/v1/models",
        tags=["inference"],
        summary="List approved private models",
        operation_id="listModels",
    )
    async def models() -> dict[str, Any]:
        REQUESTS.labels("/v1/models", resolved.runtime_backend, "200").inc()
        policy: ModelRoutingPolicy = app.state.model_routing_policy
        return {"object": "list", "data": policy.openai_models()}

    @app.post(
        "/v1/chat/completions",
        tags=["inference"],
        summary="Create a private chat completion",
        operation_id="createChatCompletion",
    )
    async def chat_completions(request: Request, payload: ChatCompletionRequest) -> dict[str, Any]:
        route = "/v1/chat/completions"
        backend = resolved.runtime_backend
        start = perf_counter()
        status = "200"
        status_code = 200
        runtime_status_code = None
        runtime_response = None
        error = None
        payload_dict = payload.model_dump(exclude_none=True)
        request.state.budget_reservation = None
        # When streaming, the response generator records metrics + audit at its own
        # end-of-stream; the outer finally must not double-record on the headers path.
        stream_owns_recording = False
        # A cache hit consumes no runtime tokens: the finally block must not re-count
        # the cached usage into the token/cost metrics (audit still records the hit).
        cache_hit = False
        try:
            policy: ModelRoutingPolicy = request.app.state.model_routing_policy
            sandbox_policies: SandboxPolicySet = request.app.state.sandbox_policy_set
            effective = _effective_settings(request, sandbox_policies, resolved)
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
            backend = model_route.backend
            payload_dict["model"] = model_route.model_id
            effective = _route_settings(effective, model_route)
            effective.validate_admission(payload_dict)
            # Redact/flag prompt secrets (non-block modes) before the payload is cached,
            # reserved, or sent - so a redacted credential is never persisted or forwarded.
            prompt_action = _apply_prompt_secret_mode(effective, payload_dict, route)
            if prompt_action:
                request.state.prompt_guardrail_action = prompt_action
            # Exact-match per-sandbox cache (non-streaming only). A hit returns the prior
            # response without a runtime call or budget reservation.
            cache_enabled = resolved.response_cache_enabled and not payload_dict.get("stream")
            cache_id = ""
            if cache_enabled:
                cache_id = cache_key(request.state.sandbox_id, payload_dict)
                cached = await asyncio.to_thread(request.app.state.response_cache.get, cache_id)
                if cached is not None:
                    CACHE_LOOKUPS.labels("hit").inc()
                    request.state.cache_status = "HIT"
                    cache_hit = True
                    # Bind for the finally block (audit), then return.
                    runtime_response = cached
                    return cached
                CACHE_LOOKUPS.labels("miss").inc()
                request.state.cache_status = "MISS"
            tracker: SandboxBudgetTracker = request.app.state.budget_tracker
            reservation = await asyncio.to_thread(tracker.reserve, request.state.sandbox_id, payload_dict, effective)
            request.state.budget_reservation = reservation.audit_dict() if reservation is not None else None
            # Keep the reservation object itself: settlement needs the reserved estimate,
            # and the audit dict above is only a projection of it.
            request.state.budget_reservation_object = reservation
            _record_budget_reservation(reservation, effective)
            request.state.budget_headers = _budget_headers(reservation, effective)
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
                backend = stream_backend
                payload_dict["model"] = used_model
                stream_owns_recording = True

                async def stream_body() -> Any:
                    stream_status = "200"
                    stream_status_code = 200
                    stream_error: str | None = None
                    usage: dict[str, Any] | None = None
                    # The streamed bytes are already committed to the wire, so the output
                    # guardrail cannot redact/block them mid-stream; instead accumulate a
                    # bounded copy and detect+flag at end-of-stream (enforce via non-stream).
                    scan_enabled = resolved.output_guardrail_enabled
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
                        latency_seconds = perf_counter() - start
                        REQUESTS.labels(route, stream_backend, stream_status).inc()
                        SANDBOX_REQUESTS.labels(
                            _sandbox_label(request.state.sandbox_id), stream_backend, stream_status
                        ).inc()
                        LATENCY.labels(route, stream_backend).observe(latency_seconds)
                        usage_response = {"usage": usage} if usage is not None else None
                        _record_token_usage(stream_backend, usage_response)
                        _record_estimated_cost(resolved, request.state.sandbox_id, stream_backend, usage)
                        if scan_enabled and scanned:
                            patterns, terms = resolved.output_findings(scanned.decode("utf-8", "ignore"))
                            if patterns or terms:
                                OUTPUT_GUARDRAIL.labels("flagged_stream", route).inc()
                        await _settle_and_audit(
                            resolved,
                            request,
                            payload_dict,
                            status_code=stream_status_code,
                            latency_seconds=latency_seconds,
                            backend=stream_backend,
                            runtime_response=usage_response,
                            error=stream_error,
                        )

                # FastAPI streams this Response object directly; the dict[str, Any] return
                # annotation describes the JSON path and drives the OpenAPI response schema.
                return StreamingResponse(stream_body(), media_type="text/event-stream")  # type: ignore[return-value]
            # Non-streaming: try each route in the chain, failing over to the next on a
            # retryable/connection error or open circuit. `backend` and the payload model
            # are updated to the route that actually served, so metrics and audit reflect it.
            last_exc: httpx.HTTPError | None = None
            for index, candidate in enumerate(chain):
                attempt = dict(payload_dict)
                attempt["model"] = candidate.model_id
                backend = candidate.backend
                try:
                    runtime_response = await client.chat_completions(
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
            if runtime_response is None:
                raise last_exc or RuntimeError("no runtime route available")
            # Inspect the completion before it is cached or returned: redact/block leaked
            # credentials, PII, or denied content (OWASP LLM02:2025/LLM05:2025). Applied pre-cache so
            # a secret is never persisted in the response cache.
            _apply_output_guardrail(runtime_response, resolved, route, request)
            if cache_enabled:
                await asyncio.to_thread(request.app.state.response_cache.set, cache_id, runtime_response)
            if shadow_route is not None:
                _schedule_shadow(client, shadow_route, payload_dict, request)
            # The finally block reads runtime_response for token-usage metrics and the
            # audit log, so it is bound above rather than returned inline.
            return runtime_response
        except AdmissionPolicyError as exc:
            status_code, headers = _admission_status(exc.reason, resolved)
            status = str(status_code)
            error = str(exc)
            ADMISSION_REJECTIONS.labels(
                exc.reason,
                backend,
                _sandbox_label(request.state.sandbox_id),
            ).inc()
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
        except BudgetBackendError as exc:
            status = "503"
            status_code = 503
            error = "sandbox budget backend is unavailable"
            raise HTTPException(
                status_code=503,
                detail={
                    "message": error,
                    "reason": "budget_backend_unavailable",
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
                headers={"Retry-After": "5"},
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = "502"
            status_code = 502
            runtime_status_code = exc.response.status_code
            error = "runtime returned an error"
            raise HTTPException(
                status_code=502,
                detail={
                    "message": error,
                    "runtime_status": exc.response.status_code,
                    "backend": backend,
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
            ) from exc
        except httpx.HTTPError as exc:
            status = "502"
            status_code = 502
            error = "runtime request failed"
            raise HTTPException(
                status_code=502,
                detail={
                    "message": error,
                    "backend": backend,
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
            ) from exc
        except ValueError as exc:
            status = "502"
            status_code = 502
            error = "runtime returned an invalid response"
            raise HTTPException(
                status_code=502,
                detail={
                    "message": error,
                    "backend": backend,
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
            ) from exc
        finally:
            # Streaming responses record at end-of-stream inside stream_body(); the
            # outer finally only records the non-streaming and error-before-headers paths.
            if not stream_owns_recording:
                REQUESTS.labels(route, backend, status).inc()
                SANDBOX_REQUESTS.labels(_sandbox_label(request.state.sandbox_id), backend, status).inc()
                latency_seconds = perf_counter() - start
                LATENCY.labels(route, backend).observe(latency_seconds)
                if not cache_hit:
                    _record_token_usage(backend, runtime_response)
                    _record_estimated_cost(
                        resolved, request.state.sandbox_id, backend, (runtime_response or {}).get("usage")
                    )
                await _settle_and_audit(
                    resolved,
                    request,
                    payload_dict,
                    status_code=status_code,
                    latency_seconds=latency_seconds,
                    backend=backend,
                    runtime_response=runtime_response,
                    runtime_status_code=runtime_status_code,
                    error=error,
                )

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
        route = "/v1/completions"
        backend = resolved.runtime_backend
        start = perf_counter()
        status = "200"
        status_code = 200
        runtime_status_code = None
        runtime_response = None
        error = None
        payload_dict = payload.model_dump(exclude_none=True)
        request.state.budget_reservation = None
        try:
            policy: ModelRoutingPolicy = request.app.state.model_routing_policy
            sandbox_policies: SandboxPolicySet = request.app.state.sandbox_policy_set
            effective = _effective_settings(request, sandbox_policies, resolved)
            try:
                model_route = policy.resolve(payload_dict.get("model"), effective.model_id)
            except ValueError as exc:
                raise AdmissionPolicyError("model_not_allowed", str(exc)) from exc
            backend = model_route.backend
            payload_dict["model"] = model_route.model_id
            effective = _route_settings(effective, model_route)
            # Legacy completions do not use the chat SSE usage/guardrail machinery. Reject
            # streaming explicitly rather than forwarding an unmetered stream.
            if payload_dict.get("stream"):
                raise AdmissionPolicyError(
                    "streaming_not_supported",
                    "streaming is not supported on /v1/completions; use /v1/chat/completions",
                )
            effective.validate_completion_admission(payload_dict)
            prompt_action = _apply_prompt_secret_mode(effective, payload_dict, route)
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
            for field in ("max_tokens", "max_completion_tokens", "n"):
                value = payload_dict.get(field)
                if value is not None:
                    budget_payload[field] = value
            tracker: SandboxBudgetTracker = request.app.state.budget_tracker
            reservation = await asyncio.to_thread(tracker.reserve, request.state.sandbox_id, budget_payload, effective)
            request.state.budget_reservation = reservation.audit_dict() if reservation is not None else None
            # Keep the reservation object itself: settlement needs the reserved estimate,
            # and the audit dict above is only a projection of it.
            request.state.budget_reservation_object = reservation
            _record_budget_reservation(reservation, effective)
            request.state.budget_headers = _budget_headers(reservation, effective)
            client: RuntimeClient = request.app.state.runtime_client
            runtime_response = await client.completions(
                payload_dict,
                headers=_runtime_headers(request),
                backend=backend,
            )
            _apply_output_guardrail(runtime_response, resolved, route, request, legacy_completion=True)
            # The finally block reads runtime_response for token-usage metrics and the audit
            # log, so it is bound above rather than returned inline.
            return runtime_response
        except AdmissionPolicyError as exc:
            status_code, headers = _admission_status(exc.reason, resolved)
            status = str(status_code)
            error = str(exc)
            ADMISSION_REJECTIONS.labels(exc.reason, backend, _sandbox_label(request.state.sandbox_id)).inc()
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
        except BudgetBackendError as exc:
            status = "503"
            status_code = 503
            error = "sandbox budget backend is unavailable"
            raise HTTPException(
                status_code=503,
                detail={
                    "message": error,
                    "reason": "budget_backend_unavailable",
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
                headers={"Retry-After": "5"},
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = "502"
            status_code = 502
            runtime_status_code = exc.response.status_code
            error = "runtime returned an error"
            raise HTTPException(
                status_code=502,
                detail={
                    "message": error,
                    "runtime_status": exc.response.status_code,
                    "backend": backend,
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
            ) from exc
        except httpx.HTTPError as exc:
            status = "502"
            status_code = 502
            error = "runtime request failed"
            raise HTTPException(
                status_code=502,
                detail={
                    "message": error,
                    "backend": backend,
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
            ) from exc
        except ValueError as exc:
            status = "502"
            status_code = 502
            error = "runtime returned an invalid response"
            raise HTTPException(
                status_code=502,
                detail={
                    "message": error,
                    "backend": backend,
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
            ) from exc
        finally:
            REQUESTS.labels(route, backend, status).inc()
            SANDBOX_REQUESTS.labels(_sandbox_label(request.state.sandbox_id), backend, status).inc()
            latency_seconds = perf_counter() - start
            LATENCY.labels(route, backend).observe(latency_seconds)
            _record_token_usage(backend, runtime_response)
            _record_estimated_cost(resolved, request.state.sandbox_id, backend, (runtime_response or {}).get("usage"))
            await _settle_and_audit(
                resolved,
                request,
                payload_dict,
                status_code=status_code,
                latency_seconds=latency_seconds,
                backend=backend,
                runtime_response=runtime_response,
                runtime_status_code=runtime_status_code,
                error=error,
            )

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
        route = "/v1/messages"
        backend = resolved.runtime_backend
        start = perf_counter()
        status = "200"
        status_code = 200
        runtime_status_code = None
        runtime_response = None
        error = None
        # Translate the Anthropic request into the internal OpenAI chat payload up front so
        # the whole governance path (admission, prompt-secret modes, budget, audit) operates
        # on the translated messages exactly as it does for chat - not a second, weaker path.
        payload_dict = anthropic_to_chat_payload(payload)
        request_model = payload.model
        request.state.budget_reservation = None
        # When streaming, the translated event stream records metrics + audit at its own
        # end-of-stream; the outer finally must not double-record on the headers path.
        stream_owns_recording = False
        try:
            policy: ModelRoutingPolicy = request.app.state.model_routing_policy
            sandbox_policies: SandboxPolicySet = request.app.state.sandbox_policy_set
            effective = _effective_settings(request, sandbox_policies, resolved)
            try:
                model_route = policy.resolve(payload_dict.get("model"), effective.model_id)
            except ValueError as exc:
                raise AdmissionPolicyError("model_not_allowed", str(exc)) from exc
            backend = model_route.backend
            payload_dict["model"] = model_route.model_id
            effective = _route_settings(effective, model_route)
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
            prompt_action = _apply_prompt_secret_mode(effective, payload_dict, route)
            if prompt_action:
                request.state.prompt_guardrail_action = prompt_action
            tracker: SandboxBudgetTracker = request.app.state.budget_tracker
            reservation = await asyncio.to_thread(tracker.reserve, request.state.sandbox_id, payload_dict, effective)
            request.state.budget_reservation = reservation.audit_dict() if reservation is not None else None
            # Keep the reservation object itself: settlement needs the reserved estimate,
            # and the audit dict above is only a projection of it.
            request.state.budget_reservation_object = reservation
            _record_budget_reservation(reservation, effective)
            request.state.budget_headers = _budget_headers(reservation, effective)
            client: RuntimeClient = request.app.state.runtime_client
            if payload_dict.get("stream"):
                # Single-route chain: /v1/messages resolves one model rather than a fallback
                # chain, so this reuses the chat opener without inventing failover behavior
                # the non-streaming path on this endpoint does not have.
                stream, stream_backend, used_model, first_chunk = await _open_stream_with_fallback(
                    client, [model_route], payload_dict, request
                )
                backend = stream_backend
                payload_dict["model"] = used_model
                stream_owns_recording = True
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
                        latency_seconds = perf_counter() - start
                        REQUESTS.labels(route, stream_backend, stream_status).inc()
                        SANDBOX_REQUESTS.labels(
                            _sandbox_label(request.state.sandbox_id), stream_backend, stream_status
                        ).inc()
                        LATENCY.labels(route, stream_backend).observe(latency_seconds)
                        stream_usage = translator.usage
                        usage_response = {"usage": stream_usage} if stream_usage is not None else None
                        _record_token_usage(stream_backend, usage_response)
                        _record_estimated_cost(resolved, request.state.sandbox_id, stream_backend, stream_usage)
                        # The streamed bytes are committed, so the guardrail detects and flags
                        # at end-of-stream (enforcement stays on the non-streaming path). The
                        # translator kept the decoded text, so the scan sees real text rather
                        # than its JSON escaping.
                        if resolved.output_guardrail_enabled and translator.scanned_text:
                            patterns, terms = resolved.output_findings(translator.scanned_text)
                            if patterns or terms:
                                OUTPUT_GUARDRAIL.labels("flagged_stream", route).inc()
                        await _settle_and_audit(
                            resolved,
                            request,
                            payload_dict,
                            status_code=stream_status_code,
                            latency_seconds=latency_seconds,
                            backend=stream_backend,
                            runtime_response=usage_response,
                            error=stream_error,
                        )

                # FastAPI streams this Response object directly; the dict[str, Any] return
                # annotation describes the JSON path and drives the OpenAPI response schema.
                return StreamingResponse(stream_body(), media_type="text/event-stream")  # type: ignore[return-value]
            runtime_response = await client.chat_completions(
                payload_dict,
                headers=_runtime_headers(request),
                backend=backend,
            )
            # The output guardrail is endpoint-independent: /v1/messages must not be a bypass
            # around the redact/block policy the chat path enforces (OWASP LLM02:2025/LLM05:2025). It
            # runs on the OpenAI-shaped completion before translation back to Anthropic.
            _apply_output_guardrail(runtime_response, resolved, route, request)
            # Translate the governed OpenAI completion back into an Anthropic Message. The
            # finally block reads runtime_response (the OpenAI shape) for token-usage metrics
            # and the audit fingerprint, so it is bound above rather than returned inline.
            return chat_completion_to_anthropic(runtime_response, request_model=request_model)
        except AdmissionPolicyError as exc:
            status_code, headers = _admission_status(exc.reason, resolved)
            status = str(status_code)
            error = str(exc)
            ADMISSION_REJECTIONS.labels(exc.reason, backend, _sandbox_label(request.state.sandbox_id)).inc()
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
        except BudgetBackendError as exc:
            status = "503"
            status_code = 503
            error = "sandbox budget backend is unavailable"
            raise HTTPException(
                status_code=503,
                detail={
                    "message": error,
                    "reason": "budget_backend_unavailable",
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
                headers={"Retry-After": "5"},
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = "502"
            status_code = 502
            runtime_status_code = exc.response.status_code
            error = "runtime returned an error"
            raise HTTPException(
                status_code=502,
                detail={
                    "message": error,
                    "runtime_status": exc.response.status_code,
                    "backend": backend,
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
            ) from exc
        except httpx.HTTPError as exc:
            status = "502"
            status_code = 502
            error = "runtime request failed"
            raise HTTPException(
                status_code=502,
                detail={
                    "message": error,
                    "backend": backend,
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
            ) from exc
        except ValueError as exc:
            status = "502"
            status_code = 502
            error = "runtime returned an invalid response"
            raise HTTPException(
                status_code=502,
                detail={
                    "message": error,
                    "backend": backend,
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
            ) from exc
        finally:
            # Streaming responses record at end-of-stream inside stream_body(); the outer
            # finally only records the non-streaming and error-before-headers paths.
            if not stream_owns_recording:
                REQUESTS.labels(route, backend, status).inc()
                SANDBOX_REQUESTS.labels(_sandbox_label(request.state.sandbox_id), backend, status).inc()
                latency_seconds = perf_counter() - start
                LATENCY.labels(route, backend).observe(latency_seconds)
                _record_token_usage(backend, runtime_response)
                _record_estimated_cost(
                    resolved, request.state.sandbox_id, backend, (runtime_response or {}).get("usage")
                )
                # Audit the translated (OpenAI-shaped) payload_dict so the fingerprint (message
                # count, roles, prompt hash) is computed the same way as chat - /v1/messages
                # traffic is attributable with the identical redacted receipt shape.
                await _settle_and_audit(
                    resolved,
                    request,
                    payload_dict,
                    status_code=status_code,
                    latency_seconds=latency_seconds,
                    backend=backend,
                    runtime_response=runtime_response,
                    runtime_status_code=runtime_status_code,
                    error=error,
                )

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
        route = "/v1/responses"
        backend = resolved.runtime_backend
        start = perf_counter()
        status = "200"
        status_code = 200
        runtime_status_code = None
        runtime_response = None
        error = None
        # Translate the Responses request into the internal OpenAI chat payload up front so
        # the whole governance path (admission, prompt-secret modes, budget, audit) operates
        # on the translated messages exactly as it does for chat - not a second, weaker path.
        payload_dict = responses_to_chat_payload(payload)
        request_model = payload.model
        request.state.budget_reservation = None
        try:
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
                payload_dict = responses_to_chat_payload(payload, base_messages=prior.messages)
            policy: ModelRoutingPolicy = request.app.state.model_routing_policy
            sandbox_policies: SandboxPolicySet = request.app.state.sandbox_policy_set
            effective = _effective_settings(request, sandbox_policies, resolved)
            try:
                model_route = policy.resolve(payload_dict.get("model"), effective.model_id)
            except ValueError as exc:
                raise AdmissionPolicyError("model_not_allowed", str(exc)) from exc
            backend = model_route.backend
            payload_dict["model"] = model_route.model_id
            effective = _route_settings(effective, model_route)
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
            prompt_action = _apply_prompt_secret_mode(effective, payload_dict, route)
            if prompt_action:
                request.state.prompt_guardrail_action = prompt_action
            tracker: SandboxBudgetTracker = request.app.state.budget_tracker
            reservation = await asyncio.to_thread(tracker.reserve, request.state.sandbox_id, payload_dict, effective)
            request.state.budget_reservation = reservation.audit_dict() if reservation is not None else None
            # Keep the reservation object itself: settlement needs the reserved estimate,
            # and the audit dict above is only a projection of it.
            request.state.budget_reservation_object = reservation
            _record_budget_reservation(reservation, effective)
            request.state.budget_headers = _budget_headers(reservation, effective)
            client: RuntimeClient = request.app.state.runtime_client
            runtime_response = await client.chat_completions(
                payload_dict,
                headers=_runtime_headers(request),
                backend=backend,
            )
            # The output guardrail is endpoint-independent: /v1/responses must not be a bypass
            # around the redact/block policy the chat path enforces (OWASP LLM02:2025/LLM05:2025). It
            # runs on the OpenAI-shaped completion before translation to the Responses shape.
            _apply_output_guardrail(runtime_response, resolved, route, request)
            # Translate the governed OpenAI completion into a Responses object. The finally
            # block reads runtime_response (the OpenAI shape) for token-usage metrics and the
            # audit fingerprint, so it is bound above rather than returned inline.
            responses_body = chat_completion_to_responses(runtime_response, request_model=request_model)
            # Persist when the caller asked to store it (and the store is enabled), so it can be
            # retrieved and chained via previous_response_id (ADR 0012).
            if payload.store and response_store is not None:
                responses_body = _persist_response(
                    response_store, request, payload, payload_dict, runtime_response, responses_body
                )
            return responses_body
        except AdmissionPolicyError as exc:
            status_code, headers = _admission_status(exc.reason, resolved)
            status = str(status_code)
            error = str(exc)
            ADMISSION_REJECTIONS.labels(exc.reason, backend, _sandbox_label(request.state.sandbox_id)).inc()
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
        except BudgetBackendError as exc:
            status = "503"
            status_code = 503
            error = "sandbox budget backend is unavailable"
            raise HTTPException(
                status_code=503,
                detail={
                    "message": error,
                    "reason": "budget_backend_unavailable",
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
                headers={"Retry-After": "5"},
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = "502"
            status_code = 502
            runtime_status_code = exc.response.status_code
            error = "runtime returned an error"
            raise HTTPException(
                status_code=502,
                detail={
                    "message": error,
                    "runtime_status": exc.response.status_code,
                    "backend": backend,
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
            ) from exc
        except httpx.HTTPError as exc:
            status = "502"
            status_code = 502
            error = "runtime request failed"
            raise HTTPException(
                status_code=502,
                detail={
                    "message": error,
                    "backend": backend,
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
            ) from exc
        except ValueError as exc:
            status = "502"
            status_code = 502
            error = "runtime returned an invalid response"
            raise HTTPException(
                status_code=502,
                detail={
                    "message": error,
                    "backend": backend,
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
            ) from exc
        finally:
            REQUESTS.labels(route, backend, status).inc()
            SANDBOX_REQUESTS.labels(_sandbox_label(request.state.sandbox_id), backend, status).inc()
            latency_seconds = perf_counter() - start
            LATENCY.labels(route, backend).observe(latency_seconds)
            _record_token_usage(backend, runtime_response)
            _record_estimated_cost(resolved, request.state.sandbox_id, backend, (runtime_response or {}).get("usage"))
            # Audit the translated (OpenAI-shaped) payload_dict so the fingerprint (message
            # count, roles, prompt hash) is computed the same way as chat - /v1/responses
            # traffic is attributable with the identical redacted receipt shape.
            await _settle_and_audit(
                resolved,
                request,
                payload_dict,
                status_code=status_code,
                latency_seconds=latency_seconds,
                backend=backend,
                runtime_response=runtime_response,
                runtime_status_code=runtime_status_code,
                error=error,
            )

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

    @app.post(
        "/v1/embeddings",
        tags=["inference"],
        summary="Create private embeddings",
        operation_id="createEmbeddings",
    )
    async def embeddings(request: Request, payload: EmbeddingsRequest) -> dict[str, Any]:
        route = "/v1/embeddings"
        backend = resolved.runtime_backend
        start = perf_counter()
        status = "200"
        status_code = 200
        runtime_status_code = None
        runtime_response = None
        error = None
        payload_dict = payload.model_dump(exclude_none=True)
        request.state.budget_reservation = None
        try:
            policy: ModelRoutingPolicy = request.app.state.model_routing_policy
            sandbox_policies: SandboxPolicySet = request.app.state.sandbox_policy_set
            effective = _effective_settings(request, sandbox_policies, resolved)
            try:
                model_route = policy.resolve(payload_dict.get("model"), effective.model_id)
            except ValueError as exc:
                raise AdmissionPolicyError("model_not_allowed", str(exc)) from exc
            backend = model_route.backend
            payload_dict["model"] = model_route.model_id
            effective = _route_settings(effective, model_route)
            effective.validate_embedding_admission(payload_dict)
            prompt_action = _apply_prompt_secret_mode(effective, payload_dict, route)
            if prompt_action:
                request.state.prompt_guardrail_action = prompt_action
            # Count embedding inputs against the sandbox budget the same way prompts are.
            raw_input = payload_dict.get("input")
            texts = raw_input if isinstance(raw_input, list) else [raw_input]
            budget_payload = {"messages": [{"content": str(text)} for text in texts], "max_tokens": 0}
            tracker: SandboxBudgetTracker = request.app.state.budget_tracker
            reservation = await asyncio.to_thread(tracker.reserve, request.state.sandbox_id, budget_payload, effective)
            request.state.budget_reservation = reservation.audit_dict() if reservation is not None else None
            # Keep the reservation object itself: settlement needs the reserved estimate,
            # and the audit dict above is only a projection of it.
            request.state.budget_reservation_object = reservation
            _record_budget_reservation(reservation, effective)
            request.state.budget_headers = _budget_headers(reservation, effective)
            client: RuntimeClient = request.app.state.runtime_client
            runtime_response = await client.embeddings(
                payload_dict,
                headers=_runtime_headers(request),
                backend=backend,
            )
            # Bound before returning: the finally block reads runtime_response for
            # token usage and the audit log, so this assignment is not redundant.
            return runtime_response  # noqa: RET504
        except AdmissionPolicyError as exc:
            status_code, headers = _admission_status(exc.reason, resolved)
            status = str(status_code)
            error = str(exc)
            ADMISSION_REJECTIONS.labels(exc.reason, backend, _sandbox_label(request.state.sandbox_id)).inc()
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
        except BudgetBackendError as exc:
            status = "503"
            status_code = 503
            error = "sandbox budget backend is unavailable"
            raise HTTPException(
                status_code=503,
                detail={
                    "message": error,
                    "reason": "budget_backend_unavailable",
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
                headers={"Retry-After": "5"},
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = "502"
            status_code = 502
            runtime_status_code = exc.response.status_code
            error = "runtime returned an error"
            raise HTTPException(
                status_code=502,
                detail={
                    "message": error,
                    "runtime_status": exc.response.status_code,
                    "backend": backend,
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
            ) from exc
        except httpx.HTTPError as exc:
            status = "502"
            status_code = 502
            error = "runtime request failed"
            raise HTTPException(
                status_code=502,
                detail={
                    "message": error,
                    "backend": backend,
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
            ) from exc
        except ValueError as exc:
            status = "502"
            status_code = 502
            error = "runtime returned an invalid response"
            raise HTTPException(
                status_code=502,
                detail={
                    "message": error,
                    "backend": backend,
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
            ) from exc
        finally:
            REQUESTS.labels(route, backend, status).inc()
            SANDBOX_REQUESTS.labels(_sandbox_label(request.state.sandbox_id), backend, status).inc()
            latency_seconds = perf_counter() - start
            LATENCY.labels(route, backend).observe(latency_seconds)
            _record_token_usage(backend, runtime_response)
            _record_estimated_cost(resolved, request.state.sandbox_id, backend, (runtime_response or {}).get("usage"))
            await _settle_and_audit(
                resolved,
                request,
                payload_dict,
                status_code=status_code,
                latency_seconds=latency_seconds,
                backend=backend,
                runtime_response=runtime_response,
                runtime_status_code=runtime_status_code,
                error=error,
            )

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
            if total_chars > resolved.max_prompt_chars:
                raise AdmissionPolicyError(
                    "input_too_large",
                    f"moderations input has {total_chars} characters; limit is {resolved.max_prompt_chars}",
                )
            results = [moderate_text(text, resolved) for text in texts]
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
            REQUESTS.labels(route, resolved.runtime_backend, status).inc()
            SANDBOX_REQUESTS.labels(_sandbox_label(request.state.sandbox_id), resolved.runtime_backend, status).inc()
            latency_seconds = perf_counter() - start
            LATENCY.labels(route, resolved.runtime_backend).observe(latency_seconds)
            await _settle_and_audit(
                resolved,
                request,
                payload_dict,
                status_code=status_code,
                latency_seconds=latency_seconds,
                backend=resolved.runtime_backend,
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
            if len(payload.requests) > resolved.max_batch_requests:
                raise AdmissionPolicyError(
                    "batch_too_large",
                    f"batch has {len(payload.requests)} requests; limit is {resolved.max_batch_requests}",
                )
            policy: ModelRoutingPolicy = request.app.state.model_routing_policy
            sandbox_policies: SandboxPolicySet = request.app.state.sandbox_policy_set
            effective = _effective_settings(request, sandbox_policies, resolved)
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
                entry.update(_payload_fingerprint(item_dict))
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
                        _apply_output_guardrail(response, resolved, route, request)
                        _record_token_usage(model_route.backend, response)
                        _record_estimated_cost(
                            resolved,
                            request.state.sandbox_id,
                            model_route.backend,
                            response.get("usage") if isinstance(response, dict) else None,
                        )
                        _audit_item(index, 200, item_dict, item_prompt_action)
                        return {"index": index, "status_code": 200, "response": response}
                    except AdmissionPolicyError as exc:
                        item_code, _ = _admission_status(exc.reason, resolved)
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
            status_code, headers = _admission_status(exc.reason, resolved)
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
            REQUESTS.labels(route, resolved.runtime_backend, status).inc()
            SANDBOX_REQUESTS.labels(_sandbox_label(request.state.sandbox_id), resolved.runtime_backend, status).inc()
            latency_seconds = perf_counter() - start
            LATENCY.labels(route, resolved.runtime_backend).observe(latency_seconds)
            if resolved.audit_log_enabled:
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
                _chain_audit_event(request, event)
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

    @app.post(
        "/v1/receipts",
        tags=["sandbox"],
        summary="Record an agent action on the audit chain",
        description=(
            "Agent-action receipt intake (ADR 0014). Records what a workspace did beyond "
            "calling a model (denied egress, tool execution, file writes, credential "
            "requests) on the SAME tamper-evident hash chain as model calls, so "
            "'make audit-verify' checks them together. The action_type vocabulary is closed; "
            "an unknown value is rejected. This endpoint RECORDS claims and enforces nothing: "
            "a receipt reporting a denial is evidence that some other control denied the "
            "action, never the control itself, and submitting one permits nothing. The sandbox "
            "on the receipt is the caller's bound sandbox, so a workspace can only add to its "
            "own history. Off by default (AGENT_RECEIPTS_ENABLED)."
        ),
        operation_id="createAgentReceipt",
    )
    async def create_receipt(request: Request, payload: ReceiptRequest) -> dict[str, Any]:
        route = "/v1/receipts"
        status = "200"
        try:
            if not resolved.agent_receipts_enabled:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "message": "agent-action receipts are not enabled",
                        "reason": "receipts_not_enabled",
                        "request_id": request.state.request_id,
                        "sandbox_id": request.state.sandbox_id,
                    },
                )
            # request.state.sandbox_id is already forced to the caller's bound sandbox by
            # the auth middleware. Rejecting a contradicting body value rather than quietly
            # overwriting it means a mis-configured producer fails loudly instead of filing
            # its receipts under the wrong tenant.
            if payload.sandbox_id is not None and payload.sandbox_id != request.state.sandbox_id:
                status = "403"
                AGENT_RECEIPTS.labels("rejected", "sandbox_identity_mismatch").inc()
                raise HTTPException(
                    status_code=403,
                    detail={
                        "message": "receipt sandbox_id does not match the caller's bound sandbox",
                        "reason": "sandbox_identity_mismatch",
                        "request_id": request.state.request_id,
                        "sandbox_id": request.state.sandbox_id,
                    },
                )
            validate_receipt(payload, resolved)
            event = build_receipt_event(payload, resolved, sandbox_id=request.state.sandbox_id)
            event["chain_id"] = getattr(request.app.state, "audit_chain_id", None)
            event["request_id"] = request.state.request_id
            event["traceparent"] = request.state.traceparent
            event["principal"] = getattr(request.state, "principal", None)
            event["ts"] = time()
            chain_audit_event(request, event)
            _emit_audit_record(event)
            AGENT_RECEIPTS.labels(payload.action_type, payload.decision).inc()
            return {
                "recorded": True,
                "chain_id": event["chain_id"],
                "record_hash": event["record_hash"],
                "request_id": request.state.request_id,
                "sandbox_id": request.state.sandbox_id,
            }
        except AdmissionPolicyError as exc:
            status = "400"
            AGENT_RECEIPTS.labels("rejected", exc.reason).inc()
            raise HTTPException(
                status_code=400,
                detail={
                    "message": str(exc),
                    "reason": exc.reason,
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
            ) from exc
        finally:
            REQUESTS.labels(route, "gateway", status).inc()

    _install_openapi_contract(app, resolved)
    return app


app = create_app()
