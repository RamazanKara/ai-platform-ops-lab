#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
ENV_NAME_PATTERN = re.compile(r"- name:\s+([A-Z0-9_]+)")


@dataclass(frozen=True)
class ConfigVar:
    name: str
    field: str
    kind: str
    app_default: Any
    chart_value: str
    chart_default: Any
    description: str
    aliases: tuple[str, ...] = ()
    sensitive: bool = False
    allowed_values: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServiceContract:
    service: str
    service_dir: Path
    chart_dir: Path
    snapshot: Path
    variables: tuple[ConfigVar, ...]
    settings_file: str = "app/settings.py"
    deployment_template: str = "templates/deployment.yaml"
    values_file: str = "values.yaml"
    contract_version: str = "v1"


GATEWAY_VARS = (
    ConfigVar("RUNTIME_BACKEND", "runtime_backend", "string", "ollama", "runtime.backend", "ollama", "Runtime adapter used by the gateway.", allowed_values=("ollama", "vllm")),
    ConfigVar("MODEL_ID", "model_id", "string", "qwen3.5:0.8b", "runtime.modelId", "qwen3.5:0.8b", "Default model used when requests omit a model."),
    ConfigVar("OLLAMA_BASE_URL", "ollama_base_url", "url", "http://ollama.ollama.svc.cluster.local:11434", "runtime.ollamaBaseUrl", "http://ollama.ollama.svc.cluster.local:11434", "Base URL for the Ollama runtime."),
    ConfigVar("VLLM_BASE_URL", "vllm_base_url", "url", "http://vllm.vllm.svc.cluster.local:8000", "runtime.vllmBaseUrl", "http://vllm.vllm.svc.cluster.local:8000", "Base URL for the vLLM runtime."),
    ConfigVar("REQUEST_TIMEOUT_SECONDS", "request_timeout_seconds", "float", 120.0, "runtime.requestTimeoutSeconds", "120", "Runtime request timeout in seconds."),
    ConfigVar("RUNTIME_MAX_RETRIES", "runtime_max_retries", "integer", 2, "runtime.maxRetries", 2, "Bounded retry count for runtime requests; retries cover transient 5xx/429 and connect errors (and pre-first-byte on the streaming path)."),
    ConfigVar("RUNTIME_RETRY_BACKOFF_SECONDS", "runtime_retry_backoff_seconds", "float", 0.1, "runtime.retryBackoffSeconds", "0.1", "Exponential retry backoff base in seconds (doubled per attempt, with equal jitter)."),
    ConfigVar("RUNTIME_CIRCUIT_FAILURE_THRESHOLD", "runtime_circuit_failure_threshold", "integer", 0, "runtime.circuitFailureThreshold", 0, "Consecutive runtime failures before opening the circuit; zero disables circuit breaking."),
    ConfigVar("RUNTIME_CIRCUIT_RESET_SECONDS", "runtime_circuit_reset_seconds", "float", 30.0, "runtime.circuitResetSeconds", "30", "Seconds before an opened runtime circuit is retried."),
    ConfigVar("ALLOWED_MODELS", "allowed_models", "csv", ["qwen3.5:0.8b"], "runtime.allowedModels", ["qwen3.5:0.8b"], "Comma-separated allowlist of approved model IDs."),
    ConfigVar("MODEL_ROUTING_POLICY_PATH", "model_routing_policy_path", "path", None, "routing.policyPath", "", "Optional path to a platform.ai/v1alpha1 ModelRoutingPolicy YAML file."),
    ConfigVar("SANDBOX_POLICY_PATH", "sandbox_policy_path", "path", None, "sandboxPolicy.policyPath", "", "Optional path to a platform.ai/v1alpha1 SandboxPolicySet YAML file."),
    ConfigVar("MAX_MESSAGES", "max_messages", "integer", 16, "admission.maxMessages", 16, "Maximum messages accepted per chat request."),
    ConfigVar("MAX_PROMPT_CHARS", "max_prompt_chars", "integer", 8192, "admission.maxPromptChars", 8192, "Maximum aggregate prompt characters accepted per request."),
    ConfigVar("MAX_REQUEST_BODY_BYTES", "max_request_body_bytes", "integer", 1048576, "admission.maxRequestBodyBytes", 1048576, "Maximum JSON request body size before parsing; Files uploads use batch.maxFileBytes."),
    ConfigVar("MAX_COMPLETION_TOKENS", "max_completion_tokens", "integer", 1024, "admission.maxCompletionTokens", 1024, "Maximum requested completion tokens (honors both max_tokens and max_completion_tokens)."),
    ConfigVar("MAX_COMPLETIONS_PER_REQUEST", "max_completions_per_request", "integer", 1, "admission.maxCompletionsPerRequest", 1, "Maximum completions per request (the OpenAI 'n' parameter); the completion budget is charged n times."),
    ConfigVar("IMAGE_PART_TOKEN_ESTIMATE", "image_part_token_estimate", "integer", 768, "admission.imagePartTokenEstimate", 768, "Flat token estimate charged per image_url content part so multimodal prompts are metered."),
    ConfigVar("MAX_IMAGE_BYTES", "max_image_bytes", "integer", 0, "admission.maxImageBytes", 0, "Maximum decoded size of a single data-URL image part; zero disables the check."),
    ConfigVar("MAX_TOOLS", "max_tools", "integer", 64, "admission.maxTools", 64, "Maximum tool/function definitions accepted per request."),
    ConfigVar("MAX_TOOL_CHARS", "max_tool_chars", "integer", 32768, "admission.maxToolChars", 32768, "Maximum serialized characters of tool/function definitions per request."),
    ConfigVar("ALLOW_STREAMING", "allow_streaming", "boolean", False, "admission.allowStreaming", True, "Whether streaming chat completions are admitted (shipped on; the regulated-offline profile disables it)."),
    ConfigVar("RATE_LIMIT_ENABLED", "rate_limit_enabled", "boolean", False, "rateLimit.enabled", False, "Whether the short-window per-sandbox request rate limiter is enabled."),
    ConfigVar("RATE_LIMIT_REQUESTS_PER_WINDOW", "rate_limit_requests_per_window", "integer", 0, "rateLimit.requestsPerWindow", 0, "Max requests per sandbox per rate-limit window; zero means unlimited."),
    ConfigVar("RATE_LIMIT_WINDOW_SECONDS", "rate_limit_window_seconds", "integer", 60, "rateLimit.windowSeconds", 60, "Rate-limit fixed-window length in seconds."),
    ConfigVar("RATE_LIMIT_FAIL_OPEN", "rate_limit_fail_open", "boolean", False, "rateLimit.failOpen", False, "When the shared rate-limit backend (Redis) is unreachable, fail OPEN (admit with a logged warning + metric) instead of the default fail-closed 503. Deliberate availability-over-enforcement tradeoff; budgets stay fail-closed regardless."),
    ConfigVar("MAX_CONCURRENT_REQUESTS", "max_concurrent_requests", "integer", 0, "concurrency.maxConcurrentRequests", 0, "Max in-flight requests before load shedding with 503; zero disables the limit."),
    ConfigVar("MAX_BATCH_REQUESTS", "max_batch_requests", "integer", 32, "concurrency.maxBatchRequests", 32, "Maximum number of chat requests accepted in one /v1/batch-inference call."),
    ConfigVar("USD_PER_1K_TOKENS", "usd_per_1k_tokens", "float", 0.0, "cost.usdPer1kTokens", 0, "Estimated cost per 1000 tokens surfaced by /v1/usage; zero disables the cost model."),
    ConfigVar("COST_CURRENCY", "cost_currency", "string", "USD", "cost.currency", "USD", "Currency label for the /v1/usage cost estimate."),
    ConfigVar("RESPONSE_CACHE_ENABLED", "response_cache_enabled", "boolean", False, "responseCache.enabled", False, "Whether the exact-match per-sandbox response cache is enabled for non-streaming chat."),
    ConfigVar("RESPONSE_CACHE_TTL_SECONDS", "response_cache_ttl_seconds", "integer", 60, "responseCache.ttlSeconds", 60, "Response cache entry time-to-live in seconds."),
    ConfigVar("RESPONSE_CACHE_MAX_ENTRIES", "response_cache_max_entries", "integer", 1024, "responseCache.maxEntries", 1024, "Maximum cached responses before LRU eviction."),
    ConfigVar("RESPONSE_CACHE_BACKEND", "response_cache_backend", "string", "memory", "responseCache.backend", "memory", "Response cache backend.", allowed_values=("memory", "redis")),
    ConfigVar("RESPONSE_CACHE_REDIS_URL", "response_cache_redis_url", "url", "redis://budget-redis.budget.svc.cluster.local:6379/1", "responseCache.redisUrl", "redis://budget-redis.budget.svc.cluster.local:6379/1", "Redis URL for the shared response cache when backend is redis."),
    ConfigVar("RESPONSE_CACHE_REDIS_TIMEOUT_SECONDS", "response_cache_redis_timeout_seconds", "float", 0.5, "responseCache.redisTimeoutSeconds", "0.5", "Redis response-cache operation timeout in seconds."),
    ConfigVar("RESPONSE_CACHE_KEY_PREFIX", "response_cache_key_prefix", "string", "private-ai-platform-kit:response-cache", "responseCache.keyPrefix", "private-ai-platform-kit:response-cache", "Redis key prefix for shared response-cache entries."),
    ConfigVar("PROMPT_SECRET_DETECTION_ENABLED", "prompt_secret_detection_enabled", "boolean", True, "guardrails.promptSecretDetection.enabled", True, "Whether prompt credential-pattern detection is enabled."),
    ConfigVar("PROMPT_SECRET_MODE", "prompt_secret_mode", "string", "block", "guardrails.promptSecretDetection.mode", "block", "Action on a prompt secret match: block (reject), redact (replace matched spans before forwarding), or flag (allow and record).", allowed_values=("block", "redact", "flag")),
    ConfigVar("PROMPT_SECRET_PATTERNS", "prompt_secret_patterns", "csv", ["private_key", "github_token", "slack_token", "aws_access_key_id", "google_api_key", "bearer_token", "generic_api_key_assignment"], "guardrails.promptSecretDetection.patterns", ["private_key", "github_token", "slack_token", "aws_access_key_id", "google_api_key", "bearer_token", "generic_api_key_assignment"], "Built-in secret/PII detector names applied to prompts (credential detectors by default; PII detectors email/us_ssn/credit_card are opt-in)."),
    ConfigVar("BLOCKED_CONTENT_TERMS", "blocked_content_terms", "csv", [], "guardrails.blockedContentTerms", [], "Case-insensitive substrings rejected anywhere in prompt/embedding input."),
    ConfigVar("OUTPUT_GUARDRAIL_ENABLED", "output_guardrail_enabled", "boolean", False, "guardrails.outputGuardrail.enabled", False, "Whether the response-path output guardrail inspects model completions (OWASP LLM02:2025/LLM05:2025)."),
    ConfigVar("OUTPUT_GUARDRAIL_MODE", "output_guardrail_mode", "string", "redact", "guardrails.outputGuardrail.mode", "redact", "Action on a flagged completion.", allowed_values=("flag", "redact", "block")),
    ConfigVar("OUTPUT_GUARDRAIL_PATTERNS", "output_guardrail_patterns", "csv", ["private_key", "github_token", "slack_token", "aws_access_key_id", "google_api_key", "bearer_token", "generic_api_key_assignment", "email", "us_ssn", "credit_card"], "guardrails.outputGuardrail.patterns", ["private_key", "github_token", "slack_token", "aws_access_key_id", "google_api_key", "bearer_token", "generic_api_key_assignment", "email", "us_ssn", "credit_card"], "Built-in detector names scanned on model output (credentials + PII by default)."),
    ConfigVar("SANDBOX_BUDGET_ENABLED", "sandbox_budget_enabled", "boolean", False, "budget.enabled", True, "Whether sandbox request and token budgets are enforced."),
    ConfigVar("SANDBOX_BUDGET_BACKEND", "sandbox_budget_backend", "string", "memory", "budget.backend", "memory", "Budget counter backend.", allowed_values=("memory", "redis")),
    ConfigVar("SANDBOX_REQUEST_BUDGET", "sandbox_request_budget", "integer", 0, "budget.requestLimit", 1000, "Maximum requests per sandbox budget window; zero means unlimited."),
    ConfigVar("SANDBOX_PROMPT_CHAR_BUDGET", "sandbox_prompt_char_budget", "integer", 0, "budget.promptCharLimit", "2000000", "Maximum prompt characters per sandbox budget window; zero means unlimited."),
    ConfigVar("SANDBOX_ESTIMATED_TOKEN_BUDGET", "sandbox_estimated_token_budget", "integer", 0, "budget.estimatedTokenLimit", 750000, "Maximum estimated prompt tokens per sandbox budget window; zero means unlimited."),
    ConfigVar("BUDGET_ESTIMATED_CHARS_PER_TOKEN", "budget_estimated_chars_per_token", "integer", 4, "budget.estimatedCharsPerToken", 4, "Prompt-character to token estimate used for budget accounting."),
    ConfigVar("SANDBOX_BUDGET_WINDOW_SECONDS", "sandbox_budget_window_seconds", "integer", 86400, "budget.windowSeconds", 86400, "Sandbox budget window in seconds."),
    ConfigVar("SANDBOX_BUDGET_REDIS_URL", "sandbox_budget_redis_url", "url", "redis://budget-redis.budget.svc.cluster.local:6379/0", "budget.redisUrl", "redis://budget-redis.budget.svc.cluster.local:6379/0", "Redis URL for shared budget counters."),
    ConfigVar("SANDBOX_BUDGET_REDIS_TIMEOUT_SECONDS", "sandbox_budget_redis_timeout_seconds", "float", 0.5, "budget.redisTimeoutSeconds", "0.5", "Redis budget operation timeout in seconds."),
    ConfigVar("SANDBOX_BUDGET_KEY_PREFIX", "sandbox_budget_key_prefix", "string", "private-ai-platform-kit:sandbox-budget", "budget.keyPrefix", "private-ai-platform-kit:sandbox-budget", "Redis key prefix for sandbox budget counters."),
    ConfigVar("AUDIT_LOG_ENABLED", "audit_log_enabled", "boolean", True, "traceability.auditLogEnabled", True, "Whether redacted audit logging is enabled."),
    ConfigVar("AUDIT_CHAIN_STORE_BACKEND", "audit_chain_store_backend", "string", "memory", "traceability.auditChainStore.backend", "memory", "Where the audit chain head is persisted so a restart continues the chain of chains.", allowed_values=("memory", "file", "redis")),
    ConfigVar("AUDIT_CHAIN_STORE_PATH", "audit_chain_store_path", "string", "/var/lib/inference-gateway/audit-chain-head.json", "traceability.auditChainStore.path", "/var/lib/inference-gateway/audit-chain-head.json", "Head file path for the file chain-store backend."),
    ConfigVar("AUDIT_CHAIN_STORE_REDIS_URL", "audit_chain_store_redis_url", "url", "redis://budget-redis.budget.svc.cluster.local:6379/0", "traceability.auditChainStore.redisUrl", "redis://budget-redis.budget.svc.cluster.local:6379/0", "Redis URL for the redis chain-store backend."),
    ConfigVar("AUDIT_CHAIN_STORE_KEY", "audit_chain_store_key", "string", "inference-gateway:audit-chain-head", "traceability.auditChainStore.key", "inference-gateway:audit-chain-head", "Redis key holding the audit chain head."),
    ConfigVar("AUDIT_CHAIN_STORE_TIMEOUT_SECONDS", "audit_chain_store_timeout_seconds", "float", 0.5, "traceability.auditChainStore.timeoutSeconds", "0.5", "Chain-store operation timeout in seconds."),
    ConfigVar("AUDIT_CHAIN_PERSIST_INTERVAL_SECONDS", "audit_chain_persist_interval_seconds", "float", 5.0, "traceability.auditChainStore.persistIntervalSeconds", "5", "How often the chain head is written out, keeping the write off the request path."),
    ConfigVar("AGENT_RECEIPTS_ENABLED", "agent_receipts_enabled", "boolean", False, "receipts.enabled", False, "Whether the agent-action receipt intake (POST /v1/receipts) is served."),
    ConfigVar("AGENT_RECEIPT_MAX_FIELD_CHARS", "agent_receipt_max_field_chars", "integer", 256, "receipts.maxFieldChars", 256, "Maximum length of a free-text field on an agent-action receipt."),
    ConfigVar("DEFAULT_SANDBOX_ID", "default_sandbox_id", "string", "local-lab", "traceability.defaultSandboxId", "local-lab", "Fallback sandbox ID when callers do not send X-Sandbox-ID."),
    ConfigVar("API_KEY_AUTH_ENABLED", "api_key_auth_enabled", "boolean", False, "auth.enabled", False, "Whether business endpoints require API key or bearer-token auth."),
    ConfigVar("API_KEY_HEADER", "api_key_header", "string", "X-API-Key", "auth.apiKeyHeader", "X-API-Key", "Header name accepted for API key authentication."),
    ConfigVar("API_KEY_SHA256S", "api_key_sha256s", "csv", [], "auth.apiKeyHashes", [], "Comma-separated SHA-256 API key hashes.", sensitive=True),
    ConfigVar("API_KEY_RECORDS_PATH", "api_key_records_path", "path", None, "auth.keyRecordsPath", "", "Optional path to a JSON/YAML API-key records file (per-key scopes, expiry, sandbox binding, budget overrides); a malformed file fails closed at startup. Mount it via auth.keyRecords.existingSecret."),
    ConfigVar("JWT_AUTH_ENABLED", "jwt_auth_enabled", "boolean", False, "auth.jwt.enabled", False, "Whether JWT bearer-token validation is accepted beside API key auth."),
    ConfigVar("JWT_JWKS_URL", "jwt_jwks_url", "url", "", "auth.jwt.jwksUrl", "", "JWKS URL used by optional JWT validation."),
    ConfigVar("JWT_ISSUER", "jwt_issuer", "string", "", "auth.jwt.issuer", "", "Expected JWT issuer when set."),
    ConfigVar("JWT_AUDIENCE", "jwt_audience", "string", "", "auth.jwt.audience", "", "Expected JWT audience when set."),
    ConfigVar("JWT_REQUIRED_SCOPES", "jwt_required_scopes", "csv", [], "auth.jwt.requiredScopes", [], "Scopes required in JWT scope or scp claims."),
    ConfigVar("JWT_CACHE_SECONDS", "jwt_cache_seconds", "integer", 300, "auth.jwt.cacheSeconds", 300, "JWKS cache TTL in seconds."),
    ConfigVar("JWT_TENANT_CLAIM", "jwt_tenant_claim", "string", "", "auth.jwt.tenantClaim", "", "JWT claim that binds the sandbox id; mismatched or missing values are rejected. Empty disables binding."),
    ConfigVar("OTEL_TRACING_ENABLED", "otel_tracing_enabled", "boolean", False, "observability.tracing.enabled", False, "Whether OpenTelemetry span export is enabled."),
    ConfigVar("OTEL_EXPORTER_OTLP_ENDPOINT", "otel_exporter_otlp_endpoint", "url", "", "observability.tracing.otlpEndpoint", "", "OTLP/HTTP endpoint for span export when tracing is enabled."),
    ConfigVar("OTEL_SERVICE_NAME", "otel_service_name", "string", "inference-gateway", "observability.tracing.serviceName", "inference-gateway", "Service name attribute attached to exported spans."),
    ConfigVar("BATCH_API_ENABLED", "batch_api_enabled", "boolean", False, "batch.enabled", False, "Whether the asynchronous OpenAI Files + Batch API (/v1/files, /v1/batches) is served. Off by default; needs an object store and a Redis-backed store + queue plus the batch-processor worker."),
    ConfigVar("BATCH_OBJECT_STORE_BACKEND", "batch_object_store_backend", "string", "filesystem", "batch.objectStore.backend", "filesystem", "Object-store backend for batch JSONL blobs.", allowed_values=("filesystem", "memory", "s3")),
    ConfigVar("BATCH_OBJECT_STORE_ROOT", "batch_object_store_root", "string", "/var/lib/inference-gateway/batch", "batch.objectStore.root", "/var/lib/inference-gateway/batch", "Root directory for the filesystem object-store backend (single-node/local)."),
    ConfigVar("BATCH_S3_ENDPOINT_URL", "batch_s3_endpoint_url", "url", "", "batch.objectStore.s3.endpointUrl", "", "S3/MinIO endpoint URL for the s3 object-store backend."),
    ConfigVar("BATCH_S3_BUCKET", "batch_s3_bucket", "string", "", "batch.objectStore.s3.bucket", "", "S3 bucket holding batch blobs when the object-store backend is s3."),
    ConfigVar("BATCH_S3_REGION", "batch_s3_region", "string", "us-east-1", "batch.objectStore.s3.region", "us-east-1", "S3 region used for request signing."),
    ConfigVar("BATCH_S3_ACCESS_KEY_ID", "batch_s3_access_key_id", "string", "", "batch.objectStore.s3.accessKeyId", "", "S3 access key id for the s3 object-store backend."),
    ConfigVar("BATCH_S3_SECRET_ACCESS_KEY", "batch_s3_secret_access_key", "string", "", "batch.objectStore.s3.secretAccessKey", "", "S3 secret access key; source it from batch.objectStore.s3.existingSecret in production.", sensitive=True),
    ConfigVar("BATCH_STORE_BACKEND", "batch_store_backend", "string", "memory", "batch.store.backend", "memory", "Backend for batch file/job records and the work queue.", allowed_values=("memory", "redis")),
    ConfigVar("BATCH_REDIS_URL", "batch_redis_url", "url", "redis://budget-redis.budget.svc.cluster.local:6379/2", "batch.store.redisUrl", "redis://budget-redis.budget.svc.cluster.local:6379/2", "Redis URL for the shared batch store + queue when backend is redis."),
    ConfigVar("BATCH_REDIS_TIMEOUT_SECONDS", "batch_redis_timeout_seconds", "float", 0.5, "batch.store.redisTimeoutSeconds", "0.5", "Redis batch-store operation timeout in seconds."),
    ConfigVar("BATCH_KEY_PREFIX", "batch_key_prefix", "string", "private-ai-platform-kit:batch", "batch.store.keyPrefix", "private-ai-platform-kit:batch", "Redis key prefix for batch records, indexes, and the work queue."),
    ConfigVar("BATCH_MAX_FILE_BYTES", "batch_max_file_bytes", "integer", 104857600, "batch.maxFileBytes", 104857600, "Maximum size in bytes of an uploaded batch input file."),
    ConfigVar("BATCH_MAX_REQUESTS_PER_BATCH", "batch_max_requests_per_batch", "integer", 50000, "batch.maxRequestsPerBatch", 50000, "Maximum number of request lines in a batch input file."),
    ConfigVar("BATCH_COMPLETION_WINDOW", "batch_completion_window", "string", "24h", "batch.completionWindow", "24h", "Completion window honored as an expiry bound (e.g. 24h, 30m)."),
    ConfigVar("BATCH_RETENTION_SECONDS", "batch_retention_seconds", "integer", 604800, "batch.retentionSeconds", 604800, "Retention horizon in seconds for completed batches and their files before GC."),
    ConfigVar("RESPONSES_STORE_ENABLED", "responses_store_enabled", "boolean", False, "responses.store.enabled", False, "Whether the stateful Responses API surface (store=true, previous_response_id, GET/DELETE/input_items) is served. Off by default; storing responses persists raw conversation content (ADR 0012)."),
    ConfigVar("RESPONSES_STORE_BACKEND", "responses_store_backend", "string", "memory", "responses.store.backend", "memory", "Backend for stored Responses objects.", allowed_values=("memory", "redis")),
    ConfigVar("RESPONSES_REDIS_URL", "responses_redis_url", "url", "redis://budget-redis.budget.svc.cluster.local:6379/3", "responses.store.redisUrl", "redis://budget-redis.budget.svc.cluster.local:6379/3", "Redis URL for the shared response store when backend is redis."),
    ConfigVar("RESPONSES_REDIS_TIMEOUT_SECONDS", "responses_redis_timeout_seconds", "float", 0.5, "responses.store.redisTimeoutSeconds", "0.5", "Redis response-store operation timeout in seconds."),
    ConfigVar("RESPONSES_KEY_PREFIX", "responses_key_prefix", "string", "private-ai-platform-kit:responses", "responses.store.keyPrefix", "private-ai-platform-kit:responses", "Redis key prefix for stored responses."),
    ConfigVar("RESPONSES_RETENTION_SECONDS", "responses_retention_seconds", "integer", 86400, "responses.store.retentionSeconds", 86400, "Retention TTL in seconds for stored responses before they expire."),
    ConfigVar("ADMIN_CONSOLE_ENABLED", "admin_console_enabled", "boolean", False, "adminConsole.enabled", False, "Whether the read-only admin console is served at /console (ADR 0013). Off by default."),
)

