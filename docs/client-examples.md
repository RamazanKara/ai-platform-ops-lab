# Client API examples

The gateway implements a documented subset of the OpenAI API. Clients that use those
routes can point their base URL at the gateway and send the platform headers. Check the
[OpenAPI contract](https://github.com/RamazanKara/private-ai-platform-kit/blob/main/platform/api-contracts/inference-gateway.openapi.json)
and [scope](scope-and-non-goals.md) before assuming that an SDK feature is supported.
These examples use the local port-forward address (`make local-up` then port-forward the
gateway service); replace it with your ingress host in a customer cluster.

The `model` id must be on the active profile's allowlist: the local lab allows
`qwen2.5:0.5b`, while the customer profiles default to `qwen3.5:0.8b` (Ollama) and
`Qwen/Qwen3-Coder-Next` (vLLM). Streaming is admitted by default in every shipped
profile (`admission.allowStreaming: true`); a deployment that requires end-of-stream-only
guardrail enforcement sets it `false`. Examples below that
use a customer model are marked accordingly.

All business endpoints accept:

- `Authorization: Bearer <api-key-or-jwt>` (or the `X-API-Key` header) when auth is enabled
- `X-Sandbox-ID: <sandbox>`: the tenant/sandbox the request is attributed to
- `X-Request-ID` and W3C `traceparent`: optional, echoed back for tracing

Responses echo `X-Request-ID`, `X-Sandbox-ID`, and `traceparent` for correlation. When
sandbox budgets are enabled, `/v1/chat/completions` (including streaming) and
`/v1/embeddings` responses also carry the OpenAI-style budget headers that agent
frameworks parse to pace themselves:

- `x-ratelimit-limit-requests` / `x-ratelimit-remaining-requests`: the sandbox request
  budget and what remains of it in the current window
- `x-ratelimit-limit-tokens` / `x-ratelimit-remaining-tokens`: the estimated-token budget
  and what remains of it, floored at zero

Each pair is present only when the corresponding limit is configured (greater than zero);
cache hits (`X-Cache: HIT`) consume no budget and omit them. See the
[budget controls runbook](https://github.com/RamazanKara/private-ai-platform-kit/blob/main/runbooks/budget-controls.md) for sizing and triage.

## curl

```bash
GATEWAY=http://127.0.0.1:8080
KEY=local-development-only

# Chat completion
curl -fsS "$GATEWAY/v1/chat/completions" \
  -H "Authorization: Bearer $KEY" -H "X-Sandbox-ID: demo" \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"hello"}]}'

# Embeddings
curl -fsS "$GATEWAY/v1/embeddings" \
  -H "Authorization: Bearer $KEY" -H "X-Sandbox-ID: demo" \
  -H 'Content-Type: application/json' \
  -d '{"input":"embed this text"}'

# Legacy text completion (prompt-based; governed like chat, non-streaming)
curl -fsS "$GATEWAY/v1/completions" \
  -H "Authorization: Bearer $KEY" -H "X-Sandbox-ID: demo" \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Write a haiku about the sea.","max_tokens":64}'

# Native Anthropic Messages API (governed like chat, non-streaming). max_tokens is required.
curl -fsS "$GATEWAY/v1/messages" \
  -H "Authorization: Bearer $KEY" -H "X-Sandbox-ID: demo" \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5:0.5b","max_tokens":64,"messages":[{"role":"user","content":"hello"}]}'

# OpenAI Responses API (synchronous; optional tenant-scoped server-side state).
curl -fsS "$GATEWAY/v1/responses" \
  -H "Authorization: Bearer $KEY" -H "X-Sandbox-ID: demo" \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5:0.5b","input":"hello","max_output_tokens":64}'

# Moderations (content policy classification)
curl -fsS "$GATEWAY/v1/moderations" \
  -H "Authorization: Bearer $KEY" -H "X-Sandbox-ID: demo" \
  -H 'Content-Type: application/json' \
  -d '{"input":"text to classify"}'
```

## Moderations taxonomy (not OpenAI's harm categories)

`/v1/moderations` is OpenAI-compatible in shape but classifies against the platform
*governance* taxonomy, not OpenAI's harm taxonomy. The response carries a top-level
`"taxonomy": "governance"` marker and the `categories`/`category_scores` keys are
`credential`, `pii`, and `blocked_terms` (rule-based credential/PII/denylist detection),
not `hate`, `violence`, `self-harm`, etc. Branch on the `taxonomy` field if you consume
both a real OpenAI moderation endpoint and this one. A semantic toxicity classifier can be
layered behind the same endpoint later without changing callers.

## Legacy `/v1/completions` (prompt-based)

The gateway also exposes the pre-chat `/v1/completions` endpoint for tools that still use a
`prompt` (a string or list of strings) instead of `messages`. It runs through the **same**
governance path as chat (model allowlist, admission limits, prompt secret policy, sandbox
budget, output guardrail, and audit), so legacy-completion traffic is not a control bypass.
Streaming is **not** supported on `/v1/completions` in this release (send `stream: false`,
or use `/v1/chat/completions` for streaming); a streaming request is rejected with a clear
`streaming_not_supported` error. Prefer `/v1/chat/completions` for new integrations.

## Native Anthropic Messages API (`/v1/messages`)

The gateway exposes a **native** Anthropic-shaped `/v1/messages` endpoint, so Anthropic-SDK
and Claude-style agents can point at the gateway directly, with no translation sidecar required
for the common case. The Anthropic request and response are translated to and from the
internal OpenAI chat shape and run through the **same** governance path as chat (model
allowlist, admission limits, prompt secret policy, sandbox budget, output guardrail, and
audit), so `/v1/messages` traffic is not a control bypass. Anthropic **requires**
`max_tokens`; a request that omits it is rejected, and the value is also enforced against the
gateway's completion-token cap. The response is an Anthropic `Message` (`type: "message"`,
`content` blocks, `stop_reason`, `usage.input_tokens`/`output_tokens`).

```bash
curl -fsS "$GATEWAY/v1/messages" \
  -H "Authorization: Bearer $KEY" -H "X-Sandbox-ID: demo" \
  -H 'Content-Type: application/json' \
  -d '{
        "model": "qwen2.5:0.5b",
        "max_tokens": 128,
        "system": "You are a terse assistant.",
        "messages": [{"role": "user", "content": "Give me one fact about the sea."}]
      }'
```

Translation is faithful but pragmatic: message and `system` text are exact; Anthropic tool
definitions (`name`/`description`/`input_schema`) and `tool_use`/`tool_result` content blocks
are mapped to their closest OpenAI equivalents on a best-effort basis. For Anthropic-shaped
features the native endpoint does not cover, such as content blocks with no OpenAI
equivalent, the translation-sidecar approach below remains available.

### Streaming

`stream: true` returns the Anthropic event sequence (`message_start`, then a
`content_block_start` / `content_block_delta` / `content_block_stop` run per content block,
then `message_delta` with the stop reason and usage, then `message_stop`), translated from
the same governed chat stream `/v1/chat/completions` uses. This is what an interactive
Claude-style agent needs: the Anthropic SDKs stream by default, so before this the native
endpoint served scripts rather than agents.

```python
import anthropic

client = anthropic.Anthropic(base_url=GATEWAY, api_key=KEY)
with client.messages.stream(
    model="qwen2.5:0.5b",
    max_tokens=128,
    messages=[{"role": "user", "content": "Give me one fact about the sea."}],
) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)
    final = stream.get_final_message()
```

Streaming obeys the same `admission.allowStreaming` toggle as chat, so one setting governs
both surfaces. Two behaviors are worth knowing:

- **Token counts arrive at the end.** OpenAI-compatible runtimes only report usage on a
  terminal event, which is after `message_start` has to be sent, so `message_start` carries
  zeros and the true counts ride on the final `message_delta`. The Anthropic SDKs reconcile
  the final usage from there, so `get_final_message().usage` is correct.
- **The output guardrail flags rather than redacts.** Streamed bytes are already committed to
  the wire, so the guardrail scans the assistant text at end-of-stream and records a finding.
  Use non-streaming requests where the guardrail must be able to block.

Reasoning and thinking deltas cannot leak through this path: the translator reads only
`delta.content` and `delta.tool_calls`, so anything else a runtime streams has no route into
the Anthropic events.

`make sdk-conformance` drives the real `anthropic` and `openai` clients against the gateway
and asserts the vendors' own parsers accept it, including the streamed tool-call
reconstruction. That, rather than the OpenAPI snapshot, is the compatibility evidence.

## OpenAI Responses API (`/v1/responses`)

The gateway exposes the synchronous OpenAI **Responses API**, so tooling built
on `client.responses.create(...)` can point at the gateway directly. The Responses request and
response are translated to and from the internal OpenAI chat shape and run through the **same**
governance path as chat (model allowlist, admission limits, prompt secret policy, sandbox
budget, output guardrail, and audit), so `/v1/responses` traffic is not a control bypass.
`input` accepts a plain string or an array of input items/messages; `instructions` is prepended
as a system message; `max_output_tokens` maps to the gateway's completion-token cap and is
enforced against it. The response is a Responses object (`object: "response"`, `status`,
`output[]` with `output_text` content parts and any `function_call` items,
`usage.input_tokens`/`output_tokens`/`total_tokens`).

```bash
curl -fsS "$GATEWAY/v1/responses" \
  -H "Authorization: Bearer $KEY" -H "X-Sandbox-ID: demo" \
  -H 'Content-Type: application/json' \
  -d '{
        "model": "qwen2.5:0.5b",
        "max_output_tokens": 128,
        "instructions": "You are a terse assistant.",
        "input": "Give me one fact about the sea."
      }'
```

Server-side state is opt-in and off by default because it persists raw conversation content.
Set `RESPONSES_STORE_ENABLED=true` and use the Redis backend for multi-replica deployments;
then `store: true`, `previous_response_id`, `GET`/`DELETE /v1/responses/{id}`, and
`GET /v1/responses/{id}/input_items` are tenant-scoped and TTL-bounded. When state is disabled,
those requests fail explicitly with `stateful_not_supported`. Streaming is **not** supported on `/v1/responses` (send
`stream: false`, or use `/v1/chat/completions` for OpenAI-shaped streaming); a streaming request
is rejected with a clear `streaming_not_supported` error. Assistant `tool_calls` are mapped to
`function_call` output items (`name`, `arguments`, `call_id`) on a best-effort basis, and a
`length` finish maps to `status: "incomplete"` with `incomplete_details.reason:
"max_output_tokens"`.

## Error responses

Every gateway error body is OpenAI-shaped:

```json
{
  "error": {
    "message": "requested completion tokens is 50; limit is 10",
    "type": "invalid_request_error",
    "code": "max_tokens_too_large",
    "request_id": "…",
    "sandbox_id": "demo"
  },
  "detail": { "…": "original detail, preserved this release" }
}
```

`error.type` follows the OpenAI taxonomy (`invalid_request_error`,
`authentication_error`, `permission_error`, `rate_limit_error`, `api_error`, …) so an
OpenAI SDK's typed exceptions (e.g. `RateLimitError`) map correctly. `error.code` carries
the gateway's machine reason. The legacy `detail` object is preserved alongside for one
release while callers migrate; do not depend on it long-term. Pydantic request-validation
errors (HTTP 422) keep FastAPI's default `{"detail": [...]}` shape.

## Python (openai SDK)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8080/v1",
    api_key="local-development-only",
    default_headers={"X-Sandbox-ID": "demo"},
)

