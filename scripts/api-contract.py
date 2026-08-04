#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}


@dataclass(frozen=True)
class RouteContract:
    method: str
    request_schema: str | None = None


@dataclass(frozen=True)
class ServiceContract:
    service_dir: Path
    title: str
    version: str
    snapshot: Path
    routes: dict[str, RouteContract]
    protected_paths: frozenset[str]
    required_schemas: dict[str, dict[str, set[str]]]


CONTRACTS = {
    "inference-gateway": ServiceContract(
        service_dir=ROOT / "src/inference-gateway",
        title="Private AI Platform Kit Inference Gateway",
        version="0.28.0",
        snapshot=ROOT / "platform/api-contracts/inference-gateway.openapi.json",
        routes={
            "/healthz": RouteContract("get"),
            "/readyz": RouteContract("get"),
            "/metrics": RouteContract("get"),
            "/v1/sandbox/budget": RouteContract("get"),
            "/v1/usage": RouteContract("get"),
            "/v1/models": RouteContract("get"),
            "/v1/chat/completions": RouteContract(
                "post",
                request_schema="ChatCompletionRequest",
            ),
            "/v1/completions": RouteContract(
                "post",
                request_schema="CompletionRequest",
            ),
            # Native Anthropic Messages API, translated to/from the OpenAI chat shape and
            # routed through the same governance path (auth-required like the other routes).
            "/v1/messages": RouteContract(
                "post",
                request_schema="MessagesRequest",
            ),
            # OpenAI Responses API (stateless subset), translated to/from the OpenAI chat
            # shape and routed through the same governance path (auth-required).
            "/v1/responses": RouteContract(
                "post",
                request_schema="ResponsesRequest",
            ),
            "/v1/embeddings": RouteContract(
                "post",
                request_schema="EmbeddingsRequest",
            ),
            "/v1/moderations": RouteContract(
                "post",
                request_schema="ModerationRequest",
            ),
            # /v1/batch-inference is the canonical synchronous-batch route (its own BatchRequest
            # schema, deliberately named apart from the OpenAI async file-batch API).
            "/v1/batch-inference": RouteContract(
                "post",
                request_schema="BatchRequest",
            ),
            # Asynchronous OpenAI Files + Batch API (ADR 0011). Every route is auth-required and
            # tenant-scoped; multi-method paths contract their primary method here.
            "/v1/files": RouteContract("post"),
            "/v1/files/{file_id}": RouteContract("get"),
            "/v1/files/{file_id}/content": RouteContract("get"),
            "/v1/batches": RouteContract("post", request_schema="CreateBatchRequest"),
            "/v1/batches/{batch_id}": RouteContract("get"),
            "/v1/batches/{batch_id}/cancel": RouteContract("post"),
            # Stateful Responses API (ADR 0012): retrieve/input-items/delete on stored responses.
            "/v1/responses/{response_id}": RouteContract("get"),
            "/v1/responses/{response_id}/input_items": RouteContract("get"),
            # Agent-action receipt intake (ADR 0014): auth-required and tenant-scoped like
            # every other write surface; records claims, enforces nothing.
            "/v1/receipts": RouteContract("post", request_schema="ReceiptRequest"),
        },
        protected_paths=frozenset(
            {
                "/v1/models",
                "/v1/sandbox/budget",
                "/v1/usage",
                "/v1/chat/completions",
                "/v1/completions",
                "/v1/messages",
                "/v1/responses",
                "/v1/embeddings",
                "/v1/moderations",
                "/v1/batch-inference",
                "/v1/files",
                "/v1/files/{file_id}",
                "/v1/files/{file_id}/content",
                "/v1/batches",
                "/v1/batches/{batch_id}",
                "/v1/batches/{batch_id}/cancel",
                "/v1/responses/{response_id}",
                "/v1/responses/{response_id}/input_items",
                "/v1/receipts",
            }
        ),
        required_schemas={
            "ChatCompletionRequest": {
                "properties": {
                    "model",
                    "messages",
                    "temperature",
                    "max_tokens",
                    "stream",
                    "tools",
                    "tool_choice",
                    "functions",
                    "function_call",
                    "response_format",
                },
                "required": {"messages"},
            },
            "Message": {
                # content is optional: an assistant turn may carry only tool_calls.
                "properties": {"role", "content", "name", "tool_calls", "tool_call_id"},
                "required": {"role"},
            },
            "CompletionRequest": {
                "properties": {"model", "prompt", "max_tokens", "stream"},
                "required": {"prompt"},
            },
            # Anthropic Messages request. ``max_tokens`` is required by Anthropic (and drives
            # the admission cap), so it is a required field alongside ``messages``.
            "MessagesRequest": {
                "properties": {
                    "model",
                    "messages",
                    "system",
                    "max_tokens",
                    "temperature",
                    "top_p",
                    "stop_sequences",
                    "tools",
                    "tool_choice",
                    "stream",
                    "metadata",
                },
                "required": {"messages", "max_tokens"},
            },
            # OpenAI Responses request (stateless subset). ``input`` is required; ``store`` and
            # ``previous_response_id`` are modelled so the handler can reject the stateful path.
            "ResponsesRequest": {
                "properties": {
                    "model",
                    "input",
                    "instructions",
                    "max_output_tokens",
                    "temperature",
                    "top_p",
                    "tools",
                    "tool_choice",
                    "stream",
                    "metadata",
                    "store",
                    "previous_response_id",
                },
                "required": {"input"},
            },
            "EmbeddingsRequest": {
                "properties": {"model", "input"},
                "required": {"input"},
            },
            "ModerationRequest": {
                "properties": {"model", "input"},
                "required": {"input"},
            },
            "BatchRequest": {
                "properties": {"requests"},
                "required": {"requests"},
            },
        },
    ),
    "rag-service": ServiceContract(
        service_dir=ROOT / "src/rag-service",
        title="Private AI Platform Kit RAG Service",
        version="0.28.0",
        snapshot=ROOT / "platform/api-contracts/rag-service.openapi.json",
        routes={
            "/healthz": RouteContract("get"),
            "/metrics": RouteContract("get"),
            "/v1/rag/documents": RouteContract("get"),
            "/v1/rag/query": RouteContract("post", request_schema="RagQueryRequest"),
        },
        protected_paths=frozenset({"/v1/rag/documents", "/v1/rag/query"}),
        required_schemas={
            "RagQueryRequest": {
                "properties": {
                    "query",
                    "top_k",
                    "include_context",
                    "include_messages",
                    "max_context_chars",
                },
                "required": {"query"},
            },
        },
    ),
}

