"""FastAPI RAG service exposing traceable, auth-gated knowledge retrieval endpoints."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter, time
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field

from app.audit import (
    AUDIT_GENESIS,
    ChainHead,
    advance_chain,
    build_chain_store,
    chain_audit_event,
    chain_start_event,
)
from app.body_limit import RequestBodyLimitMiddleware
from app.embeddings import build_embedding_provider
from app.jwt_auth import JwksUnavailableError, JwtAuthError, JwtVerifier
from app.reranker import build_reranker_provider
from app.retriever import LexicalRetriever, QdrantRetriever, VectorStoreError, build_context
from app.settings import Settings, validate_sandbox_id
from app.tracing import configure_tracing, trace_request

AUDIT_LOGGER = logging.getLogger("ai_platform_ops_lab.rag.audit")
TRACEPARENT_PATTERN = re.compile(r"^[\da-f]{2}-[\da-f]{32}-[\da-f]{16}-[\da-f]{2}$")
SERVICE_VERSION = "0.28.1"
OPENAPI_DESCRIPTION = (
    "Private retrieval service for platform and customer knowledge. The service "
    "returns traceable retrieval results, optional context blocks, and "
    "OpenAI-compatible grounded messages for downstream chat-completion calls."
)
OPENAPI_TAGS = [
    {"name": "health", "description": "Readiness and liveness checks."},
    {"name": "observability", "description": "Prometheus metrics endpoints."},
    {"name": "retrieval", "description": "Knowledge document and RAG query endpoints."},
]

REQUESTS = Counter(
    "rag_service_requests_total",
    "Total RAG service requests by route and status.",
    ["route", "status"],
)
LATENCY = Histogram(
    "rag_service_request_duration_seconds",
    "RAG service request latency by route.",
    ["route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
RETRIEVAL_RESULTS = Histogram(
    "rag_service_retrieval_results",
    "Number of retrieved documents by sandbox.",
    ["sandbox"],
    buckets=(0, 1, 2, 3, 5, 8, 13),
)
AUTH_FAILURES = Counter(
    "rag_service_auth_failures_total",
    "Total RAG service authentication failures by route and reason.",
    ["route", "reason"],
)


class RagQueryRequest(BaseModel):
    """Request body for a RAG retrieval query with context and message options."""

    query: str = Field(..., min_length=1)
    top_k: int | None = None
    include_context: bool = True
    include_messages: bool = True
    max_context_chars: int | None = None


def _request_id_from_header(request: Request) -> str:
    """Return a validated X-Request-ID header value, generating a UUID when absent."""
    request_id = request.headers.get("x-request-id", "").strip()
    if not request_id:
        return str(uuid4())
    # Visible ASCII only: the value is echoed into the X-Request-ID response header,
    # where control bytes would be rejected at write time (an unhandled 500).
    if len(request_id) > 128 or any(not (33 <= ord(char) <= 126) for char in request_id):
        raise ValueError("X-Request-ID must be 1-128 visible ASCII characters without spaces")
    return request_id


def _traceparent_from_header(request: Request) -> str | None:
    """Return the validated W3C ``traceparent`` header, or None when not provided."""
    traceparent = request.headers.get("traceparent")
    if traceparent is None:
        return None
    traceparent = traceparent.strip().lower()
    if not TRACEPARENT_PATTERN.fullmatch(traceparent):
        raise ValueError("traceparent must use W3C trace context format")
    return traceparent


def _hash_query(query: str) -> str:
    """Return the SHA-256 hex digest of the query for redacted audit logging."""
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _auth_required(path: str) -> bool:
    """Return whether the given request path requires authentication."""
    return path not in {"/healthz", "/readyz", "/metrics", "/docs", "/openapi.json"}


def _install_openapi_contract(app: FastAPI, settings: Settings) -> None:
    """Attach bearer/API-key security schemes to the app's generated OpenAPI schema."""
    default_openapi = app.openapi

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = default_openapi()
        components = schema.setdefault("components", {})
        security_schemes = components.setdefault("securitySchemes", {})
        security_schemes["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "description": "Bearer token accepted by RAG middleware when API key authentication is enabled.",
        }
        security_schemes["ApiKeyAuth"] = {
            "type": "apiKey",
            "in": "header",
            "name": settings.api_key_header,
            "description": "API key header accepted by RAG middleware when API key authentication is enabled.",
        }
        for path, operations in schema.get("paths", {}).items():
            if not _auth_required(path):
                continue
            for method, operation in operations.items():
                if method.lower() in {"get", "post", "put", "patch", "delete"}:
                    operation["security"] = [{"BearerAuth": []}, {"ApiKeyAuth": []}]
        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


