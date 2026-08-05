"""OpenAI-compatible inference gateway with auth, admission, budgets, and runtime routing."""

import asyncio
import logging
import os
from time import time
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.audit import (
    AUDIT_GENESIS,
    build_chain_store,
    open_audit_chain,
    persist_audit_head,
)
from app.batch_api import register_batch_routes
from app.batchstore import build_batch_store
from app.body_limit import RequestBodyLimitMiddleware
from app.budget import (
    BudgetBackendError,
    build_sandbox_budget_tracker,
)
from app.cache import build_response_cache
from app.inference_api import register_inference_routes
from app.jwt_auth import JwksUnavailableError, JwtVerifier
from app.key_records import KeyRecordSet, key_record_effective_budget_updates
from app.messages_api import register_messages_routes
from app.metrics import (
    INFLIGHT,
    RATE_LIMIT_FAIL_OPEN,
    REQUESTS,
)
from app.metrics import (
    sandbox_label as _sandbox_label,
)
from app.objectstore import build_object_store
from app.policy import ModelRoutingPolicy, SandboxPolicySet
from app.ratelimit import build_rate_limiter
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
    _sandbox_binding_response,
    _traceparent_from_header,
    _valid_jwt,
)
from app.response_store import build_response_store
from app.responses_api import register_responses_routes
from app.runtime_client import RuntimeClient
from app.sandbox_api import register_sandbox_routes
from app.settings import (
    Settings,
    validate_sandbox_id,
)
from app.tracing import configure_tracing, trace_request

SERVICE_VERSION = "0.28.1"
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
                await asyncio.to_thread(persist_audit_head, app)

        task = asyncio.ensure_future(_persist_loop())
        app.state.audit_persist_task = task
        app.state.background_tasks.add(task)
        task.add_done_callback(app.state.background_tasks.discard)

    async def _audit_chain_shutdown() -> None:
        task = getattr(app.state, "audit_persist_task", None)
        if task is not None:
            task.cancel()
        await asyncio.to_thread(persist_audit_head, app)

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

    register_sandbox_routes(app, resolved)
    register_inference_routes(app, resolved)
    register_messages_routes(app, resolved)
    register_responses_routes(app, resolved)

    _install_openapi_contract(app, resolved)
    return app


app = create_app()