# Tool-calling (the flagship coding-agent path) is forwarded to the runtime.
resp = client.chat.completions.create(
    model="qwen2.5:0.5b",
    messages=[{"role": "user", "content": "What is the weather in Berlin?"}],
    tools=[{
        "type": "function",
        "function": {
            "name": "get_weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
        },
    }],
)
print(resp.choices[0].message)

# Streaming (admitted by default in every shipped profile)
for chunk in client.chat.completions.create(
    model="qwen3.5:0.8b",
    messages=[{"role": "user", "content": "stream a haiku"}],
    stream=True,
):
    print(chunk.choices[0].delta.content or "", end="")
```

## Python (plain httpx, no SDK)

```python
import httpx

gateway = "http://127.0.0.1:8080"
headers = {"Authorization": "Bearer local-development-only", "X-Sandbox-ID": "demo"}

r = httpx.post(
    f"{gateway}/v1/chat/completions",
    headers=headers,
    json={"messages": [{"role": "user", "content": "hello"}]},
    timeout=120,
)
r.raise_for_status()
print(r.json()["choices"][0]["message"]["content"])
```

## Python (first-party client)

The kit ships a minimal, retry-aware first-party client (`sdk/python`, packaged as
`private-ai-platform-kit-client`) for scripts that do not want the full `openai` dependency:

```bash
python -m pip install private-ai-platform-kit-client
```

```python
from ai_platform_client import GatewayClient

