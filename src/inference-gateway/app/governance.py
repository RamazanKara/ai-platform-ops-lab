"""The shared rail every governed inference endpoint runs on.

Every inference endpoint does the same four things around its own logic: resolve the
model route, reserve sandbox budget, map one failure taxonomy onto the OpenAI-shaped
error envelope, and record exactly one receipt (metrics, settlement, audit) per request.
Until v0.28.x each handler carried a private copy of that tail - five near-identical
blocks of exception mapping and end-of-request recording that had to be edited in five
places to stay identical. This module is that tail, written once.

The contract: a handler opens :func:`governed`, does its endpoint-specific work inside,
and keeps the yielded :class:`GovernedCall` current (backend after routing, the payload
the receipt should fingerprint, the runtime response once it exists). The rail owns what
happens when the work raises and what gets recorded when it ends, on the success and
failure paths alike. Streaming handlers hand recording to their stream body by setting
``stream_owns_recording`` and calling :func:`record_stream_end` at true end-of-stream,
because for a stream the request is not over when the headers are.

Except-order in the rail is load-bearing: ``AdmissionPolicyError`` subclasses
``ValueError``, so the admission arm must come first or every policy rejection would be
misreported as a malformed runtime response.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from time import perf_counter
from typing import Any, Literal

import httpx
from fastapi import HTTPException, Request

from app.audit import write_audit_log
from app.budget import BudgetBackendError, BudgetReservation, SandboxBudgetTracker
from app.metrics import (
    ADMISSION_REJECTIONS,
    BUDGET_SETTLED_TOKENS,
    BUDGET_SETTLEMENTS,
    ESTIMATED_COST,
    LATENCY,
    OUTPUT_GUARDRAIL,
    REQUESTS,
    SANDBOX_BUDGET_LIMIT,
    SANDBOX_BUDGET_USAGE,
    SANDBOX_REQUESTS,
    TOKEN_USAGE,
)
from app.metrics import (
    sandbox_label as _sandbox_label,
)
from app.policy import ModelRoutingPolicy, SandboxPolicySet
from app.settings import AdmissionPolicyError, Settings


@dataclass
class GovernedCall:
    """Mutable per-request state the rail reads when the call ends.

    Handlers mutate this as facts become known: ``backend`` once the route resolves (and
    again if failover moves it), ``payload`` if translation rebinds it, and
    ``runtime_response`` once the runtime answers, so the finally-path records what
    actually happened rather than what the handler assumed at entry.
    """

    route: str
    backend: str
    payload: dict[str, Any]
    runtime_response: dict[str, Any] | None = None
    status: str = "200"
    status_code: int = 200
    runtime_status_code: int | None = None
    error: str | None = None
    # True once a streaming body has taken ownership of end-of-request recording; the
    # rail's finally must then stay silent or every stream would be double-counted.
    stream_owns_recording: bool = False
    # A cache hit consumed no runtime tokens: the token/cost metrics must not re-count
    # the cached usage (the audit receipt still records the hit).
    cache_hit: bool = False
    start: float = field(default_factory=perf_counter)


class governed:
    """Async context manager wrapping one governed endpoint invocation.

    Owns the shared exception mapping (admission -> 4xx, budget backend -> 503, runtime
    failures -> 502) and the end-of-request recording. Written as a class rather than an
    ``@asynccontextmanager`` generator so the exception arms read as the flat block they
    replaced instead of a generator throwing into itself.
    """

    def __init__(self, request: Request, settings: Settings, *, route: str, payload: dict[str, Any]) -> None:
        self._request = request
        self._settings = settings
        self.call = GovernedCall(route=route, backend=settings.runtime_backend, payload=payload)

    async def __aenter__(self) -> GovernedCall:
        self._request.state.budget_reservation = None
        return self.call

    # Literal[False], not bool: the rail never suppresses an exception, and the narrower
    # type is what lets mypy prove that code after an ``async with`` raise is unreachable.
    async def __aexit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> Literal[False]:
        request, settings, call = self._request, self._settings, self.call
        try:
            if exc is None:
                return False
            if isinstance(exc, AdmissionPolicyError):
                status_code, headers = admission_status(exc.reason, settings)
                call.status = str(status_code)
                call.status_code = status_code
                call.error = str(exc)
                ADMISSION_REJECTIONS.labels(exc.reason, call.backend, _sandbox_label(request.state.sandbox_id)).inc()
                raise HTTPException(
                    status_code=status_code,
                    detail={
                        "message": call.error,
                        "reason": exc.reason,
                        "request_id": request.state.request_id,
                        "sandbox_id": request.state.sandbox_id,
                    },
                    headers=headers,
                ) from exc
            if isinstance(exc, BudgetBackendError):
                call.status = "503"
                call.status_code = 503
                call.error = "sandbox budget backend is unavailable"
                raise HTTPException(
                    status_code=503,
                    detail={
                        "message": call.error,
                        "reason": "budget_backend_unavailable",
                        "request_id": request.state.request_id,
                        "sandbox_id": request.state.sandbox_id,
                    },
                    headers={"Retry-After": "5"},
                ) from exc
            if isinstance(exc, httpx.HTTPStatusError):
                call.status = "502"
                call.status_code = 502
                call.runtime_status_code = exc.response.status_code
                call.error = "runtime returned an error"
                raise HTTPException(
                    status_code=502,
                    detail={
                        "message": call.error,
                        "runtime_status": exc.response.status_code,
                        "backend": call.backend,
                        "request_id": request.state.request_id,
                        "sandbox_id": request.state.sandbox_id,
                    },
                ) from exc
            if isinstance(exc, httpx.HTTPError):
                call.status = "502"
                call.status_code = 502
                call.error = "runtime request failed"
                raise HTTPException(
                    status_code=502,
                    detail={
                        "message": call.error,
                        "backend": call.backend,
                        "request_id": request.state.request_id,
                        "sandbox_id": request.state.sandbox_id,
                    },
                ) from exc
            if isinstance(exc, ValueError):
                call.status = "502"
                call.status_code = 502
                call.error = "runtime returned an invalid response"
                raise HTTPException(
                    status_code=502,
                    detail={
                        "message": call.error,
                        "backend": call.backend,
                        "request_id": request.state.request_id,
                        "sandbox_id": request.state.sandbox_id,
                    },
                ) from exc
            # Anything else (HTTPException raised deliberately, cancellation, a genuine
            # bug) propagates untouched; the finally below still records it.
            return False
        finally:
            # Streaming responses record at true end-of-stream inside their body; the
            # rail records the non-streaming and error-before-headers paths.
            if not call.stream_owns_recording:
                REQUESTS.labels(call.route, call.backend, call.status).inc()
                SANDBOX_REQUESTS.labels(_sandbox_label(request.state.sandbox_id), call.backend, call.status).inc()
                latency_seconds = perf_counter() - call.start
                LATENCY.labels(call.route, call.backend).observe(latency_seconds)
                if not call.cache_hit:
                    record_token_usage(call.backend, call.runtime_response)
                    record_estimated_cost(
                        settings, request.state.sandbox_id, call.backend, (call.runtime_response or {}).get("usage")
                    )
                await settle_and_audit(
                    settings,
                    request,
                    call.payload,
                    status_code=call.status_code,
                    latency_seconds=latency_seconds,
                    backend=call.backend,
                    runtime_response=call.runtime_response,
                    runtime_status_code=call.runtime_status_code,
                    error=call.error,
                )


async def record_stream_end(
    request: Request,
    settings: Settings,
    *,
    route: str,
    backend: str,
    start: float,
    status: str,
    status_code: int,
    usage: dict[str, Any] | None,
    error: str | None,
    payload: dict[str, Any],
    guardrail_text: str,
) -> None:
    """Record a streaming request once its body has fully drained.

    The streamed bytes are already committed to the wire, so the output guardrail cannot
    redact or block here; it scans the accumulated text and flags findings on the metric,
    with enforcement remaining a non-streaming concern. ``usage`` is whatever terminal
    usage event the stream carried; None means the runtime never reported, which settles
    nothing and leaves the conservative reservation standing.
    """
    latency_seconds = perf_counter() - start
    REQUESTS.labels(route, backend, status).inc()
    SANDBOX_REQUESTS.labels(_sandbox_label(request.state.sandbox_id), backend, status).inc()
    LATENCY.labels(route, backend).observe(latency_seconds)
    usage_response = {"usage": usage} if usage is not None else None
    record_token_usage(backend, usage_response)
    record_estimated_cost(settings, request.state.sandbox_id, backend, usage)
    if settings.output_guardrail_enabled and guardrail_text:
        patterns, terms = settings.output_findings(guardrail_text)
        if patterns or terms:
            OUTPUT_GUARDRAIL.labels("flagged_stream", route).inc()
    await settle_and_audit(
        settings,
        request,
        payload,
        status_code=status_code,
        latency_seconds=latency_seconds,
        backend=backend,
        runtime_response=usage_response,
        error=error,
    )


def resolve_single_route(request: Request, settings: Settings, payload_dict: dict[str, Any]) -> tuple[Settings, Any]:
    """Resolve the request's model to its single route and return (effective settings, route).

    The shared prologue for every endpoint that routes to exactly one model (chat resolves
    a failover *chain* and keeps its own prologue). Applies sandbox-policy and per-key
    overrides, converts an unapproved model into the ``model_not_allowed`` admission
    rejection, rewrites ``payload_dict["model"]`` to the canonical id, and folds the
    route's calibrated chars-per-token into the returned settings.
    """
    policy: ModelRoutingPolicy = request.app.state.model_routing_policy
    sandbox_policies: SandboxPolicySet = request.app.state.sandbox_policy_set
    effective = effective_settings(request, sandbox_policies, settings)
    try:
        model_route = policy.resolve(payload_dict.get("model"), effective.model_id)
    except ValueError as exc:
        raise AdmissionPolicyError("model_not_allowed", str(exc)) from exc
    payload_dict["model"] = model_route.model_id
    return route_settings(effective, model_route), model_route


async def reserve_budget(request: Request, effective: Settings, budget_payload: dict[str, Any]) -> None:
    """Reserve sandbox budget for the payload and surface the reservation everywhere it goes.

    One reservation feeds four consumers: the audit receipt (as a dict projection), the
    settlement path (as the object itself, which needs the reserved estimate), the budget
    gauges, and the ``x-ratelimit-*`` headroom headers. Runs on a worker thread because
    the Redis client is synchronous and a slow Redis must not stall the event loop.
    """
    tracker: SandboxBudgetTracker = request.app.state.budget_tracker
    reservation = await asyncio.to_thread(tracker.reserve, request.state.sandbox_id, budget_payload, effective)
    request.state.budget_reservation = reservation.audit_dict() if reservation is not None else None
    request.state.budget_reservation_object = reservation
    record_budget_reservation(reservation, effective)
    request.state.budget_headers = budget_headers(reservation, effective)


def effective_settings(request: Request, policy_set: SandboxPolicySet, settings: Settings) -> Settings:
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


def route_settings(settings: Settings, model_route: Any) -> Settings:
    """Apply a route's calibrated chars-per-token to the settings used for budgeting.

    Falls back to the gateway default when the route declares none, so a catalog that has
    not been calibrated yet behaves exactly as before rather than silently changing what
    every tenant is charged.
    """
    per_token = getattr(model_route, "estimated_chars_per_token", 0)
    if not per_token or per_token == settings.budget_estimated_chars_per_token:
        return settings
    return replace(settings, budget_estimated_chars_per_token=per_token)


def admission_status(reason: str, settings: Settings) -> tuple[int, dict[str, str] | None]:
    """Map an admission-rejection reason to its HTTP status and retry headers."""
    if reason.startswith("sandbox_") and reason.endswith("_exceeded"):
        headers = None
        if settings.sandbox_budget_window_seconds > 0:
            headers = {"Retry-After": str(settings.sandbox_budget_window_seconds)}
        return 429, headers
    return 400, None


def budget_headers(reservation: BudgetReservation | None, settings: Settings) -> dict[str, str]:
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


def record_token_usage(backend: str, runtime_response: dict[str, Any] | None) -> None:
    """Increment the per-backend token counters from a runtime-reported usage object."""
    usage = (runtime_response or {}).get("usage")
    if not isinstance(usage, dict):
        return
    for token_type in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(token_type)
        if isinstance(value, (int, float)) and value >= 0:
            TOKEN_USAGE.labels(backend, token_type).inc(value)


def record_estimated_cost(settings: Settings, sandbox_id: str, backend: str, usage: dict[str, Any] | None) -> None:
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


def record_budget_reservation(reservation: BudgetReservation | None, settings: Settings) -> None:
    """Publish a reservation's usage and limits to the sandbox budget gauges."""
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


async def settle_budget(request: Request, runtime_response: dict[str, Any] | None) -> None:
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


async def settle_and_audit(
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
    await settle_budget(request, runtime_response)
    write_audit_log(
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