def _api_key_from_request(request: Request, settings: Settings) -> str | None:
    """Extract the API key from the bearer token or configured API-key header."""
    authorization = request.headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            return token
    api_key = request.headers.get(settings.api_key_header)
    if api_key:
        return api_key.strip()
    return None


def _valid_api_key(request: Request, settings: Settings) -> bool:
    """Return whether the request carries an API key matching a configured digest."""
    api_key = _api_key_from_request(request, settings)
    if not api_key:
        return False
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    return any(hmac.compare_digest(digest, expected) for expected in settings.api_key_sha256s)


def _auth_failure_response(request: Request, reason: str) -> JSONResponse:
    """Record the failure metric and build the 401 response with trace headers."""
    AUTH_FAILURES.labels(request.url.path, reason).inc()
    response = JSONResponse(
        status_code=401,
        content={
            "detail": {
                "message": "authentication required",
                "reason": reason,
                "request_id": request.state.request_id,
                "sandbox_id": request.state.sandbox_id,
            }
        },
    )
    response.headers["WWW-Authenticate"] = "Bearer"
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["X-Sandbox-ID"] = request.state.sandbox_id
    if request.state.traceparent:
        response.headers["traceparent"] = request.state.traceparent
    return response


def _jwks_unavailable_response(request: Request) -> JSONResponse:
    """Build a 503 response when the JWKS issuer is unreachable (not a token rejection).

    An unreachable issuer with no cached keys is an operational failure to retry, distinct
    from a rejected token (401); returning 503 lets the caller back off instead of treating
    a transient IdP outage as an auth denial.
    """
    AUTH_FAILURES.labels(request.url.path, "jwks_unavailable").inc()
    response = JSONResponse(
        status_code=503,
        content={
            "detail": {
                "message": "authentication is temporarily unavailable",
                "reason": "jwks_unavailable",
                "request_id": request.state.request_id,
                "sandbox_id": request.state.sandbox_id,
            }
        },
        headers={"Retry-After": "5"},
    )
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["X-Sandbox-ID"] = request.state.sandbox_id
    if request.state.traceparent:
        response.headers["traceparent"] = request.state.traceparent
    return response


def _sandbox_binding_response(request: Request, reason: str) -> JSONResponse:
    """Build a 403 when the verified JWT tenant claim is missing/invalid or contradicts the header.

    A verified token that names a tenant is authoritative: a caller that presents such a token
    but also asserts a different ``X-Sandbox-ID`` is not authorized for that identity, so the
    request is rejected rather than silently honoring either value.
    """
    AUTH_FAILURES.labels(request.url.path, reason).inc()
    response = JSONResponse(
        status_code=403,
        content={
            "detail": {
                "message": "sandbox identity is not authorized for this caller",
                "reason": reason,
                "request_id": request.state.request_id,
                "sandbox_id": request.state.sandbox_id,
            }
        },
    )
    response.headers["X-Request-ID"] = request.state.request_id
    response.headers["X-Sandbox-ID"] = request.state.sandbox_id
    if request.state.traceparent:
        response.headers["traceparent"] = request.state.traceparent
    return response


def _bearer_token_from_request(request: Request) -> str | None:
    """Return the bearer token from the Authorization header when present and well-formed.

    Only a three-segment ``Bearer <jwt>`` value is treated as a candidate token; anything
    else (an API key presented as a bearer, a malformed value) yields None so it is handled
    as "no JWT presented" rather than a verification failure.
    """
    authorization = request.headers.get("authorization", "").strip()
    if not authorization.lower().startswith("bearer "):
        return None
    token = authorization[7:].strip()
    if not token or token.count(".") != 2:
        return None
    return token


