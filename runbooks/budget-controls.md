# Sandbox Budget Controls Runbook

Use this runbook when a lab user hits a gateway budget limit or when sizing budgets for a local demo or customer-owned Kubernetes cluster.

## What Is Enforced

The inference gateway can enforce three budget ceilings per `X-Sandbox-ID`:

- accepted request count
- cumulative prompt characters
- cumulative estimated tokens

Estimated tokens are reserved at admission as `ceil(prompt characters / estimatedCharsPerToken) + requested max_tokens`. If the caller omits `max_tokens`, the gateway uses the configured `admission.maxCompletionTokens` ceiling for the estimate.

That reservation is the worst case the request could cost, because admission has to charge before the model runs. Once the runtime reports what the call actually consumed, the gateway **settles** the reservation: the estimate is replaced by the measured total, and the difference is returned to the window. This matters most for exactly the traffic this platform exists for, since a coding agent typically asks for a large `max_tokens` and emits a fraction of it; without settlement it would be metered as if it had emitted all of it and hit the window limit long before its real spend justified it.

Settlement runs on the streaming and non-streaming paths alike, and the correction is recorded on the request's audit receipt as `budget_settlement` (reserved, actual, refunded, overrun, settled). Two behaviors are deliberate:

- A runtime that reports **no usage** leaves the reservation standing rather than refunding it, so a request that burned runtime capacity and then failed late is still charged.
- A settlement that fails never fails a request that already succeeded. The response is committed, and an unsettled reservation can only over-charge, never hand out free budget.

`estimatedCharsPerToken` is calibrated per model in [the model catalog](https://github.com/RamazanKara/private-ai-platform-kit/blob/main/platform/model-catalog/models.yaml) and carried into the routing policy, because one global divisor is wrong in opposite directions for different models: prose in a Latin script runs near four characters per token, source code nearer three, and non-Latin scripts closer to one. A model that declares none falls back to the gateway default.

The gateway supports two budget backends. `memory` stores usage in the gateway process and is useful for unit tests or single-pod development. `redis` stores usage in a Redis-compatible service and is the default for local and customer values because it works across multiple gateway replicas.

## Configuration

Set budgets in Helm values:

    budget:
      enabled: true
      backend: redis
      requestLimit: 250
      promptCharLimit: 500000
      estimatedTokenLimit: 150000
      estimatedCharsPerToken: 4
      windowSeconds: 86400
      redisUrl: redis://budget-redis.budget.svc.cluster.local:6379/0
      redisTimeoutSeconds: "0.5"
      keyPrefix: private-ai-platform-kit:local:sandbox-budget

The rendered gateway Deployment exposes these as:

    SANDBOX_BUDGET_ENABLED
    SANDBOX_BUDGET_BACKEND
    SANDBOX_REQUEST_BUDGET
    SANDBOX_PROMPT_CHAR_BUDGET
    SANDBOX_ESTIMATED_TOKEN_BUDGET
    BUDGET_ESTIMATED_CHARS_PER_TOKEN
    SANDBOX_BUDGET_WINDOW_SECONDS
    SANDBOX_BUDGET_REDIS_URL
    SANDBOX_BUDGET_REDIS_TIMEOUT_SECONDS
    SANDBOX_BUDGET_KEY_PREFIX

The bundled `deploy/charts/budget-redis` chart is a local and portable default. Customer clusters can keep the same gateway values shape and point `budget.redisUrl` at a managed or enterprise Redis-compatible service.

Reviewed quota and chargeback plans live in `platform/governance/quota-plans.yaml`. Keep gateway budgets aligned with the tenant's `platform.ai/owner`, `platform.ai/cost-center`, `platform.ai/environment`, and `platform.ai/sandbox-id` labels so Prometheus, OpenCost-style reporting, logs, and evidence packs attribute usage to the same owner.

## Inspect Current Usage

Port-forward the gateway and query the budget endpoint with the sandbox id:

    kubectl -n inference port-forward svc/inference-gateway-inference-gateway 18082:8080
    curl -sS -H 'X-Sandbox-ID: local-lab' http://127.0.0.1:18082/v1/sandbox/budget

Expected shape:

    {
      "enabled": true,
      "backend": "redis",
      "sandbox_id": "local-lab",
      "usage": {
        "requests": 3,
        "prompt_chars": 1200,
        "estimated_tokens": 800
      },
      "limits": {
        "requests": 250,
        "prompt_chars": 500000,
        "estimated_tokens": 150000
      },
      "window_seconds": 86400,
      "window_ttl_seconds": 86120,
      "estimated_chars_per_token": 4
    }

## Response Budget Headers

When budgets are enabled, successful `/v1/chat/completions` (including streaming) and `/v1/embeddings` responses report the sandbox budget after the request's own reservation, using the OpenAI-style header names agent frameworks already parse:

    x-ratelimit-limit-requests: 250
    x-ratelimit-remaining-requests: 247
    x-ratelimit-limit-tokens: 150000
    x-ratelimit-remaining-tokens: 148900

Remaining values are floored at zero. A header pair is omitted when its limit is 0 (unlimited), and cache hits (`X-Cache: HIT`) omit all four because they reserve no budget. Token values are the same estimated tokens the budget enforces, not runtime-reported usage.

## Triage Rejections

Budget rejections return HTTP 400. Check `detail.reason`:

- `sandbox_request_budget_exceeded`: too many accepted requests for the sandbox.
- `sandbox_prompt_budget_exceeded`: prompt-character budget would be exceeded.
- `sandbox_token_budget_exceeded`: estimated-token budget would be exceeded.

Then inspect:

    kubectl -n inference logs deploy/inference-gateway-inference-gateway --tail=100 | grep inference_request

Audit events include `budget` usage snapshots without raw prompt text. Prometheus also exposes:

    inference_gateway_sandbox_budget_usage
    inference_gateway_sandbox_budget_limit
    inference_gateway_admission_rejections_total

Confirm the shared backend is reachable:

    kubectl -n budget get deploy,svc,networkpolicy
    kubectl -n budget exec deploy/budget-redis -- redis-cli ping

## Response

If the sandbox is intentionally load testing or running an approved evaluation, raise the budget in the environment values and redeploy. If the traffic is unexpected, keep the budget in place, identify the caller through request IDs and sandbox IDs, and review the model catalog and admission limits before allowing more traffic.