RAG_VARS = (
    ConfigVar("RAG_DOCUMENT_DIR", "document_dir", "path", "/knowledge", "knowledge.mountPath", "/knowledge", "Directory containing mounted knowledge documents."),
    ConfigVar("RAG_RETRIEVAL_BACKEND", "retrieval_backend", "string", "lexical", "retrieval.backend", "lexical", "Retrieval backend used by the service.", aliases=("RETRIEVAL_BACKEND",), allowed_values=("lexical", "qdrant")),
    ConfigVar("DEFAULT_SANDBOX_ID", "default_sandbox_id", "string", "local-lab", "traceability.defaultSandboxId", "local-lab", "Fallback sandbox ID when callers do not send X-Sandbox-ID."),
    ConfigVar("AUDIT_LOG_ENABLED", "audit_log_enabled", "boolean", True, "traceability.auditLogEnabled", True, "Whether redacted RAG audit logging is enabled."),
    ConfigVar("RAG_AUDIT_CHAIN_STORE_BACKEND", "audit_chain_store_backend", "string", "memory", "traceability.auditChainStore.backend", "memory", "Where the retrieval-receipt chain head is persisted across restarts.", allowed_values=("memory", "file", "redis")),
    ConfigVar("RAG_AUDIT_CHAIN_STORE_PATH", "audit_chain_store_path", "string", "/var/lib/rag-service/audit-chain-head.json", "traceability.auditChainStore.path", "/var/lib/rag-service/audit-chain-head.json", "Head file path for the file chain-store backend."),
    ConfigVar("RAG_AUDIT_CHAIN_STORE_REDIS_URL", "audit_chain_store_redis_url", "url", "redis://budget-redis.budget.svc.cluster.local:6379/0", "traceability.auditChainStore.redisUrl", "redis://budget-redis.budget.svc.cluster.local:6379/0", "Redis URL for the redis chain-store backend."),
    ConfigVar("RAG_AUDIT_CHAIN_STORE_KEY", "audit_chain_store_key", "string", "rag-service:audit-chain-head", "traceability.auditChainStore.key", "rag-service:audit-chain-head", "Redis key holding the retrieval-receipt chain head."),
    ConfigVar("RAG_AUDIT_CHAIN_STORE_TIMEOUT_SECONDS", "audit_chain_store_timeout_seconds", "float", 0.5, "traceability.auditChainStore.timeoutSeconds", "0.5", "Chain-store operation timeout in seconds."),
    ConfigVar("RAG_AUDIT_CHAIN_PERSIST_INTERVAL_SECONDS", "audit_chain_persist_interval_seconds", "float", 5.0, "traceability.auditChainStore.persistIntervalSeconds", "5", "How often the chain head is written out, keeping the write off the retrieval path."),
    ConfigVar("MAX_QUERY_CHARS", "max_query_chars", "integer", 2048, "retrieval.maxQueryChars", 2048, "Maximum accepted query length in characters."),
    ConfigVar("MAX_REQUEST_BODY_BYTES", "max_request_body_bytes", "integer", 1048576, "retrieval.maxRequestBodyBytes", 1048576, "Maximum RAG request body size before parsing."),
    ConfigVar("DEFAULT_TOP_K", "default_top_k", "integer", 3, "retrieval.defaultTopK", 3, "Default number of retrieval results."),
    ConfigVar("MAX_TOP_K", "max_top_k", "integer", 8, "retrieval.maxTopK", 8, "Maximum allowed retrieval result count."),
    ConfigVar("MAX_CONTEXT_CHARS", "max_context_chars", "integer", 6000, "retrieval.maxContextChars", 6000, "Maximum context characters returned to callers."),
    ConfigVar("QDRANT_URL", "vector_store_url", "url", "", "retrieval.vectorStore.url", "", "Qdrant base URL when vector retrieval is enabled.", aliases=("VECTOR_STORE_URL",)),
    ConfigVar("QDRANT_COLLECTION", "vector_collection", "string", "private-ai-platform-kit", "retrieval.vectorStore.collection", "private-ai-platform-kit", "Qdrant collection name.", aliases=("VECTOR_COLLECTION",)),
    ConfigVar("QDRANT_COLLECTION_VERSION", "vector_collection_version", "string", "v1", "retrieval.vectorStore.collectionVersion", "v1", "Logical Qdrant collection version used for ingestion payloads and retrieval filters.", aliases=("VECTOR_COLLECTION_VERSION",)),
    ConfigVar("QDRANT_TIMEOUT_SECONDS", "vector_timeout_seconds", "float", 1.0, "retrieval.vectorStore.timeoutSeconds", 1.0, "Qdrant request timeout in seconds.", aliases=("VECTOR_TIMEOUT_SECONDS",)),
    ConfigVar("QDRANT_VECTOR_DIMENSIONS", "vector_dimensions", "integer", 384, "retrieval.vectorStore.dimensions", 384, "Vector dimension count expected by Qdrant."),
    ConfigVar("QDRANT_BOOTSTRAP_FROM_KNOWLEDGE", "vector_bootstrap_enabled", "boolean", True, "retrieval.vectorStore.bootstrapFromKnowledge", True, "Whether knowledge documents are bootstrapped into Qdrant on first query."),
    ConfigVar("RAG_RETRIEVAL_CANDIDATE_MULTIPLIER", "retrieval_candidate_multiplier", "integer", 4, "retrieval.candidateMultiplier", 4, "Dense candidates over-fetched (x topK) before the hybrid lexical rerank."),
    ConfigVar("RAG_RETRIEVAL_LEXICAL_WEIGHT", "retrieval_lexical_weight", "float", 0.5, "retrieval.lexicalWeight", 0.5, "Weight (0..1) of lexical query-term overlap blended with the dense score."),
    ConfigVar("RAG_RETRIEVAL_ALLOWED_CLASSIFICATIONS", "retrieval_allowed_classifications", "csv", [], "retrieval.allowedClassifications", [], "Access-scope retrieval to documents whose classification is in this list; empty returns all."),
    ConfigVar("RAG_RETRIEVAL_TENANT_ISOLATION_ENABLED", "retrieval_tenant_isolation_enabled", "boolean", True, "retrieval.tenantIsolation.enabled", False, "Whether retrieval is scoped to the caller's tenant (X-Sandbox-ID) via the tenant field. Enabled by default (app default true) and enforced on both the Qdrant and lexical backends; fails closed when the tenant is missing or not explicitly asserted. The bundled local lexical profile sets the chart value false (single-tenant). The header is client-asserted under the shared API key, so this is tenant scoping for trusted callers (e.g. gateway-fronted traffic with JWT tenant binding), not an identity control against a caller that lies about its tenant."),
    ConfigVar("RAG_RETRIEVAL_TENANT_FIELD", "retrieval_tenant_field", "string", "owner", "retrieval.tenantIsolation.field", "owner", "Qdrant payload field (stamped at ingest) matched against the caller's tenant id."),
    ConfigVar("RAG_RERANKER_PROVIDER", "reranker_provider", "string", "none", "retrieval.reranker.provider", "none", "Optional cross-encoder reranker second stage.", allowed_values=("none", "openai-compatible")),
    ConfigVar("RAG_RERANKER_BASE_URL", "reranker_base_url", "url", "", "retrieval.reranker.baseUrl", "", "Base URL of the /rerank endpoint when the reranker provider is openai-compatible."),
    ConfigVar("RAG_RERANKER_MODEL", "reranker_model", "string", "", "retrieval.reranker.model", "", "Reranker model identifier sent to the /rerank endpoint."),
    ConfigVar("RAG_RERANKER_TIMEOUT_SECONDS", "reranker_timeout_seconds", "float", 2.0, "retrieval.reranker.timeoutSeconds", 2.0, "Reranker request timeout in seconds."),
    ConfigVar("RAG_EMBEDDING_PROVIDER", "embedding_provider", "string", "hash", "retrieval.embedding.provider", "hash", "Embedding provider for Qdrant vectors.", allowed_values=("hash", "openai-compatible")),
    ConfigVar("RAG_EMBEDDING_BASE_URL", "embedding_base_url", "url", "", "retrieval.embedding.baseUrl", "", "OpenAI-compatible embedding endpoint base URL."),
    ConfigVar("RAG_EMBEDDING_MODEL", "embedding_model", "string", "hash-text-v1", "retrieval.embedding.model", "hash-text-v1", "Embedding model identifier sent to the provider."),
    ConfigVar("RAG_SOURCE_MANIFEST", "rag_source_manifest", "path", None, "sourceManifest.path", "", "Optional path to a platform.ai/v1alpha1 RagSourceManifest YAML file."),
    ConfigVar("API_KEY_AUTH_ENABLED", "api_key_auth_enabled", "boolean", False, "auth.enabled", False, "Whether business endpoints require API key or bearer-token auth."),
    ConfigVar("API_KEY_HEADER", "api_key_header", "string", "X-API-Key", "auth.apiKeyHeader", "X-API-Key", "Header name accepted for API key authentication."),
    ConfigVar("API_KEY_SHA256S", "api_key_sha256s", "csv", [], "auth.apiKeyHashes", [], "Comma-separated SHA-256 API key hashes.", sensitive=True),
    ConfigVar("RAG_JWT_ENABLED", "jwt_enabled", "boolean", False, "auth.jwt.enabled", False, "Whether the RAG service verifies its own audience-bound bearer token and derives the tenant from the verified claim (overriding X-Sandbox-ID). Off by default: header-trust as before."),
    ConfigVar("RAG_JWT_JWKS_URL", "jwt_jwks_url", "url", "", "auth.jwt.jwksUrl", "", "JWKS URL used to fetch signing keys when JWT verification is enabled."),
    ConfigVar("RAG_JWT_ISSUER", "jwt_issuer", "string", "", "auth.jwt.issuer", "", "Expected JWT issuer when set."),
    ConfigVar("RAG_JWT_AUDIENCE", "jwt_audience", "string", "", "auth.jwt.audience", "", "Expected JWT audience when set."),
    ConfigVar("RAG_JWT_TENANT_CLAIM", "jwt_tenant_claim", "string", "sandbox_id", "auth.jwt.tenantClaim", "sandbox_id", "Verified JWT claim the tenant/sandbox id is derived from; a header that contradicts it is rejected (403), a missing/empty claim is rejected."),
    ConfigVar("RAG_JWT_CACHE_SECONDS", "jwt_cache_seconds", "integer", 300, "auth.jwt.cacheSeconds", 300, "JWKS cache TTL in seconds."),
    ConfigVar("RAG_JWT_REQUEST_TIMEOUT_SECONDS", "jwt_request_timeout_seconds", "float", 5.0, "auth.jwt.requestTimeoutSeconds", 5.0, "JWKS fetch timeout in seconds (capped at 10)."),
    ConfigVar("RAG_JWT_REQUIRED", "jwt_required", "boolean", False, "auth.jwt.required", False, "Whether a valid JWT is mandatory on business endpoints (fail closed: no/invalid token -> 401)."),
    ConfigVar("OTEL_TRACING_ENABLED", "otel_tracing_enabled", "boolean", False, "observability.tracing.enabled", False, "Whether OpenTelemetry span export is enabled."),
    ConfigVar("OTEL_EXPORTER_OTLP_ENDPOINT", "otel_exporter_otlp_endpoint", "url", "", "observability.tracing.otlpEndpoint", "", "OTLP/HTTP endpoint for span export when tracing is enabled."),
    ConfigVar("OTEL_SERVICE_NAME", "otel_service_name", "string", "rag-service", "observability.tracing.serviceName", "rag-service", "Service name attribute attached to exported spans."),
)