SDK_METHOD_ROUTES = {
    "chat": "/v1/chat/completions",
    "chat_stream": "/v1/chat/completions",
    "embeddings": "/v1/embeddings",
    "moderations": "/v1/moderations",
    "batch": "/v1/batch-inference",
    "upload_batch_file": "/v1/files",
    "get_file": "/v1/files/",
    "get_file_content": "/v1/files/",
    "list_files": "/v1/files",
    "delete_file": "/v1/files/",
    "create_batch": "/v1/batches",
    "get_batch": "/v1/batches/",
    "cancel_batch": "/v1/batches/",
    "list_batches": "/v1/batches",
    "create_response": "/v1/responses",
    "get_response": "/v1/responses/",
    "delete_response": "/v1/responses/",
    "response_input_items": "/v1/responses/",
    "models": "/v1/models",
    "usage": "/v1/usage",
    "sandbox_budget": "/v1/sandbox/budget",
}


def require(errors: list[str], condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


@contextmanager
def patched_env(updates: dict[str, str]) -> Iterator[None]:
    previous = {name: os.environ.get(name) for name in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def service_import_path(path: Path) -> Iterator[None]:
    sys.path.insert(0, str(path))
    try:
        yield
    finally:
        sys.path = [item for item in sys.path if item != str(path)]
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]


def load_gateway_schema(contract: ServiceContract) -> dict[str, Any]:
    env = {
        "RUNTIME_BACKEND": "ollama",
        "OLLAMA_BASE_URL": "http://ollama.local:11434",
        "VLLM_BASE_URL": "http://vllm.local:8000",
        "MODEL_ID": "qwen3.5:0.8b",
        "ALLOWED_MODELS": "qwen3.5:0.8b",
        "API_KEY_AUTH_ENABLED": "false",
    }
    with patched_env(env), service_import_path(contract.service_dir):
        from app.main import create_app
        from app.settings import Settings

        app = create_app(
            Settings(
                runtime_backend="ollama",
                ollama_base_url="http://ollama.local:11434",
                vllm_base_url="http://vllm.local:8000",
                model_id="qwen3.5:0.8b",
                request_timeout_seconds=30.0,
                allowed_models=("qwen3.5:0.8b",),
            )
        )
        return app.openapi()


def load_rag_schema(contract: ServiceContract) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        document_dir = Path(tmp)
        (document_dir / "contract.md").write_text(
            "# API Contract\nPrivate platform knowledge document.",
            encoding="utf-8",
        )
        env = {
            "RAG_DOCUMENT_DIR": str(document_dir),
            "RAG_RETRIEVAL_BACKEND": "lexical",
            "API_KEY_AUTH_ENABLED": "false",
        }
        with patched_env(env), service_import_path(contract.service_dir):
            from app.main import create_app
            from app.settings import Settings

            app = create_app(Settings(document_dir=document_dir))
            return app.openapi()


def load_schema(service: str, contract: ServiceContract) -> dict[str, Any]:
    if service == "inference-gateway":
        return load_gateway_schema(contract)
    if service == "rag-service":
        return load_rag_schema(contract)
    raise ValueError(f"unknown service: {service}")


def component_schemas(schema: dict[str, Any]) -> dict[str, Any]:
    components = schema.get("components", {})
    schemas = components.get("schemas", {})
    return schemas if isinstance(schemas, dict) else {}


def security_scheme_names(operation: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for item in operation.get("security", []):
        if isinstance(item, dict):
            names.update(item)
    return names


def schema_ref_name(value: dict[str, Any]) -> str | None:
    ref = value.get("$ref")
    if isinstance(ref, str):
        return ref.rsplit("/", 1)[-1]
    for key in ("allOf", "anyOf", "oneOf"):
        for item in value.get(key, []):
            if isinstance(item, dict):
                found = schema_ref_name(item)
                if found:
                    return found
    return None


def request_schema_name(operation: dict[str, Any]) -> str | None:
    request_body = operation.get("requestBody", {})
    json_content = request_body.get("content", {}).get("application/json", {})
    schema = json_content.get("schema", {})
    if isinstance(schema, dict):
        return schema_ref_name(schema)
    return None


def operation_items(schema: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    items: list[tuple[str, str, dict[str, Any]]] = []
    for path, operations in schema.get("paths", {}).items():
        if not isinstance(operations, dict):
            continue
        for method, operation in operations.items():
            if method.lower() in HTTP_METHODS and isinstance(operation, dict):
                items.append((path, method.lower(), operation))
    return items


def validate_schema(service: str, contract: ServiceContract, schema: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    info = schema.get("info", {})
    paths = schema.get("paths", {})
    schemas = component_schemas(schema)
    security_schemes = schema.get("components", {}).get("securitySchemes", {})

    require(errors, str(schema.get("openapi", "")).startswith("3."), f"{service}: OpenAPI version must be 3.x")
    require(errors, info.get("title") == contract.title, f"{service}: unexpected OpenAPI title")
    require(errors, info.get("version") == contract.version, f"{service}: unexpected OpenAPI version")
    require(errors, set(paths) == set(contract.routes), f"{service}: public paths changed: {sorted(paths)}")
    require(errors, "BearerAuth" in security_schemes, f"{service}: missing BearerAuth security scheme")
    require(errors, "ApiKeyAuth" in security_schemes, f"{service}: missing ApiKeyAuth security scheme")

    operation_ids: list[str] = []
    for path, route in contract.routes.items():
        operation = paths.get(path, {}).get(route.method, {})
        require(errors, bool(operation), f"{service}: missing {route.method.upper()} {path}")
        if not operation:
            continue
        operation_ids.append(str(operation.get("operationId", "")))
        require(errors, bool(operation.get("summary")), f"{service}: {route.method.upper()} {path} missing summary")
        require(errors, bool(operation.get("tags")), f"{service}: {route.method.upper()} {path} missing tags")
        require(
            errors,
            "200" in operation.get("responses", {}),
            f"{service}: {route.method.upper()} {path} missing 200 response",
        )
        if route.request_schema:
            require(
                errors,
                request_schema_name(operation) == route.request_schema,
                f"{service}: {route.method.upper()} {path} must use {route.request_schema}",
            )
        names = security_scheme_names(operation)
        if path in contract.protected_paths:
            require(
                errors,
                {"BearerAuth", "ApiKeyAuth"} <= names,
                f"{service}: {route.method.upper()} {path} must declare bearer and API-key auth",
            )
        else:
            require(errors, not names, f"{service}: {route.method.upper()} {path} should not require auth")

    duplicates = {item for item in operation_ids if operation_ids.count(item) > 1}
    require(errors, not duplicates, f"{service}: duplicate operation IDs: {sorted(duplicates)}")

    for name, expected in contract.required_schemas.items():
        found = schemas.get(name)
        require(errors, isinstance(found, dict), f"{service}: missing component schema {name}")
        if not isinstance(found, dict):
            continue
        properties = set(found.get("properties", {}))
        required = set(found.get("required", []))
        require(
            errors,
            expected["properties"] <= properties,
            f"{service}: schema {name} missing properties {sorted(expected['properties'] - properties)}",
        )
        require(
            errors,
            expected["required"] <= required,
            f"{service}: schema {name} missing required fields {sorted(expected['required'] - required)}",
        )

    message = schemas.get("Message")
    if isinstance(message, dict):
        role_schema = message.get("properties", {}).get("role", {})
        enum_values = set(role_schema.get("enum", []))
        require(
            errors,
            {"system", "user", "assistant", "tool"} <= enum_values,
            f"{service}: Message.role enum missing expected roles",
        )

    rag_query = schemas.get("RagQueryRequest")
    if isinstance(rag_query, dict):
        query_schema = rag_query.get("properties", {}).get("query", {})
        require(
            errors, query_schema.get("minLength") == 1, f"{service}: RagQueryRequest.query must enforce minLength 1"
        )

    return errors


def canonical_json(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True) + "\n"


def validate_python_sdk() -> list[str]:
    """Keep the first-party client's method surface aligned with gateway paths."""
    errors: list[str] = []
    sdk_path = ROOT / "sdk/python/ai_platform_client.py"
    tree = ast.parse(sdk_path.read_text(encoding="utf-8"), filename=str(sdk_path))
    gateway_class = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "GatewayClient"),
        None,
    )
    require(errors, gateway_class is not None, "python SDK: GatewayClient class is missing")
    if gateway_class is None:
        return errors
    methods = {node.name: node for node in gateway_class.body if isinstance(node, ast.FunctionDef)}
    for method, route in SDK_METHOD_ROUTES.items():
        node = methods.get(method)
        require(errors, node is not None, f"python SDK: GatewayClient.{method} is missing")
        if node is None:
            continue
        literals = {
            value.value for value in ast.walk(node) if isinstance(value, ast.Constant) and isinstance(value.value, str)
        }
        require(
            errors,
            any(route in literal for literal in literals),
            f"python SDK: GatewayClient.{method} must target {route}",
        )
    return errors


def selected_contracts(service: str | None) -> dict[str, ServiceContract]:
    if service:
        return {service: CONTRACTS[service]}
    return CONTRACTS


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate and validate versioned OpenAPI contracts for platform services.",
    )
    parser.add_argument("--check", action="store_true", help="Validate generated contracts and committed snapshots.")
    parser.add_argument("--write", action="store_true", help="Write committed OpenAPI snapshots.")
    parser.add_argument("--service", choices=sorted(CONTRACTS), help="Limit checks to one service.")
    args = parser.parse_args()
    if not args.check and not args.write:
        args.check = True

    errors: list[str] = []
    wrote: list[str] = []
    for service, contract in selected_contracts(args.service).items():
        schema = load_schema(service, contract)
        errors.extend(validate_schema(service, contract, schema))
        rendered = canonical_json(schema)
        if args.write:
            contract.snapshot.parent.mkdir(parents=True, exist_ok=True)
            contract.snapshot.write_text(rendered, encoding="utf-8")
            wrote.append(contract.snapshot.relative_to(ROOT).as_posix())
        if args.check:
            if not contract.snapshot.exists():
                errors.append(f"{service}: missing snapshot {contract.snapshot.relative_to(ROOT)}")
                continue
            current = contract.snapshot.read_text(encoding="utf-8")
            if current != rendered:
                errors.append(f"{service}: OpenAPI snapshot is stale; run scripts/api-contract.py --write")
    if args.service in (None, "inference-gateway"):
        errors.extend(validate_python_sdk())

    if errors:
        print("api contract check failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    if wrote:
        print("wrote api contracts:")
        for path in wrote:
            print(f"- {path}")
    else:
        print("api contracts ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
