#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
VALID_STATUSES = {"proposed", "approved", "deprecated", "blocked"}
VALID_RUNTIMES = {"ollama", "vllm"}
VALID_ACCELERATORS = {"cpu", "nvidia", "amd"}
VALID_RISK_TIERS = {"low", "medium", "high"}
VALID_DATA_CLASSES = {"public", "internal", "confidential", "restricted"}


@dataclass(frozen=True)
class CatalogModel:
    model_id: str
    runtime: str
    status: str
    owner: str
    stage: str
    accelerators: list[str]
    max_prompt_chars: int
    max_completion_tokens: int
    promotion_request: str


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text()) or {}


def nested(mapping: Any, *keys: str, default: Any = None) -> Any:
    current = mapping
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return current if current is not None else default


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def require(errors: list[str], condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def catalog_models(catalog: dict[str, Any], errors: list[str]) -> dict[str, dict[str, Any]]:
    require(errors, catalog.get("apiVersion") == "platform.ai/v1alpha1", "model catalog apiVersion must be platform.ai/v1alpha1")
    require(errors, catalog.get("kind") == "ModelCatalog", "model catalog kind must be ModelCatalog")
    models = nested(catalog, "spec", "models", default=[])
    require(errors, isinstance(models, list) and bool(models), "model catalog must define at least one model")
    by_id: dict[str, dict[str, Any]] = {}
    for index, model in enumerate(models):
        if not isinstance(model, dict):
            errors.append(f"model entry {index} must be a mapping")
            continue
        model_id = str(model.get("id", ""))
        require(errors, bool(model_id), f"model entry {index} must define id")
        if model_id in by_id:
            errors.append(f"duplicate model id {model_id}")
        by_id[model_id] = model
    return by_id


def validate_model_entry(model_id: str, model: dict[str, Any], errors: list[str]) -> None:
    runtime = model.get("runtime")
    status = model.get("status")
    accelerators = model.get("accelerators")
    require(errors, runtime in VALID_RUNTIMES, f"{model_id}: runtime must be one of {sorted(VALID_RUNTIMES)}")
    require(errors, status in VALID_STATUSES, f"{model_id}: status must be one of {sorted(VALID_STATUSES)}")
    require(errors, bool(model.get("owner")), f"{model_id}: owner is required")
    require(errors, bool(model.get("stage")), f"{model_id}: stage is required")
    require(errors, model.get("riskTier") in VALID_RISK_TIERS, f"{model_id}: riskTier must be one of {sorted(VALID_RISK_TIERS)}")
    require(errors, model.get("dataClassification") in VALID_DATA_CLASSES, f"{model_id}: dataClassification must be one of {sorted(VALID_DATA_CLASSES)}")
    require(errors, bool(model.get("license")), f"{model_id}: license is required")
    require(errors, bool(model.get("source")), f"{model_id}: source is required")
    require(errors, isinstance(accelerators, list) and bool(accelerators), f"{model_id}: accelerators must be a non-empty list")
    if isinstance(accelerators, list):
        invalid = sorted(set(accelerators) - VALID_ACCELERATORS)
        require(errors, not invalid, f"{model_id}: accelerators contain unsupported values {invalid}")
    for field in ("contextWindow", "maxPromptChars", "maxCompletionTokens"):
        value = model.get(field)
        require(errors, isinstance(value, int) and value > 0, f"{model_id}: {field} must be a positive integer")
    # The budget estimate's divisor is reviewed data, not a global constant: a coding
    # model tokenizes source at roughly three characters per token where prose runs near
    # four, and a non-Latin script closer to one. Bounded to catch a transposed digit.
    chars_per_token = model.get("estimatedCharsPerToken")
    require(
        errors,
        isinstance(chars_per_token, int) and not isinstance(chars_per_token, bool) and 1 <= chars_per_token <= 16,
        f"{model_id}: estimatedCharsPerToken must be an integer between 1 and 16",
    )
    request_path = model.get("promotionRequest")
    if status == "approved":
        require(errors, bool(request_path), f"{model_id}: approved models must reference promotionRequest")
    if request_path:
        require(errors, (ROOT / str(request_path)).exists(), f"{model_id}: promotionRequest {request_path} must exist")
    card_path = model.get("modelCard")
    if status == "approved":
        require(errors, bool(card_path), f"{model_id}: approved models must reference a modelCard datasheet")
    if card_path:
        require(errors, (ROOT / str(card_path)).exists(), f"{model_id}: modelCard {card_path} must exist")


def allowed_models_for_environment(environment: str) -> tuple[list[str], dict[str, Any]]:
    values_path = ROOT / f"deploy/clusters/{environment}/values/inference-gateway.yaml"
    values = load_yaml(values_path)
    return list(nested(values, "runtime", "allowedModels", default=[])), values


def validate_allowlists(models: dict[str, dict[str, Any]], errors: list[str]) -> dict[str, list[str]]:
    allowlists: dict[str, list[str]] = {}
    for environment in ("local", "customer"):
        allowed, values = allowed_models_for_environment(environment)
        allowlists[environment] = allowed
        require(errors, bool(allowed), f"{environment}: runtime.allowedModels must not be empty")
        max_prompt = nested(values, "admission", "maxPromptChars", default=0)
        max_completion = nested(values, "admission", "maxCompletionTokens", default=0)
        for model_id in allowed:
            model = models.get(model_id)
            require(errors, model is not None, f"{environment}: allowed model {model_id} is missing from model catalog")
            if not model:
                continue
            require(errors, model.get("status") == "approved", f"{environment}: allowed model {model_id} must have status approved")
            require(
                errors,
                int(model.get("maxPromptChars", 0)) <= int(max_prompt),
                f"{environment}: allowed model {model_id} maxPromptChars exceeds gateway admission.maxPromptChars",
            )
            require(
                errors,
                int(model.get("maxCompletionTokens", 0)) <= int(max_completion),
                f"{environment}: allowed model {model_id} maxCompletionTokens exceeds gateway admission.maxCompletionTokens",
            )
    return allowlists


def routing_models_for_allowlist(models: dict[str, dict[str, Any]], allowlist: list[str]) -> list[dict[str, Any]]:
    routing: list[dict[str, Any]] = []
    for model_id in allowlist:
        model = models.get(model_id)
        if not model:
            continue
        # estimatedCharsPerToken is carried through so the divisor the gateway meters with
        # is provably the one the catalog reviewed, rather than a number that drifted in a
        # values file where nobody would notice it.
        routing.append(
            {
                "id": model_id,
                "backend": str(model.get("runtime")),
                "estimatedCharsPerToken": model.get("estimatedCharsPerToken"),
            }
        )
    return routing


def validate_gateway_routing_policies(
    models: dict[str, dict[str, Any]],
    allowlists: dict[str, list[str]],
    errors: list[str],
) -> None:
    for environment, allowlist in sorted(allowlists.items()):
        values_path = ROOT / f"deploy/clusters/{environment}/values/inference-gateway.yaml"
        values = load_yaml(values_path)
        routing = nested(values, "routing", "policy", default={})
        expected = routing_models_for_allowlist(models, allowlist)
        require(
            errors,
            nested(routing, "enabled", default=False) is True,
            f"{environment}: routing.policy.enabled must be true so ModelRoutingPolicy is mounted",
        )
        require(
            errors,
            nested(routing, "models", default=[]) == expected,
            f"{environment}: routing.policy.models must match approved model catalog runtimes",
        )


def validate_vllm_profiles(models: dict[str, dict[str, Any]], errors: list[str]) -> None:
    for profile, expected_accelerator in (("vllm", "nvidia"), ("vllm-nvidia", "nvidia"), ("vllm-amd", "amd")):
        path = ROOT / f"deploy/clusters/customer/values/{profile}.yaml"
        if not path.exists():
            errors.append(f"customer {profile} values must exist")
            continue
        values = load_yaml(path)
        model_id = nested(values, "model", "name")
        model = models.get(model_id)
        require(errors, model is not None, f"{profile}: model {model_id} is missing from model catalog")
        if model:
            require(errors, model.get("runtime") == "vllm", f"{profile}: model {model_id} must use runtime vllm")
            require(errors, expected_accelerator in model.get("accelerators", []), f"{profile}: model {model_id} must support {expected_accelerator}")
            require(errors, model.get("status") == "approved", f"{profile}: model {model_id} must be approved")


def validate_configmap_matches_catalog(catalog: dict[str, Any], errors: list[str]) -> None:
    configmap_path = ROOT / "platform/model-catalog/k8s/configmap.yaml"
    require(errors, configmap_path.exists(), "model catalog ConfigMap must exist")
    if not configmap_path.exists():
        return
    docs = [doc for doc in yaml.safe_load_all(configmap_path.read_text()) if isinstance(doc, dict)]
    configmaps = [doc for doc in docs if doc.get("kind") == "ConfigMap"]
    require(errors, len(configmaps) == 1, "model catalog ConfigMap must contain one ConfigMap")
    if not configmaps:
        return
    embedded = yaml.safe_load(nested(configmaps[0], "data", "models.yaml", default="")) or {}
    require(errors, embedded == catalog, "model catalog ConfigMap embedded models.yaml must match platform/model-catalog/models.yaml")


def promotion_request_paths() -> list[Path]:
    return sorted((ROOT / "platform/model-catalog/promotion-requests").glob("*.yaml"))


def validate_promotion_requests(models: dict[str, dict[str, Any]], allowlists: dict[str, list[str]], errors: list[str]) -> dict[str, dict[str, Any]]:
    requests: dict[str, dict[str, Any]] = {}
    for path in promotion_request_paths():
        request = load_yaml(path)
        name = nested(request, "metadata", "name", default=path.stem)
        require(errors, request.get("apiVersion") == "platform.ai/v1alpha1", f"{rel(path)}: apiVersion must be platform.ai/v1alpha1")
        require(errors, request.get("kind") == "ModelPromotionRequest", f"{rel(path)}: kind must be ModelPromotionRequest")
        spec = request.get("spec", {})
        model_id = spec.get("modelId")
        requests[str(model_id)] = request
        model = models.get(model_id)
        require(errors, model is not None, f"{rel(path)}: modelId {model_id} is missing from catalog")
        require(errors, spec.get("targetStatus") in VALID_STATUSES, f"{rel(path)}: targetStatus must be valid")
        require(errors, bool(spec.get("requestedBy")), f"{rel(path)}: requestedBy is required")
        approvers = spec.get("approvers")
        require(errors, isinstance(approvers, list) and bool(approvers), f"{rel(path)}: approvers must be a non-empty list")
        require(errors, spec.get("requestedBy") not in (approvers if isinstance(approvers, list) else []), f"{rel(path)}: separation of duties - requestedBy '{spec.get('requestedBy')}' must not also be an approver")
        require(errors, bool(spec.get("businessJustification")), f"{rel(path)}: businessJustification is required")
        if model:
            require(errors, spec.get("targetStatus") == model.get("status"), f"{rel(path)}: targetStatus must match catalog status for {model_id}")
            require(errors, spec.get("runtime") == model.get("runtime"), f"{rel(path)}: runtime must match catalog runtime")
            requested_accels = set(spec.get("accelerators", []))
            catalog_accels = set(model.get("accelerators", []))
            require(errors, requested_accels == catalog_accels, f"{rel(path)}: accelerators must match catalog accelerators")
            require(errors, str(path.relative_to(ROOT)) == model.get("promotionRequest"), f"{model_id}: catalog promotionRequest must point at {rel(path)}")
        evidence = spec.get("evidence", {})
        for field in ("evalSuite", "evalSummary", "loadTestSummary", "securityWorkflow"):
            evidence_path = evidence.get(field)
            require(errors, bool(evidence_path), f"{rel(path)}: evidence.{field} is required")
            if evidence_path:
                require(errors, (ROOT / str(evidence_path)).exists(), f"{rel(path)}: evidence.{field} path {evidence_path} must exist")
        # Eval-model match: the cited eval suite must exercise the promoted model, or the
        # promotion must declare an explicit, justified proxy model (e.g. a CPU-runnable
        # stand-in for a multi-GPU model that cannot be served in CI).
        eval_suite_path = evidence.get("evalSuite")
        if eval_suite_path and (ROOT / str(eval_suite_path)).exists() and model_id:
            suite = load_yaml(ROOT / str(eval_suite_path))
            suite_model = nested(suite, "spec", "model") or suite.get("model")
            proxy = evidence.get("evalModelProxy")
            if proxy:
                require(errors, suite_model == proxy, f"{rel(path)}: evidence.evalModelProxy '{proxy}' must equal the eval suite model '{suite_model}'")
                require(errors, bool(evidence.get("evalProxyJustification")), f"{rel(path)}: evidence.evalProxyJustification is required when evalModelProxy is set")
            else:
                require(errors, suite_model == model_id, f"{rel(path)}: eval suite '{eval_suite_path}' model '{suite_model}' must match promoted model '{model_id}' (or declare evidence.evalModelProxy + evalProxyJustification)")
        rollout = spec.get("rollout", {})
        for values_path in rollout.get("allowedModelFiles", []):
            full_path = ROOT / str(values_path)
            require(errors, full_path.exists(), f"{rel(path)}: rollout allowedModelFile {values_path} must exist")
            if full_path.exists() and model_id:
                values = load_yaml(full_path)
                allowed = nested(values, "runtime", "allowedModels", default=[])
                require(errors, model_id in allowed, f"{rel(path)}: {values_path} must allow {model_id}")
        for environment in rollout.get("environments", []):
            if environment in allowlists and model_id:
                require(errors, model_id in allowlists[environment], f"{rel(path)}: {model_id} must be allowlisted in {environment}")
        for values_path in rollout.get("runtimeValueFiles", []):
            full_path = ROOT / str(values_path)
            require(errors, full_path.exists(), f"{rel(path)}: runtimeValueFile {values_path} must exist")
            if full_path.exists() and model_id:
                values = load_yaml(full_path)
                require(errors, nested(values, "model", "name") == model_id, f"{rel(path)}: {values_path} must deploy {model_id}")
        conditions = spec.get("conditions", {})
        require(errors, conditions.get("promptLogging") == "redacted", f"{rel(path)}: conditions.promptLogging must be redacted")
        require(errors, isinstance(conditions.get("requiresGpu"), bool), f"{rel(path)}: conditions.requiresGpu must be boolean")
        if name:
            require(errors, str(name).replace("_", "-") == str(name), f"{rel(path)}: metadata.name should use DNS-like hyphens")

    for model_id, model in models.items():
        if model.get("status") == "approved":
            require(errors, model_id in requests, f"{model_id}: approved model must have a promotion request")
    return requests


def collect_summary(models: dict[str, dict[str, Any]], allowlists: dict[str, list[str]]) -> list[CatalogModel]:
    rows: list[CatalogModel] = []
    for model_id, model in sorted(models.items()):
        rows.append(
            CatalogModel(
                model_id=model_id,
                runtime=str(model.get("runtime")),
                status=str(model.get("status")),
                owner=str(model.get("owner")),
                stage=str(model.get("stage")),
                accelerators=[str(item) for item in model.get("accelerators", [])],
                max_prompt_chars=int(model.get("maxPromptChars", 0)),
                max_completion_tokens=int(model.get("maxCompletionTokens", 0)),
                promotion_request=str(model.get("promotionRequest", "")),
            )
        )
    return rows


def write_report(output_dir: Path, rows: list[CatalogModel], allowlists: dict[str, list[str]]) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = output_dir / f"model-catalog-{stamp}.json"
    md_path = output_dir / f"model-catalog-{stamp}.md"
    payload = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "models": [asdict(row) for row in rows],
        "allowlists": allowlists,
    }
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    lines = [
        "# Model Catalog Governance Report",
        "",
        f"Generated: `{payload['generated_at']}`",
        "",
        "## Models",
        "",
        "| Model | Status | Runtime | Accelerators | Owner | Stage | Promotion request |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| `{row.model_id}` | {row.status} | {row.runtime} | {', '.join(row.accelerators)} | {row.owner} | {row.stage} | `{row.promotion_request}` |"
        )
    lines.extend(["", "## Gateway Allowlists", "", "| Environment | Models |", "| --- | --- |"])
    for environment, allowed in sorted(allowlists.items()):
        models = ", ".join(f"`{item}`" for item in allowed)
        lines.append(f"| {environment} | {models} |")
    md_path.write_text("\n".join(lines) + "\n")
    return json_path, md_path


