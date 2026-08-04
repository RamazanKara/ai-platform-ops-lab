#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "platform/network/egress-catalog.yaml"
VALID_STATUSES = {"proposed", "approved", "deprecated", "blocked"}
VALID_DATA_CLASSES = {"public", "internal", "confidential", "restricted"}


@dataclass(frozen=True)
class EgressReference:
    source: str
    environment: str
    cidr: str
    ports: list[int]
    catalog_ref: str
    # The expiry the reference itself declares. It must be present and must agree with the
    # catalog entry, because this is the value the chart stamps onto the rendered
    # NetworkPolicy and the cluster acts on. A reference that silently disagreed with the
    # catalog would let an exception outlive its review inside the cluster.
    expires_on: str = ""


@dataclass(frozen=True)
class EgressReport:
    generated_at: str
    catalog: str
    checked_references: list[EgressReference]
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


def normalize_ports(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    ports: list[int] = []
    for item in value:
        if isinstance(item, int):
            ports.append(item)
    return sorted(ports)


def validate_cidr(value: str, source: str, errors: list[str]) -> ipaddress._BaseNetwork | None:
    try:
        return ipaddress.ip_network(value, strict=False)
    except ValueError:
        errors.append(f"{source}: invalid CIDR {value}")
        return None


def catalog_entries(catalog: dict[str, Any], errors: list[str]) -> dict[str, dict[str, Any]]:
    require(errors, catalog.get("apiVersion") == "platform.ai/v1alpha1", "egress catalog apiVersion must be platform.ai/v1alpha1")
    require(errors, catalog.get("kind") == "ApprovedEgressCatalog", "egress catalog kind must be ApprovedEgressCatalog")
    entries = nested(catalog, "spec", "entries", default=[])
    require(errors, isinstance(entries, list) and bool(entries), "egress catalog must define spec.entries")
    by_id: dict[str, dict[str, Any]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"catalog entry {index} must be a mapping")
            continue
        entry_id = str(entry.get("id", ""))
        require(errors, bool(entry_id), f"catalog entry {index} must define id")
        if entry_id in by_id:
            errors.append(f"duplicate catalog entry id {entry_id}")
        by_id[entry_id] = entry
    return by_id


def validate_catalog_entry(entry_id: str, entry: dict[str, Any], errors: list[str]) -> None:
    require(errors, entry.get("status") in VALID_STATUSES, f"{entry_id}: status must be one of {sorted(VALID_STATUSES)}")
    require(errors, bool(entry.get("owner")), f"{entry_id}: owner is required")
    environments = entry.get("environments")
    require(errors, isinstance(environments, list) and bool(environments), f"{entry_id}: environments must be a non-empty list")
    expires_on = entry.get("expiresOn")
    if expires_on:
        try:
            expiry = date.fromisoformat(str(expires_on))
            require(errors, expiry >= datetime.now(UTC).date(), f"{entry_id}: expiresOn {expires_on} is in the past")
        except ValueError:
            errors.append(f"{entry_id}: expiresOn must use YYYY-MM-DD")
    max_outbound = nested(entry, "dataClassification", "maxOutbound")
    require(errors, max_outbound in VALID_DATA_CLASSES, f"{entry_id}: dataClassification.maxOutbound must be one of {sorted(VALID_DATA_CLASSES)}")
    destinations = entry.get("destinations")
    require(errors, isinstance(destinations, list) and bool(destinations), f"{entry_id}: destinations must be a non-empty list")
    for index, destination in enumerate(destinations or []):
        if not isinstance(destination, dict):
            errors.append(f"{entry_id}: destination {index} must be a mapping")
            continue
        network = validate_cidr(str(destination.get("cidr", "")), f"{entry_id}: destination {index}", errors)
        if network and network.version == 4:
            max_prefix = int(nested(load_yaml(DEFAULT_CATALOG), "spec", "defaults", "maxCidrPrefix", "ipv4", default=24))
            require(errors, network.prefixlen >= max_prefix, f"{entry_id}: CIDR {network} is broader than /{max_prefix}")
        ports = normalize_ports(destination.get("ports"))
        require(errors, bool(ports), f"{entry_id}: destination {index} must define ports")
        for port in ports:
            require(errors, 1 <= port <= 65535, f"{entry_id}: destination {index} has invalid port {port}")
        require(errors, destination.get("protocol", "TCP") == "TCP", f"{entry_id}: destination {index} protocol must be TCP")


def catalog_destinations(entry: dict[str, Any]) -> set[tuple[str, tuple[int, ...]]]:
    destinations: set[tuple[str, tuple[int, ...]]] = set()
    for item in entry.get("destinations", []):
        destinations.add((str(ipaddress.ip_network(str(item["cidr"]), strict=False)), tuple(normalize_ports(item.get("ports")))))
    return destinations


def tenant_onboarding_references(path: Path) -> list[EgressReference]:
    spec = load_yaml(path)
    environment = str(nested(spec, "spec", "tenant", "environment", default="unknown"))
    refs: list[EgressReference] = []
    for item in nested(spec, "spec", "network", "allowedEgressCidrs", default=[]):
        if isinstance(item, dict):
            refs.append(
                EgressReference(
                    source=rel(path),
                    environment=environment,
                    cidr=str(item.get("cidr", "")),
                    ports=normalize_ports(item.get("ports")),
                    catalog_ref=str(item.get("catalogRef", "")),
                    expires_on=str(item.get("expiresOn", "")),
                )
            )
    return refs


def agent_values_references(path: Path) -> list[EgressReference]:
    values = load_yaml(path)
    environment = str(nested(values, "sandbox", "environment", default="unknown"))
    refs: list[EgressReference] = []
    for item in nested(values, "networkPolicy", "allowedEgressCidrs", default=[]):
        if isinstance(item, dict):
            refs.append(
                EgressReference(
                    source=rel(path),
                    environment=environment,
                    cidr=str(item.get("cidr", "")),
                    ports=normalize_ports(item.get("ports")),
                    catalog_ref=str(item.get("catalogRef", "")),
                    expires_on=str(item.get("expiresOn", "")),
                )
            )
    return refs


def collect_references() -> list[EgressReference]:
    refs: list[EgressReference] = []
    for path in sorted((ROOT / "tenants/onboarding").glob("*.yaml")):
        refs.extend(tenant_onboarding_references(path))
    for path in sorted((ROOT / "deploy/clusters").glob("*/values/agent-workspace.yaml")):
        refs.extend(agent_values_references(path))
    return refs


def validate_references(entries: dict[str, dict[str, Any]], refs: list[EgressReference], errors: list[str]) -> None:
    for ref in refs:
        source = f"{ref.source}: {ref.cidr}:{ref.ports}"
        require(errors, bool(ref.catalog_ref), f"{source}: catalogRef is required for external egress")
        network = validate_cidr(ref.cidr, source, errors)
        require(errors, bool(ref.ports), f"{source}: ports must be non-empty")
        if not ref.catalog_ref:
            continue
        entry = entries.get(ref.catalog_ref)
        require(errors, entry is not None, f"{source}: catalogRef {ref.catalog_ref} does not exist")
        if not entry:
            continue
        require(errors, entry.get("status") == "approved", f"{source}: catalogRef {ref.catalog_ref} is not approved")
        require(errors, ref.environment in entry.get("environments", []), f"{source}: environment {ref.environment} is not approved for {ref.catalog_ref}")
        expires_on = entry.get("expiresOn")
        if expires_on:
            require(errors, date.fromisoformat(str(expires_on)) >= datetime.now(UTC).date(), f"{source}: catalogRef {ref.catalog_ref} is expired")
        # The reference must carry the expiry too, and it must match. Only the reference's
        # copy reaches the cluster (the chart renders it onto the NetworkPolicy), so a
        # missing or stale one is an exception that outlives its review where it matters.
        require(errors, bool(ref.expires_on), f"{source}: expiresOn is required and must match catalogRef {ref.catalog_ref}")
        if ref.expires_on and expires_on:
            require(errors, ref.expires_on == str(expires_on), f"{source}: expiresOn {ref.expires_on} does not match catalogRef {ref.catalog_ref} expiresOn {expires_on}")
        if network:
            normalized = (str(network), tuple(sorted(ref.ports)))
            require(errors, normalized in catalog_destinations(entry), f"{source}: CIDR and ports are not listed in catalogRef {ref.catalog_ref}")


def run_check(catalog_path: Path) -> EgressReport:
    errors: list[str] = []
    catalog = load_yaml(catalog_path)
    entries = catalog_entries(catalog, errors)
    for entry_id, entry in entries.items():
        validate_catalog_entry(entry_id, entry, errors)
    refs = collect_references()
    validate_references(entries, refs, errors)
    return EgressReport(
        generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        catalog=rel(catalog_path),
        checked_references=refs,
        errors=errors,
    )


def markdown_escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def write_markdown(path: Path, report: EgressReport) -> None:
    lines = [
        "# Egress Governance Report",
        "",
        f"Generated: `{report.generated_at}`",
        f"Catalog: `{report.catalog}`",
        "",
        f"Summary: {len(report.checked_references)} references checked, {len(report.errors)} errors.",
        "",
        "| Source | Environment | CIDR | Ports | Catalog ref |",
        "| --- | --- | --- | --- | --- |",
    ]
    for ref in report.checked_references:
        lines.append(
            f"| `{markdown_escape(ref.source)}` | {ref.environment} | `{ref.cidr}` | `{','.join(str(port) for port in ref.ports)}` | `{ref.catalog_ref}` |"
        )
    if report.errors:
        lines.extend(["", "## Errors", ""])
        for error in report.errors:
            lines.append(f"- {error}")
    path.write_text("\n".join(lines) + "\n")


def write_json(path: Path, report: EgressReport) -> None:
    path.write_text(json.dumps(asdict(report), indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate approved external egress for coding-agent and tenant workspaces.")
    parser.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument("--output-dir", default="results/egress-governance")
    args = parser.parse_args()

    catalog_path = (ROOT / args.catalog).resolve() if not Path(args.catalog).is_absolute() else Path(args.catalog)
    report = run_check(catalog_path)
    if args.report:
        output_dir = ROOT / args.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        json_path = output_dir / f"egress-governance-{stamp}.json"
        md_path = output_dir / f"egress-governance-{stamp}.md"
        write_json(json_path, report)
        write_markdown(md_path, report)
        print(f"wrote {rel(json_path)} and {rel(md_path)}")
    if report.errors:
        print("egress governance check failed:")
        for error in report.errors:
            print(f"- {error}")
        return 1
    print(f"egress governance OK: {len(report.checked_references)} external egress reference(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