with GatewayClient("http://127.0.0.1:8080", api_key="local-development-only", sandbox_id="demo") as gw:
    print(gw.chat([{"role": "user", "content": "hello"}])["choices"][0]["message"]["content"])
    for delta in gw.chat_stream([{"role": "user", "content": "stream a haiku"}]):
        print(delta, end="")
```

## Agent & coding frameworks (drop-in)

Because the gateway is OpenAI-compatible, agent and coding frameworks work by pointing their
OpenAI base URL at the gateway and adding the `X-Sandbox-ID` header. Always route framework traffic
through the gateway (not the runtime directly) so auth, model allowlists, budgets, guardrails, and
audit apply.

### LangChain

```python
from langchain_openai import ChatOpenAI

llm = ChatOpenAI(
    base_url="http://127.0.0.1:8080/v1",
    api_key="local-development-only",
    model="qwen2.5:0.5b",
    default_headers={"X-Sandbox-ID": "demo"},
)
print(llm.invoke("hello").content)
```

### LlamaIndex

```python
from llama_index.llms.openai_like import OpenAILike

llm = OpenAILike(
    api_base="http://127.0.0.1:8080/v1",
    api_key="local-development-only",
    model="qwen2.5:0.5b",
    is_chat_model=True,
    default_headers={"X-Sandbox-ID": "demo"},
)
print(llm.complete("hello"))
```

### Aider (coding agent)

Assumes a customer vLLM profile serving `Qwen/Qwen3-Coder-Next`; point the base URL at
your gateway host.

```bash
export OPENAI_API_BASE=http://127.0.0.1:8080/v1
export OPENAI_API_KEY=local-development-only
# Aider forwards no custom header, so bind the sandbox with a JWT tenant claim (auth.jwt.tenantClaim)
# or run Aider from inside an agent-workspace namespace whose egress sets X-Sandbox-ID at the proxy.
aider --model openai/Qwen/Qwen3-Coder-Next
```

### Continue / Cline (VS Code)

Point the assistant at an OpenAI-compatible provider with `apiBase:
http://<gateway-host>/v1`, the API key, and a `requestOptions.headers` entry setting
`X-Sandbox-ID`. The gateway's `/v1/models` lists the approved models to configure.