async def _apply_jwt_identity(request: Request, settings: Settings, verifier: JwtVerifier) -> JSONResponse | None:
    """Verify the bearer JWT and bind the request's tenant to the verified claim.

    Returns a JSONResponse to short-circuit the request (401/403/503), or None to proceed.

    Security contract (mirrors the inference gateway's ``jwt_auth`` wiring):

    - A verified token derives the tenant from ``jwt_tenant_claim``; the request's
      ``sandbox_id`` is set to that verified value and marked as an explicit assertion, so
      the tenant-isolation filter scopes retrieval to the tenant the *claim* names, never
      the client-supplied header. A missing/empty claim -> 403.
    - A verified token whose tenant contradicts an explicit ``X-Sandbox-ID`` header -> 403
      (the header cannot override the verified identity).
    - An *absent* token: fail closed with 401 when ``jwt_required``; otherwise fall through
      to the existing header-trust behavior (backward compatible) by returning None.
    - A *present but invalid* token is ALWAYS rejected with 401, regardless of
      ``jwt_required`` - it is never downgraded to header-trust, or a caller could present a
      forged token alongside a spoofed ``X-Sandbox-ID`` and read another tenant's corpus.
    - JWKS unreachable with no cached keys -> 503 (operational, not an auth denial).
    """
    token = _bearer_token_from_request(request)
    if token is None:
        # No token presented: honor jwt_required, else fall through to header-trust.
        if settings.jwt_required:
            return _auth_failure_response(request, "invalid_or_missing_jwt")
        return None
    # A token WAS presented, so it must verify. A present-but-invalid token is rejected
    # here and never falls through to the header-trust path below.
    try:
        claims = await verifier.verify(token)
    except JwksUnavailableError:
        return _jwks_unavailable_response(request)
    except JwtAuthError:
        return _auth_failure_response(request, "invalid_or_missing_jwt")
    try:
        bound = validate_sandbox_id(verifier.tenant_from_claims(claims))
    except (JwtAuthError, ValueError):
        return _sandbox_binding_response(request, "sandbox_claim_invalid")
    explicit = request.headers.get("x-sandbox-id")
    if explicit is not None and validate_sandbox_id(explicit) != bound:
        return _sandbox_binding_response(request, "sandbox_identity_mismatch")
    # Bind the tenant to the verified claim. Marking it as an explicit assertion means the
    # tenant-isolation filter (which only trusts an explicitly-asserted tenant) scopes
    # retrieval to the verified tenant instead of failing closed on the server default.
    request.state.sandbox_id = bound
    request.state.sandbox_id_from_header = True
    return None


def _emit_audit_record(event: dict[str, Any]) -> None:
    """Write one already-chained receipt to the audit logger and the operator log."""
    line = json.dumps(event, sort_keys=True)
    AUDIT_LOGGER.info(line)
    logging.getLogger("uvicorn.error").info(line)


def _persist_audit_head(app: FastAPI) -> None:
    """Write the current chain head to the durable store, best effort.

    A head that cannot be persisted costs continuity for the next process, not this one,
    so a failure is logged rather than raised: retrieval must not stop because a head
    store is unreachable.
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
        logging.getLogger("uvicorn.error").exception("audit chain head could not be persisted")
        return
    state.audit_head_persisted_count = state.audit_chain_count


def open_audit_chain(app: FastAPI) -> dict[str, Any]:
    """Open this process's chain with a ``chain_start`` receipt naming its predecessor."""
    state = app.state
    try:
        previous = state.chain_store.load()
    except Exception:
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