def validate() -> tuple[list[str], dict[str, dict[str, Any]], dict[str, list[str]]]:
    errors: list[str] = []
    catalog_path = ROOT / "platform/model-catalog/models.yaml"
    require(errors, catalog_path.exists(), "platform/model-catalog/models.yaml must exist")
    catalog = load_yaml(catalog_path) if catalog_path.exists() else {}
    models = catalog_models(catalog, errors)
    for model_id, model in models.items():
        validate_model_entry(model_id, model, errors)
    allowlists = validate_allowlists(models, errors)
    validate_gateway_routing_policies(models, allowlists, errors)
    validate_vllm_profiles(models, errors)
    validate_configmap_matches_catalog(catalog, errors)
    validate_promotion_requests(models, allowlists, errors)
    return errors, models, allowlists


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and report on Private AI Platform Kit model governance.")
    parser.add_argument("--check", action="store_true", help="Validate the model catalog without writing a report.")
    parser.add_argument("--report", action="store_true", help="Write JSON and Markdown model governance reports.")
    parser.add_argument("--output-dir", default="results/model-catalog")
    args = parser.parse_args()

    errors, models, allowlists = validate()
    if errors:
        print("model catalog validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    if args.report:
        rows = collect_summary(models, allowlists)
        json_path, md_path = write_report(ROOT / args.output_dir, rows, allowlists)
        print(f"wrote {rel(json_path)} and {rel(md_path)}")
        return 0

    if args.check or not args.report:
        request_count = len(promotion_request_paths())
        print(f"model catalog OK: {len(models)} model(s), {request_count} promotion request(s)")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
