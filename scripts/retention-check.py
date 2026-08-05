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
DEFAULT_POLICY = ROOT / "platform/governance/data-retention.yaml"
VALID_CLASSES = {"public", "internal", "confidential", "restricted"}


@dataclass(frozen=True)
class RetentionReport:
    generated_at: str
    policy: str
    classes_checked: list[str]
    errors: list[str]


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text()) or {}


def nested(mapping: Any, *keys: str, default: Any = None) -> Any:
    current = mapping
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return current if current is not None else default


def require(errors: list[str], condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def validate_policy(policy: dict[str, Any], errors: list[str]) -> list[str]:
    require(
        errors,
        policy.get("apiVersion") == "platform.ai/v1alpha1",
        "retention policy apiVersion must be platform.ai/v1alpha1",
    )
    require(errors, policy.get("kind") == "DataRetentionPolicy", "retention policy kind must be DataRetentionPolicy")
    classes = nested(policy, "spec", "classes", default={})
    require(errors, isinstance(classes, dict) and bool(classes), "retention policy must define spec.classes")
    checked: list[str] = []
    for name, item in classes.items():
        checked.append(str(name))
        if not isinstance(item, dict):
            errors.append(f"class {name} must be a mapping")
            continue
        retention_days = item.get("retentionDays")
        require(
            errors,
            isinstance(retention_days, int) and retention_days > 0,
            f"class {name} retentionDays must be a positive integer",
        )
        require(
            errors,
            item.get("classification") in VALID_CLASSES,
            f"class {name} classification must be one of {sorted(VALID_CLASSES)}",
        )
    return checked


def check_audit_redaction(policy: dict[str, Any], errors: list[str]) -> None:
    audit = nested(policy, "spec", "classes", "auditLogs", default={})
    require(errors, audit.get("storesRawPrompt") is False, "audit retention policy must disallow raw prompt storage")
    require(
        errors, audit.get("storesRawCompletion") is False, "audit retention policy must disallow raw completion storage"
    )
    require(errors, audit.get("storesRawQuery") is False, "audit retention policy must disallow raw query storage")
    gateway_audit = (ROOT / "src/inference-gateway/app/audit.py").read_text()
    rag = (ROOT / "src/rag-service/app/main.py").read_text()
    require(
        errors,
        "prompt_sha256" in gateway_audit and "prompt_chars" in gateway_audit,
        "gateway audit logs must keep prompt hashes and lengths",
    )
    require(
        errors,
        "canonical_messages" in gateway_audit and "payload_fingerprint" in gateway_audit,
        "gateway audit logs must derive prompt fingerprints from canonical messages",
    )
    require(errors, "query_sha256" in rag and "query_chars" in rag, "RAG audit logs must keep query hashes and lengths")
    # The receipt construction lives in app/audit.py (write_audit_log) since the route
    # split; main.py is app assembly and no longer builds the event.
    gateway_event = (
        gateway_audit.split("event = {", 1)[1].split("event.update", 1)[0] if "event = {" in gateway_audit else ""
    )
    rag_event = (
        rag.split("event: dict[str, Any] = {", 1)[1].split("if query is not None:", 1)[0]
        if "event: dict[str, Any] = {" in rag
        else ""
    )
    require(errors, bool(gateway_event), "gateway audit event block must be discoverable")
    require(errors, bool(rag_event), "RAG audit event block must be discoverable")
    require(
        errors,
        '"content"' not in gateway_event and '"messages"' not in gateway_event,
        "gateway audit event must not store raw message content",
    )
    require(errors, '"query"' not in rag_event, "RAG audit event must not store raw query text")


def check_generated_evidence(policy: dict[str, Any], errors: list[str]) -> None:
    evidence = nested(policy, "spec", "classes", "generatedEvidence", default={})
    paths = evidence.get("paths", [])
    require(errors, isinstance(paths, list) and bool(paths), "generatedEvidence.paths must be a non-empty list")
    gitignore = (ROOT / ".gitignore").read_text()
    for path in paths:
        target = ROOT / str(path)
        require(errors, target.exists(), f"generated evidence path {path} must exist")
        pattern = f"{path}/*.json"
        html_pattern = f"{path}/*.html"
        md_pattern = f"{path}/*.md"
        generic_result_pattern = str(path).startswith("results/") and any(
            item in gitignore for item in ("results/**/*.json", "results/**/*.html", "results/**/*.md")
        )
        require(
            errors,
            generic_result_pattern or pattern in gitignore or md_pattern in gitignore or html_pattern in gitignore,
            f".gitignore must ignore generated artifacts under {path}",
        )
    sample_pattern = evidence.get("sampleFilePattern")
    require(
        errors,
        sample_pattern == "results/**/sample-*",
        "generatedEvidence.sampleFilePattern must be results/**/sample-*",
    )
    require(errors, "!results/**/sample-*" in gitignore, ".gitignore must retain sample result artifacts by convention")


def check_source_paths(policy: dict[str, Any], class_name: str, errors: list[str]) -> None:
    item = nested(policy, "spec", "classes", class_name, default={})
    for path in item.get("sourcePaths", []):
        require(errors, (ROOT / str(path)).exists(), f"{class_name} source path {path} must exist")


def run_check(policy_path: Path) -> RetentionReport:
    errors: list[str] = []
    policy = load_yaml(policy_path)
    classes = validate_policy(policy, errors)
    check_audit_redaction(policy, errors)
    check_generated_evidence(policy, errors)
    for class_name in ("ragKnowledge", "agentWorkspace", "modelGovernance"):
        check_source_paths(policy, class_name, errors)
    return RetentionReport(
        generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        policy=rel(policy_path),
        classes_checked=classes,
        errors=errors,
    )


def write_json(path: Path, report: RetentionReport) -> None:
    path.write_text(json.dumps(asdict(report), indent=2, sort_keys=True) + "\n")


def write_markdown(path: Path, report: RetentionReport) -> None:
    lines = [
        "# Data Retention Report",
        "",
        f"Generated: `{report.generated_at}`",
        f"Policy: `{report.policy}`",
        "",
        f"Summary: {len(report.classes_checked)} classes checked, {len(report.errors)} errors.",
        "",
        "| Class | Status |",
        "| --- | --- |",
    ]
    for class_name in report.classes_checked:
        lines.append(f"| {class_name} | {'fail' if report.errors else 'pass'} |")
    if report.errors:
        lines.extend(["", "## Errors", ""])
        for error in report.errors:
            lines.append(f"- {error}")
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate Private AI Platform Kit data-retention and privacy governance."
    )
    parser.add_argument("--policy", default=str(DEFAULT_POLICY))
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--output-dir", default="results/retention")
    args = parser.parse_args()

    policy_path = (ROOT / args.policy).resolve() if not Path(args.policy).is_absolute() else Path(args.policy)
    report = run_check(policy_path)
    if args.report:
        output_dir = ROOT / args.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        json_path = output_dir / f"retention-{stamp}.json"
        md_path = output_dir / f"retention-{stamp}.md"
        write_json(json_path, report)
        write_markdown(md_path, report)
        print(f"wrote {rel(json_path)} and {rel(md_path)}")
    if report.errors:
        print("retention check failed:")
        for error in report.errors:
            print(f"- {error}")
        return 1
    print(f"retention OK: {len(report.classes_checked)} class(es) checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