> Frameworks that cannot set a custom header should bind the sandbox with a JWT tenant claim
> (`auth.jwt.tenantClaim`) so per-sandbox budgets and attribution cannot be spoofed.

## Anthropic SDK / Claude-style agents (translation sidecar as an alternative)

The gateway now exposes a native Anthropic `/v1/messages` endpoint (see above), which is the
preferred path for Anthropic-SDK and Claude-style agents. A translation **sidecar** remains a
supported **alternative** for Anthropic-shaped features the native endpoint does not yet cover
(most notably streaming, and content blocks with no OpenAI equivalent). For example, a
[LiteLLM](https://docs.litellm.ai/) proxy that exposes an Anthropic-shaped `/v1/messages`
endpoint and forwards to the gateway's `/v1/chat/completions`. The sidecar does the
Anthropic-to-OpenAI request/response translation; the gateway still applies auth, model
allowlists, budgets, guardrails, and audit. Point the sidecar's upstream at the gateway and
set the platform headers on the forwarded request.

Minimal LiteLLM config sketch (`config.yaml`), with the gateway as the OpenAI-compatible
upstream:

```yaml
model_list:
  - model_name: claude-shim            # the name Anthropic-SDK clients request
    litellm_params:
      model: openai/qwen2.5:0.5b       # an allowlisted gateway model
      api_base: http://<gateway-host>/v1
      api_key: os.environ/GATEWAY_API_KEY
      extra_headers:
        X-Sandbox-ID: demo             # or bind the sandbox via a JWT tenant claim
```

```bash
litellm --config config.yaml   # serves an Anthropic-compatible /v1/messages
```

Anthropic-SDK clients then point `base_url` at the sidecar (rather than the gateway's native
`/v1/messages`) only when they need Anthropic behavior the native endpoint does not yet cover:
this is a translation shim, and features without an OpenAI chat-completions equivalent are
limited by what the sidecar can map. See [Scope and non-goals](scope-and-non-goals.md) for the
exact list of protocol surfaces the gateway does and does not implement.