def _write_audit_log(
    settings: Settings,
    request: Request,
    route: str,
    status_code: int,
    latency_seconds: float,
    query: str | None = None,
    result_ids: list[str] | None = None,
    error: str | None = None,
) -> None:
    """Emit a redacted, hash-chained retrieval receipt when auditing is enabled.

    Retrieval is the event that decides what tenant data an agent was shown, and it is the
    main way untrusted content reaches a model through this platform. A plain log line
    records that; a chained receipt lets an auditor prove the record was not edited or
    dropped afterwards. The chain uses the same primitives and the same verifier as the
    gateway's model-call receipts, and ``request_id``/``traceparent`` are carried on both,
    so a retrieval can be tied to the completion it fed across two independent chains.
    """
    if not settings.audit_log_enabled:
        return
    event: dict[str, Any] = {
        "event": "rag_query",
        "chain_id": getattr(request.app.state, "audit_chain_id", None),
        "action_type": "retrieval",
        "decision": "allowed" if status_code < 400 else "denied",
        "route": route,
        "request_id": request.state.request_id,
        "traceparent": request.state.traceparent,
        "sandbox_id": request.state.sandbox_id,
        "status_code": status_code,
        "latency_ms": round(latency_seconds * 1000, 2),
        "result_ids": result_ids or [],
        # Chain-covered wall-clock timestamp: rewriting when a retrieval happened is then
        # as detectable as rewriting that it happened.
        "ts": time(),
        "error": error,
    }
    if query is not None:
        event["query_chars"] = len(query)
        event["query_sha256"] = _hash_query(query)
    chain_audit_event(request, event)
    _emit_audit_record(event)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Eagerly bootstrap the vector store at startup; close pooled clients at shutdown.

    Bootstrapping is best-effort: a failure is recorded on the retriever (and
    reflected by ``/readyz``) rather than crashing startup, avoiding a cold-start
    thundering herd where every concurrent first request races to bootstrap.

    At shutdown the retriever and its embedding/reranker providers each close their
    shared ``httpx.AsyncClient`` (mirroring the gateway's runtime-client shutdown)
    so pooled connections are not leaked.
    """
    open_audit_chain(app)

    async def _persist_loop() -> None:
        # Coalesced rather than per-record, so a head store write never sits on the
        # retrieval path. See the gateway's equivalent for the tradeoff this makes.
        while True:
            await asyncio.sleep(app.state.settings.audit_chain_persist_interval_seconds)
            await asyncio.to_thread(_persist_audit_head, app)

    persist_task = asyncio.ensure_future(_persist_loop())
    bootstrap = getattr(app.state.retriever, "bootstrap", None)
    if callable(bootstrap):
        try:
            await bootstrap()
        except VectorStoreError:
            logging.getLogger("uvicorn.error").warning(
                "vector store bootstrap failed at startup; readiness will report not_ready until it recovers"
            )
    yield
    persist_task.cancel()
    await asyncio.to_thread(_persist_audit_head, app)
    retriever = app.state.retriever
    for dependency in (
        retriever,
        getattr(retriever, "embedding_provider", None),
        getattr(retriever, "reranker_provider", None),
    ):
        aclose = getattr(dependency, "aclose", None)
        if callable(aclose):
            await aclose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the RAG service FastAPI application with its retriever."""
    resolved = settings or Settings.from_env()
    app = FastAPI(
        title="Private AI Platform Kit RAG Service",
        version=SERVICE_VERSION,
        description=OPENAPI_DESCRIPTION,
        openapi_tags=OPENAPI_TAGS,
        docs_url="/docs",
        redoc_url=None,
        lifespan=_lifespan,
    )
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=resolved.max_request_body_bytes)
    app.state.settings = resolved
    app.state.audit_prev_hash = AUDIT_GENESIS
    app.state.audit_chain_count = 0
    # Per-process chain identity, matching the gateway's format. The random suffix keeps
    # two restarts inside the same second from minting the same id and having the verifier
    # splice two unrelated chains together.
    app.state.audit_chain_id = f"{os.getenv('HOSTNAME', 'rag-service')}:{int(time())}:{uuid4().hex[:8]}"
    app.state.chain_store = build_chain_store(resolved)
    if resolved.retrieval_backend == "qdrant":
        embedding_provider = build_embedding_provider(
            resolved.embedding_provider,
            resolved.vector_dimensions,
            resolved.embedding_model,
            resolved.embedding_base_url,
            resolved.vector_timeout_seconds,
        )
        reranker_provider = build_reranker_provider(
            resolved.reranker_provider,
            resolved.reranker_base_url,
            resolved.reranker_model,
            resolved.reranker_timeout_seconds,
        )
        app.state.retriever = QdrantRetriever.from_directory(
            resolved.document_dir,
            resolved.vector_store_url,
            resolved.vector_collection,
            resolved.vector_collection_version,
            resolved.vector_timeout_seconds,
            resolved.vector_dimensions,
            resolved.vector_bootstrap_enabled,
            embedding_provider,
            candidate_multiplier=resolved.retrieval_candidate_multiplier,
            lexical_weight=resolved.retrieval_lexical_weight,
            allowed_classifications=resolved.retrieval_allowed_classifications,
            reranker_provider=reranker_provider,
            tenant_isolation_enabled=resolved.retrieval_tenant_isolation_enabled,
            tenant_field=resolved.retrieval_tenant_field,
        )
    else:
        app.state.retriever = LexicalRetriever.from_directory(
            resolved.document_dir,
            # Stamp the local corpus with the default sandbox so tenant isolation (when enabled)
            # scopes the single-tenant lab to its own id and fails closed for any other caller.
            owner=resolved.default_sandbox_id,
            tenant_isolation_enabled=resolved.retrieval_tenant_isolation_enabled,
            tenant_field=resolved.retrieval_tenant_field,
        )
    # One verifier per app: it owns the JWKS cache (last-known-good keys + TTL) so the JWKS
    # document is not re-fetched on every request. Constructed even when disabled (cheap and
    # inert) so tests can inject a stub cache onto app.state.jwt_verifier.
    app.state.jwt_verifier = JwtVerifier(resolved)
    tracing = configure_tracing(resolved)
    app.state.tracer = tracing[0] if tracing else None
    app.state.tracer_provider = tracing[1] if tracing else None
    if app.state.tracer_provider is not None:
        # Flush buffered spans on termination: BatchSpanProcessor otherwise drops its
        # queued tail every time a pod stops, losing the last requests' traces.
        app.router.add_event_handler("shutdown", app.state.tracer_provider.shutdown)
    if resolved.vector_bootstrap_enabled and resolved.retrieval_tenant_isolation_enabled:
        # Bootstrapped platform knowledge is stamped owner=platform-team; with tenant
        # isolation on, callers only retrieve their own tenant's documents, so the
        # bootstrap corpus is invisible to tenants by design. Say so at startup instead
        # of leaving operators to debug an "empty" index.
        logging.getLogger("ai_platform_ops_lab.rag").warning(
            "tenant isolation is enabled with knowledge bootstrap: bootstrapped platform "
            "documents (owner=platform-team) are excluded from tenant-scoped retrieval"
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        try:
            request.state.request_id = _request_id_from_header(request)
            sandbox_header = request.headers.get("x-sandbox-id")
            # Track whether the tenant was explicitly asserted by the caller vs defaulted
            # server-side. Under tenant isolation the retriever only trusts an explicit id, so
            # a request that merely inherits the default sandbox does not read a tenant corpus.
            request.state.sandbox_id_from_header = sandbox_header is not None
            request.state.sandbox_id = validate_sandbox_id(
                sandbox_header if sandbox_header is not None else resolved.default_sandbox_id
            )
            request.state.traceparent = _traceparent_from_header(request)
        except ValueError as exc:
            # A request header failed validation. The message is a controlled, developer-authored
            # validation string describing the malformed header (e.g. the sandbox-id charset rule);
            # it carries no stack trace or server internals, so returning it as an actionable 400 is
            # correct API behavior, not information disclosure. Also logged for server-side triage.
            reason = str(exc)
            logging.getLogger("uvicorn.error").warning("rejected malformed request header: %s", reason)
            return JSONResponse(status_code=400, content={"detail": reason})

        async def dispatch() -> Response:
            if (
                resolved.api_key_auth_enabled
                and _auth_required(request.url.path)
                and not _valid_api_key(request, resolved)
            ):
                return _auth_failure_response(request, "invalid_or_missing_api_key")
            # Audience-bound JWT: verify the caller's own token and bind the tenant to the
            # verified claim (overriding/validating X-Sandbox-ID). Runs after the API-key
            # auth-N check so a caller is authenticated before its identity is trusted, and
            # only on business paths (health/metrics stay open for probes and scraping).
            if resolved.jwt_enabled and _auth_required(request.url.path):
                jwt_response = await _apply_jwt_identity(request, resolved, request.app.state.jwt_verifier)
                if jwt_response is not None:
                    return jwt_response
            response = await call_next(request)
            response.headers["X-Request-ID"] = request.state.request_id
            response.headers["X-Sandbox-ID"] = request.state.sandbox_id
            if request.state.traceparent:
                response.headers["traceparent"] = request.state.traceparent
            return response

        tracer = request.app.state.tracer
        if tracer is None:
            return await dispatch()
        return await trace_request(tracer, request, dispatch)

    @app.get(
        "/healthz",
        tags=["health"],
        summary="Check RAG service readiness",
        operation_id="getRagHealth",
    )
    async def healthz() -> dict[str, Any]:
        REQUESTS.labels("/healthz", "200").inc()
        body: dict[str, Any] = {
            "status": "ok",
            "documents": len(app.state.retriever.documents),
            "retrieval_backend": resolved.retrieval_backend,
            "vector_store_configured": bool(resolved.vector_store_url),
            "source_manifest_configured": resolved.rag_source_manifest is not None,
        }
        if resolved.rag_source_manifest is not None:
            body["source_manifest"] = str(resolved.rag_source_manifest)
        status = getattr(app.state.retriever, "status", None)
        if callable(status):
            body["vector_store"] = status()
        return body

    @app.get(
        "/readyz",
        tags=["health"],
        summary="Check RAG service and vector store readiness",
        operation_id="getRagReadiness",
        include_in_schema=False,
    )
    async def readyz(response: Response) -> dict[str, Any]:
        ready = True
        vector_store: dict[str, Any] = {"status": "ok"}
        embedding: dict[str, Any] = {
            "provider": resolved.embedding_provider,
            "status": "ok",
        }
        if resolved.retrieval_backend == "qdrant":
            ping = getattr(app.state.retriever, "ping", None)
            reachable = await ping() if callable(ping) else False
            vector_store = {"status": "ok" if reachable else "unavailable"}
            status_obj = getattr(app.state.retriever, "status", None)
            if callable(status_obj):
                vector_store["last_sync_status"] = status_obj().get("last_sync_status")
            provider = getattr(app.state.retriever, "embedding_provider", None)
            provider_ready = getattr(provider, "ready", None)
            embedding_reachable = await provider_ready() if callable(provider_ready) else False
            embedding = {
                "provider": getattr(provider, "name", resolved.embedding_provider),
                "status": "ok" if embedding_reachable else "unavailable",
            }
            ready = bool(reachable and embedding_reachable)
        response.status_code = 200 if ready else 503
        REQUESTS.labels("/readyz", str(response.status_code)).inc()
        return {
            "status": "ready" if ready else "not_ready",
            "retrieval_backend": resolved.retrieval_backend,
            "vector_store": vector_store,
            "embedding": embedding,
        }

    @app.get(
        "/metrics",
        tags=["observability"],
        summary="Export Prometheus metrics",
        operation_id="getRagMetrics",
    )
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get(
        "/v1/rag/documents",
        tags=["retrieval"],
        summary="List indexed knowledge documents",
        operation_id="listRagDocuments",
    )
    async def documents(request: Request) -> dict[str, Any]:
        route = "/v1/rag/documents"
        start = perf_counter()
        visible_documents = app.state.retriever.documents
        if resolved.retrieval_tenant_isolation_enabled:
            # Metadata is tenant data too. Apply the same fail-closed identity
            # rule as query(): only an explicit header or verified JWT claim is
            # trusted, and lexical documents can only be scoped by ``owner``.
            tenant = request.state.sandbox_id if request.state.sandbox_id_from_header else None
            if tenant is None or resolved.retrieval_tenant_field != "owner":
                visible_documents = []
            else:
                visible_documents = [document for document in visible_documents if document.owner == tenant]
        REQUESTS.labels(route, "200").inc()
        LATENCY.labels(route).observe(perf_counter() - start)
        return {
            "request_id": request.state.request_id,
            "sandbox_id": request.state.sandbox_id,
            "documents": [
                {
                    "id": document.id,
                    "title": document.title,
                    "source": document.source,
                }
                for document in visible_documents
            ],
        }

    @app.post(
        "/v1/rag/query",
        tags=["retrieval"],
        summary="Retrieve grounded context for a query",
        operation_id="queryRagContext",
    )
    async def query(request: Request, payload: RagQueryRequest) -> dict[str, Any]:
        route = "/v1/rag/query"
        start = perf_counter()
        status = "200"
        status_code = 200
        error = None
        result_ids: list[str] = []
        query_text = payload.query.strip()
        try:
            if not query_text:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "query must contain non-whitespace text",
                        "reason": "empty_query",
                        "request_id": request.state.request_id,
                        "sandbox_id": request.state.sandbox_id,
                    },
                )
            if len(query_text) > resolved.max_query_chars:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": "query exceeds max_query_chars",
                        "reason": "query_too_large",
                        "request_id": request.state.request_id,
                        "sandbox_id": request.state.sandbox_id,
                    },
                )
            top_k = resolved.default_top_k if payload.top_k is None else payload.top_k
            if top_k <= 0 or top_k > resolved.max_top_k:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": f"top_k must be between 1 and {resolved.max_top_k}",
                        "reason": "invalid_top_k",
                        "request_id": request.state.request_id,
                        "sandbox_id": request.state.sandbox_id,
                    },
                )
            max_context_chars = (
                resolved.max_context_chars if payload.max_context_chars is None else payload.max_context_chars
            )
            if max_context_chars <= 0 or max_context_chars > resolved.max_context_chars:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "message": f"max_context_chars must be between 1 and {resolved.max_context_chars}",
                        "reason": "invalid_max_context_chars",
                        "request_id": request.state.request_id,
                        "sandbox_id": request.state.sandbox_id,
                    },
                )

            # Under tenant isolation the tenant must come from a trusted source. When JWT
            # verification is enabled the auth middleware has already bound sandbox_id to the
            # verified tenant claim (and marked it an explicit assertion); otherwise the trusted
            # source is the caller's explicit X-Sandbox-ID (stamped by the gateway from a verified
            # token, or by a trusted egress proxy). A request that only inherited the server-side
            # default sandbox is not treated as a tenant assertion, so it fails closed rather than
            # reading the default sandbox's corpus.
            tenant = request.state.sandbox_id
            if resolved.retrieval_tenant_isolation_enabled and not request.state.sandbox_id_from_header:
                tenant = None
            results = await app.state.retriever.query(query_text, top_k, max_context_chars, tenant=tenant)
            result_ids = [result.document.id for result in results]
            context = build_context(results, max_context_chars)
            RETRIEVAL_RESULTS.labels(request.state.sandbox_id).observe(len(results))
            response: dict[str, Any] = {
                "request_id": request.state.request_id,
                "sandbox_id": request.state.sandbox_id,
                "retrieval_backend": resolved.retrieval_backend,
                "query_sha256": _hash_query(query_text),
                "results": [
                    {
                        "id": result.document.id,
                        "title": result.document.title,
                        "source": result.document.source,
                        "score": result.score,
                        "excerpt": result.excerpt,
                    }
                    for result in results
                ],
            }
            if payload.include_context:
                response["context"] = context
            if payload.include_messages:
                response["grounded_messages"] = [
                    {
                        "role": "system",
                        "content": (
                            "Use the retrieved platform context to answer. "
                            "If the context is insufficient, say what is missing. "
                            "Do not reveal hidden chain-of-thought."
                        ),
                    },
                    {
                        "role": "system",
                        "content": f"Retrieved context:\n{context}" if context else "Retrieved context: none",
                    },
                    {"role": "user", "content": query_text},
                ]
            return response
        except HTTPException as exc:
            status = str(exc.status_code)
            status_code = exc.status_code
            error = str(exc.detail)
            raise
        except VectorStoreError as exc:
            status = "503"
            status_code = 503
            error = str(exc)
            raise HTTPException(
                status_code=503,
                detail={
                    "message": "vector store is unavailable",
                    "reason": "vector_store_unavailable",
                    "request_id": request.state.request_id,
                    "sandbox_id": request.state.sandbox_id,
                },
            ) from exc
        finally:
            REQUESTS.labels(route, status).inc()
            latency_seconds = perf_counter() - start
            LATENCY.labels(route).observe(latency_seconds)
            _write_audit_log(
                resolved,
                request,
                route,
                status_code=status_code,
                latency_seconds=latency_seconds,
                query=query_text,
                result_ids=result_ids,
                error=error,
            )

    _install_openapi_contract(app, resolved)
    return app


app = create_app()
