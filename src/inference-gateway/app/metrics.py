"""Prometheus metrics emitted by the inference gateway."""

from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter(
    "inference_gateway_requests_total",
    "Total inference gateway requests by route, backend, and status.",
    ["route", "backend", "status"],
)
LATENCY = Histogram(
    "inference_gateway_request_duration_seconds",
    "Inference gateway request latency by route and backend.",
    ["route", "backend"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)
SANDBOX_REQUESTS = Counter(
    "inference_gateway_sandbox_requests_total",
    "Total inference gateway requests by sandbox, backend, and status.",
    ["sandbox", "backend", "status"],
)
TOKEN_USAGE = Counter(
    "inference_gateway_tokens_total",
    "Runtime-reported token usage by backend and token type.",
    ["backend", "token_type"],
)
ADMISSION_REJECTIONS = Counter(
    "inference_gateway_admission_rejections_total",
    "Inference requests rejected by gateway admission policy.",
    ["reason", "backend", "sandbox"],
)
SANDBOX_BUDGET_USAGE = Gauge(
    "inference_gateway_sandbox_budget_usage",
    "Current sandbox budget usage by budget type.",
    ["sandbox", "budget_type"],
)
SANDBOX_BUDGET_LIMIT = Gauge(
    "inference_gateway_sandbox_budget_limit",
    "Configured sandbox budget limit by budget type. Zero means unlimited.",
    ["sandbox", "budget_type"],
)
AGENT_RECEIPTS = Counter(
    "inference_gateway_agent_receipts_total",
    "Agent-action receipts chained by action type and decision; rejections by reason.",
    ["action_type", "decision"],
)
AUDIT_CHAIN_PERSIST = Counter(
    "inference_gateway_audit_chain_persist_total",
    "Audit chain head writes to the durable head store, by outcome.",
    ["outcome"],
)
BUDGET_SETTLEMENTS = Counter(
    "inference_gateway_budget_settlements_total",
    "Budget reservations reconciled against measured usage, by outcome.",
    ["outcome"],
)
BUDGET_SETTLED_TOKENS = Counter(
    "inference_gateway_budget_settled_tokens_total",
    "Estimated tokens returned to (refunded) or added to (overrun) sandbox budgets by settlement.",
    ["sandbox", "direction"],
)
AUTH_FAILURES = Counter(
    "inference_gateway_auth_failures_total",
    "Total gateway authentication failures by route and reason.",
    ["route", "reason"],
)
RATE_LIMITED = Counter(
    "inference_gateway_rate_limited_total",
    "Total gateway requests rejected by the per-sandbox rate limiter.",
    ["sandbox"],
)
RATE_LIMIT_FAIL_OPEN = Counter(
    "inference_gateway_rate_limit_fail_open_total",
    "Requests admitted without a rate-limit check because the backend was down and "
    "RATE_LIMIT_FAIL_OPEN is enabled (deliberate availability-over-enforcement fallback).",
    ["sandbox"],
)
RUNTIME_FALLBACKS = Counter(
    "inference_gateway_runtime_fallbacks_total",
    "Total times a request failed over from one runtime route to a fallback.",
    ["from_backend", "to_backend"],
)
LOAD_SHED = Counter(
    "inference_gateway_load_shed_total",
    "Total requests rejected by the gateway concurrency limit (load shedding).",
    ["route"],
)
INFLIGHT = Gauge(
    "inference_gateway_inflight_requests",
    "Current number of in-flight gateway requests subject to the concurrency limit.",
)
CACHE_LOOKUPS = Counter(
    "inference_gateway_response_cache_total",
    "Response cache lookups by result.",
    ["result"],
)
CANARY_ROUTED = Counter(
    "inference_gateway_canary_routed_total",
    "Requests routed to a canary model by weighted progressive delivery.",
    ["from_model", "to_model"],
)
SHADOW_REQUESTS = Counter(
    "inference_gateway_shadow_requests_total",
    "Shadow (mirrored) requests sent fire-and-forget to a shadow model.",
    ["backend", "result"],
)
OUTPUT_GUARDRAIL = Counter(
    "inference_gateway_output_guardrail_total",
    "Model completions acted on by the output guardrail, by action and surface.",
    ["action", "route"],
)
PROMPT_GUARDRAIL = Counter(
    "inference_gateway_prompt_guardrail_total",
    "Prompts acted on by the input secret guardrail in redact/flag mode, by action and surface.",
    ["action", "route"],
)
ESTIMATED_COST = Counter(
    "inference_gateway_estimated_cost_usd_total",
    "Estimated monetary cost of runtime usage by sandbox and backend (USD_PER_1K_TOKENS model).",
    ["sandbox", "backend"],
)

# Bound the distinct sandbox label values this process will emit. Sandbox ids are
# client-asserted (up to 63 free-form chars) unless JWT tenant binding is enabled, so
# without a bound a scripted client cycling X-Sandbox-ID values could mint unbounded
# Prometheus series. The cap comfortably exceeds a real tenant fleet; ids past it are
# still served and audited under their real id but collapse into one overflow label.
_MAX_SANDBOX_LABEL_VALUES = 2000
_SANDBOX_LABEL_VALUES: set[str] = set()
_SANDBOX_LABEL_OVERFLOW = "__other__"


def sandbox_label(sandbox_id: str) -> str:
    """Return the sandbox metric label, collapsing past the cardinality bound."""
    if sandbox_id in _SANDBOX_LABEL_VALUES:
        return sandbox_id
    if len(_SANDBOX_LABEL_VALUES) < _MAX_SANDBOX_LABEL_VALUES:
        _SANDBOX_LABEL_VALUES.add(sandbox_id)
        return sandbox_id
    return _SANDBOX_LABEL_OVERFLOW