CONTRACTS = {
    "inference-gateway": ServiceContract(
        service="inference-gateway",
        service_dir=ROOT / "src/inference-gateway",
        chart_dir=ROOT / "deploy/charts/inference-gateway",
        snapshot=ROOT / "platform/config-contracts/inference-gateway.config.json",
        variables=GATEWAY_VARS,
    ),
    "rag-service": ServiceContract(
        service="rag-service",
        service_dir=ROOT / "src/rag-service",
        chart_dir=ROOT / "deploy/charts/rag-service",
        snapshot=ROOT / "platform/config-contracts/rag-service.config.json",
        variables=RAG_VARS,
    ),
}


def require(errors: list[str], condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def nested(mapping: dict[str, Any], path: str) -> Any:
    current: Any = mapping
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(path)
        current = current[part]
    return current


def extract_settings_env_names(path: Path) -> set[str]:
    names: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for arg in node.args[:1]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if re.fullmatch(r"[A-Z][A-Z0-9_]+", arg.value):
                    names.add(arg.value)
            elif isinstance(arg, ast.Tuple):
                for item in arg.elts:
                    if (
                        isinstance(item, ast.Constant)
                        and isinstance(item.value, str)
                        and re.fullmatch(r"[A-Z][A-Z0-9_]+", item.value)
                    ):
                        names.add(item.value)
    return names


def extract_helm_env_names(path: Path) -> set[str]:
    return set(ENV_NAME_PATTERN.findall(path.read_text(encoding="utf-8")))


def contract_payload(contract: ServiceContract) -> dict[str, Any]:
    return {
        "contract_version": contract.contract_version,
        "service": contract.service,
        "service_settings": (contract.service_dir / contract.settings_file).relative_to(ROOT).as_posix(),
        "helm_chart": contract.chart_dir.relative_to(ROOT).as_posix(),
        "environment": [
            {
                "name": variable.name,
                "field": variable.field,
                "type": variable.kind,
                "app_default": variable.app_default,
                "chart_value": variable.chart_value,
                "chart_default": variable.chart_default,
                "aliases": list(variable.aliases),
                "sensitive": variable.sensitive,
                "allowed_values": list(variable.allowed_values),
                "description": variable.description,
            }
            for variable in sorted(contract.variables, key=lambda item: item.name)
        ],
    }


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def validate_contract(contract: ServiceContract) -> list[str]:
    errors: list[str] = []
    settings_path = contract.service_dir / contract.settings_file
    template_path = contract.chart_dir / contract.deployment_template
    values_path = contract.chart_dir / contract.values_file
    require(errors, settings_path.exists(), f"{contract.service}: settings file missing")
    require(errors, template_path.exists(), f"{contract.service}: deployment template missing")
    require(errors, values_path.exists(), f"{contract.service}: chart values missing")
    if errors:
        return errors

    primary_names = {item.name for item in contract.variables}
    accepted_names = set(primary_names)
    for item in contract.variables:
        accepted_names.update(item.aliases)

    settings_env_names = extract_settings_env_names(settings_path)
    helm_env_names = extract_helm_env_names(template_path)
    require(
        errors,
        settings_env_names <= accepted_names,
        f"{contract.service}: settings env names missing from contract: {sorted(settings_env_names - accepted_names)}",
    )
    require(
        errors,
        primary_names <= helm_env_names,
        f"{contract.service}: Helm env block missing names: {sorted(primary_names - helm_env_names)}",
    )
    require(
        errors,
        helm_env_names <= primary_names,
        f"{contract.service}: Helm env block has uncontracted names: {sorted(helm_env_names - primary_names)}",
    )

    values = yaml.safe_load(values_path.read_text(encoding="utf-8")) or {}
    template = template_path.read_text(encoding="utf-8")
    for variable in contract.variables:
        try:
            chart_default = nested(values, variable.chart_value)
        except KeyError:
            errors.append(f"{contract.service}: {variable.name} chart path missing: {variable.chart_value}")
            continue
        require(
            errors,
            chart_default == variable.chart_default,
            f"{contract.service}: {variable.name} chart default drifted from contract",
        )
        if variable.sensitive:
            require(
                errors,
                "secretKeyRef" in template and ".Values.auth.existingSecret.name" in template,
                f"{contract.service}: {variable.name} must support secretKeyRef sourcing",
            )
    return errors


def selected_contracts(service: str | None) -> dict[str, ServiceContract]:
    if service:
        return {service: CONTRACTS[service]}
    return CONTRACTS


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate and validate service runtime configuration contracts.",
    )
    parser.add_argument("--check", action="store_true", help="Validate generated contracts and committed snapshots.")
    parser.add_argument("--write", action="store_true", help="Write committed configuration snapshots.")
    parser.add_argument("--service", choices=sorted(CONTRACTS), help="Limit checks to one service.")
    args = parser.parse_args()
    if not args.check and not args.write:
        args.check = True

    errors: list[str] = []
    wrote: list[str] = []
    for service, contract in selected_contracts(args.service).items():
        errors.extend(validate_contract(contract))
        rendered = canonical_json(contract_payload(contract))
        if args.write:
            contract.snapshot.parent.mkdir(parents=True, exist_ok=True)
            contract.snapshot.write_text(rendered, encoding="utf-8")
            wrote.append(contract.snapshot.relative_to(ROOT).as_posix())
        if args.check:
            if not contract.snapshot.exists():
                errors.append(f"{service}: missing snapshot {contract.snapshot.relative_to(ROOT)}")
                continue
            if contract.snapshot.read_text(encoding="utf-8") != rendered:
                errors.append(
                    f"{service}: configuration snapshot is stale; run scripts/config-contract.py --write"
                )

    if errors:
        print("config contract check failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    if wrote:
        print("wrote config contracts:")
        for path in wrote:
            print(f"- {path}")
    else:
        print("config contracts ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
