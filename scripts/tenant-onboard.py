#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def load_spec(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return data


def nested(mapping: dict[str, Any], *keys: str, default: Any = None) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return current if current is not None else default


def display_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def validate_dns_label(value: str, field: str) -> None:
    if len(value) > 63 or not DNS_LABEL.match(value):
        raise ValueError(f"{field} must be a Kubernetes DNS label: lowercase letters, numbers, hyphens, max 63 chars")


def validate_spec(spec: dict[str, Any]) -> None:
    if spec.get("apiVersion") != "platform.ai/v1alpha1":
        raise ValueError("apiVersion must be platform.ai/v1alpha1")
    if spec.get("kind") != "TenantOnboarding":
        raise ValueError("kind must be TenantOnboarding")
    tenant = nested(spec, "spec", "tenant", default={})
    if not isinstance(tenant, dict):
        raise ValueError("spec.tenant must be a mapping")
    for field in ("id", "name", "namespace", "owner", "group", "costCenter", "environment"):
        if not tenant.get(field):
            raise ValueError(f"spec.tenant.{field} is required")
    for field in ("id", "name", "namespace"):
        validate_dns_label(str(tenant[field]), f"spec.tenant.{field}")

    quotas = nested(spec, "spec", "quotas", default={})
    for field in (
        "requestsCpu",
        "requestsMemory",
        "limitsCpu",
        "limitsMemory",
        "pods",
        "persistentVolumeClaims",
        "secrets",
        "configMaps",
    ):
        if not isinstance(quotas, dict) or quotas.get(field) in (None, ""):
            raise ValueError(f"spec.quotas.{field} is required")

    platform = nested(spec, "spec", "platform", default={})
    for field in ("gatewayUrl", "ragUrl", "requiredHeaders"):
        if not isinstance(platform, dict) or not platform.get(field):
            raise ValueError(f"spec.platform.{field} is required")
    required_headers = str(platform["requiredHeaders"])
    for header in ("X-Request-ID", "X-Sandbox-ID", "X-API-Key"):
        if header not in required_headers:
            raise ValueError(f"spec.platform.requiredHeaders must include {header}")

    network = nested(spec, "spec", "network", default={})
    for path in (("gateway", "namespace"), ("rag", "namespace")):
        value = nested(network, *path)
        if not value:
            raise ValueError(f"spec.network.{'.'.join(path)} is required")
        validate_dns_label(str(value), f"spec.network.{'.'.join(path)}")
    for path in (("gateway", "port"), ("rag", "port")):
        port = nested(network, *path)
        if not isinstance(port, int) or port < 1 or port > 65535:
            raise ValueError(f"spec.network.{'.'.join(path)} must be a TCP port number")
    for index, item in enumerate(nested(network, "allowedEgressCidrs", default=[])):
        if not isinstance(item, dict):
            raise ValueError(f"spec.network.allowedEgressCidrs[{index}] must be a mapping")
        if not item.get("catalogRef"):
            raise ValueError(f"spec.network.allowedEgressCidrs[{index}].catalogRef is required")
        # An exception with no expiry is a permanent one. The date is what the chart
        # stamps onto the NetworkPolicy for the in-cluster expiry check, so onboarding a
        # tenant without it would produce an exception nothing can retire.
        expires_on = item.get("expiresOn")
        if not expires_on:
            raise ValueError(f"spec.network.allowedEgressCidrs[{index}].expiresOn is required (YYYY-MM-DD)")
        try:
            date.fromisoformat(str(expires_on))
        except ValueError as exc:
            raise ValueError(f"spec.network.allowedEgressCidrs[{index}].expiresOn must be YYYY-MM-DD") from exc
        ipaddress.ip_network(str(item.get("cidr")), strict=False)
        ports = item.get("ports")
        if not isinstance(ports, list) or not ports:
            raise ValueError(f"spec.network.allowedEgressCidrs[{index}].ports must be a non-empty list")
        for port in ports:
            if not isinstance(port, int) or port < 1 or port > 65535:
                raise ValueError(f"spec.network.allowedEgressCidrs[{index}].ports contains invalid port {port}")

    compliance = nested(spec, "spec", "compliance", default={})
    if compliance:
        if not isinstance(compliance, dict):
            raise ValueError("spec.compliance must be a mapping")
        for field in ("profile", "dataClassification"):
            if not compliance.get(field):
                raise ValueError(f"spec.compliance.{field} is required when compliance is set")
            validate_dns_label(str(compliance[field]), f"spec.compliance.{field}")
        external_allowed = compliance.get("externalEgressAllowed", True)
        if not isinstance(external_allowed, bool):
            raise ValueError("spec.compliance.externalEgressAllowed must be a boolean")
        if external_allowed is False and network.get("allowedEgressCidrs"):
            raise ValueError("spec.network.allowedEgressCidrs must be empty when external egress is disallowed")
        retention_days = compliance.get("evidenceRetentionDays")
        if retention_days is not None and (not isinstance(retention_days, int) or retention_days <= 0):
            raise ValueError("spec.compliance.evidenceRetentionDays must be a positive integer")

    workspace = nested(spec, "spec", "agentWorkspace", default={})
    if workspace.get("enabled", True):
        namespace = str(workspace.get("namespace") or tenant["namespace"])
        validate_dns_label(namespace, "spec.agentWorkspace.namespace")
        if not workspace.get("pvcSize"):
            raise ValueError("spec.agentWorkspace.pvcSize is required when agent workspace is enabled")


def labels(tenant: dict[str, Any], name: str, compliance: dict[str, Any] | None = None) -> dict[str, str]:
    values = {
        "app.kubernetes.io/name": name,
        "app.kubernetes.io/part-of": "private-ai-platform-kit",
        "platform.ai/cost-center": str(tenant["costCenter"]),
        "platform.ai/environment": str(tenant["environment"]),
        "platform.ai/owner": str(tenant["owner"]),
        "platform.ai/sandbox-id": str(tenant["id"]),
        "platform.ai/tenant": str(tenant["name"]),
    }
    if compliance:
        values["platform.ai/compliance-profile"] = str(compliance["profile"])
        values["platform.ai/data-classification"] = str(compliance["dataClassification"])
    return values


def metadata(
    name: str, namespace: str | None, tenant: dict[str, Any], compliance: dict[str, Any] | None = None
) -> dict[str, Any]:
    data: dict[str, Any] = {"name": name, "labels": labels(tenant, name, compliance)}
    if namespace:
        data["namespace"] = namespace
    return data


def tenant_manifest(spec: dict[str, Any]) -> list[dict[str, Any]]:
    tenant = nested(spec, "spec", "tenant")
    quotas = nested(spec, "spec", "quotas")
    limits = nested(spec, "spec", "limitRange", default={})
    platform = nested(spec, "spec", "platform")
    network = nested(spec, "spec", "network")
    compliance = nested(spec, "spec", "compliance", default={})
    namespace = tenant["namespace"]
    manifest: list[dict[str, Any]] = [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": namespace,
                "labels": {
                    **labels(tenant, namespace, compliance),
                    "platform.ai/traceable-sandbox": "true",
                    "pod-security.kubernetes.io/enforce": "restricted",
                    "pod-security.kubernetes.io/audit": "restricted",
                    "pod-security.kubernetes.io/warn": "restricted",
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": metadata("tenant-quota", namespace, tenant, compliance),
            "spec": {
                "hard": {
                    "requests.cpu": str(quotas["requestsCpu"]),
                    "requests.memory": str(quotas["requestsMemory"]),
                    "limits.cpu": str(quotas["limitsCpu"]),
                    "limits.memory": str(quotas["limitsMemory"]),
                    "pods": str(quotas["pods"]),
                    "configmaps": str(quotas["configMaps"]),
                    "secrets": str(quotas["secrets"]),
                    "persistentvolumeclaims": str(quotas["persistentVolumeClaims"]),
                }
            },
        },
        {
            "apiVersion": "v1",
            "kind": "LimitRange",
            "metadata": metadata("tenant-defaults", namespace, tenant, compliance),
            "spec": {
                "limits": [
                    {
                        "type": "Container",
                        "defaultRequest": {
                            "cpu": str(limits.get("defaultRequestCpu", "100m")),
                            "memory": str(limits.get("defaultRequestMemory", "128Mi")),
                        },
                        "default": {
                            "cpu": str(limits.get("defaultCpu", "1")),
                            "memory": str(limits.get("defaultMemory", "1Gi")),
                        },
                    }
                ]
            },
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": metadata("tenant-default-deny", namespace, tenant, compliance),
            "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]},
        },
    ]

    egress: list[dict[str, Any]] = []
    if network.get("allowDns", True):
        egress.append(
            {
                "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}],
                "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
            }
        )
    for key in ("gateway", "rag"):
        item = network[key]
        egress.append(
            {
                "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": item["namespace"]}}}],
                "ports": [{"protocol": "TCP", "port": item["port"]}],
            }
        )
    for item in network.get("allowedEgressCidrs", []):
        egress.append(
            {
                "to": [{"ipBlock": {"cidr": str(item["cidr"])}}],
                "ports": [{"protocol": "TCP", "port": port} for port in item["ports"]],
            }
        )
    manifest.append(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": metadata("tenant-approved-egress", namespace, tenant, compliance),
            "spec": {"podSelector": {}, "policyTypes": ["Egress"], "egress": egress},
        }
    )
    manifest.extend(
        [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": metadata("tenant-trace-contract", namespace, tenant, compliance),
                "data": {
                    "sandbox-id": str(tenant["id"]),
                    "tenant": str(tenant["name"]),
                    "required-headers": str(platform["requiredHeaders"]),
                    "gateway-url": str(platform["gatewayUrl"]),
                    "rag-url": str(platform["ragUrl"]),
                    "approved-egress": ", ".join(
                        f"{item.get('catalogRef')}={item['cidr']}" for item in network.get("allowedEgressCidrs", [])
                    )
                    or "none",
                    "compliance-profile": str(compliance.get("profile", "standard")),
                    "data-classification": str(compliance.get("dataClassification", "internal")),
                    "external-egress-allowed": str(compliance.get("externalEgressAllowed", True)).lower(),
                    "private-registry-required": str(compliance.get("requirePrivateRegistry", False)).lower(),
                    "evidence-retention-days": str(compliance.get("evidenceRetentionDays", "")),
                },
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "Role",
                "metadata": metadata("tenant-lab-viewer", namespace, tenant, compliance),
                "rules": [
                    {
                        "apiGroups": [""],
                        "resources": ["pods", "pods/log", "services", "configmaps", "events"],
                        "verbs": ["get", "list", "watch"],
                    },
                    {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["get", "list", "watch"]},
                ],
            },
            {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": metadata("tenant-lab-viewers", namespace, tenant, compliance),
                "subjects": [{"kind": "Group", "name": str(tenant["group"]), "apiGroup": "rbac.authorization.k8s.io"}],
                "roleRef": {"kind": "Role", "name": "tenant-lab-viewer", "apiGroup": "rbac.authorization.k8s.io"},
            },
        ]
    )
    return manifest


def agent_workspace_values(spec: dict[str, Any]) -> dict[str, Any]:
    tenant = nested(spec, "spec", "tenant")
    quotas = nested(spec, "spec", "quotas")
    limits = nested(spec, "spec", "limitRange", default={})
    platform = nested(spec, "spec", "platform")
    network = nested(spec, "spec", "network")
    compliance = nested(spec, "spec", "compliance", default={})
    workspace = nested(spec, "spec", "agentWorkspace", default={})
    rbac = workspace.get("rbac", {})
    namespace = workspace.get("namespace") or tenant["namespace"]
    return {
        "namespace": {"create": False, "name": namespace},
        "sandbox": {
            "id": tenant["id"],
            "tenant": tenant["name"],
            "owner": tenant["owner"],
            "costCenter": tenant["costCenter"],
            "environment": tenant["environment"],
            "complianceProfile": compliance.get("profile", "standard"),
            "dataClassification": compliance.get("dataClassification", "internal"),
            "externalEgressAllowed": bool(compliance.get("externalEgressAllowed", True)),
            "requirePrivateRegistry": bool(compliance.get("requirePrivateRegistry", False)),
            "evidenceRetentionDays": compliance.get("evidenceRetentionDays", ""),
        },
        "serviceAccount": {
            "name": workspace.get("serviceAccountName", "agent-runner"),
            "automountServiceAccountToken": False,
        },
        "rbac": {
            "viewerGroup": rbac.get("viewerGroup", tenant["group"]),
            "allowJobManagement": bool(rbac.get("allowJobManagement", True)),
        },
        "resourceQuota": {
            "requestsCpu": str(quotas["requestsCpu"]),
            "requestsMemory": str(quotas["requestsMemory"]),
            "limitsCpu": str(quotas["limitsCpu"]),
            "limitsMemory": str(quotas["limitsMemory"]),
            "pods": str(quotas["pods"]),
            "persistentVolumeClaims": str(quotas["persistentVolumeClaims"]),
            "secrets": str(quotas["secrets"]),
            "configMaps": str(quotas["configMaps"]),
        },
        "limitRange": {
            "defaultRequestCpu": str(limits.get("defaultRequestCpu", "100m")),
            "defaultRequestMemory": str(limits.get("defaultRequestMemory", "128Mi")),
            "defaultCpu": str(limits.get("defaultCpu", "1")),
            "defaultMemory": str(limits.get("defaultMemory", "1Gi")),
        },
        "workspace": {
            "pvc": {
                "enabled": True,
                "name": workspace.get("pvcName", "agent-workspace"),
                "accessModes": workspace.get("accessModes", ["ReadWriteOnce"]),
                "size": str(workspace["pvcSize"]),
                "storageClassName": workspace.get("storageClassName", ""),
            },
            "mountPath": workspace.get("mountPath", "/workspace"),
        },
        "platform": {
            "gatewayUrl": platform["gatewayUrl"],
            "ragUrl": platform["ragUrl"],
            "requiredHeaders": platform["requiredHeaders"],
        },
        "networkPolicy": {
            "enabled": True,
            "allowDns": bool(network.get("allowDns", True)),
            "gateway": network["gateway"],
            "rag": network["rag"],
            "allowedEgressCidrs": [
                {
                    "catalogRef": item["catalogRef"],
                    "cidr": item["cidr"],
                    "ports": item["ports"],
                    # Carried through to the rendered NetworkPolicy annotation so the
                    # cluster can retire the exception when its review lapses.
                    "expiresOn": str(item["expiresOn"]),
                }
                for item in network.get("allowedEgressCidrs", [])
            ],
        },
    }


def render_readme(spec: dict[str, Any], tenant_yaml: str, agent_values_yaml: str) -> str:
    tenant = nested(spec, "spec", "tenant")
    compliance = nested(spec, "spec", "compliance", default={})
    namespace = tenant["namespace"]
    lines = [
        f"# Tenant Onboarding: {tenant['name']}",
        "",
        f"Sandbox ID: `{tenant['id']}`",
        f"Namespace: `{namespace}`",
        f"Owner: `{tenant['owner']}`",
    ]
    if compliance:
        lines.extend(
            [
                f"Compliance profile: `{compliance['profile']}`",
                f"Data classification: `{compliance['dataClassification']}`",
                f"External egress allowed: `{str(compliance.get('externalEgressAllowed', True)).lower()}`",
            ]
        )
    lines.extend(
        [
            "",
            "Apply the tenant namespace controls:",
            "",
            f"    kubectl apply -f {tenant_yaml}",
            "",
            "Install or update the matching coding-agent workspace:",
            "",
            f"    helm upgrade --install agent-workspace deploy/charts/agent-workspace --namespace {namespace} --values {agent_values_yaml}",
            "",
            "Run a tenant smoke check after the gateway and RAG service are ready:",
            "",
            f"    TENANT_ID={tenant['id']} TENANT_NAMESPACE={namespace} make tenant-smoke",
            "",
        ]
    )
    if compliance and compliance.get("externalEgressAllowed") is False:
        lines.extend(
            [
                "This profile intentionally renders no external CIDR egress. Add external dependencies only through a reviewed spec change.",
                "",
            ]
        )
    return "\n".join(lines)


def render(spec: dict[str, Any]) -> tuple[str, str, str]:
    tenant_yaml = yaml.safe_dump_all(tenant_manifest(spec), sort_keys=False)
    agent_yaml = yaml.safe_dump(agent_workspace_values(spec), sort_keys=False)
    readme = render_readme(spec, "tenant-lab.yaml", "agent-workspace-values.yaml")
    return tenant_yaml, agent_yaml, readme


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate tenant lab and coding-agent workspace onboarding artifacts.")
    parser.add_argument("--spec", default="tenants/onboarding/coding-agents.yaml")
    parser.add_argument("--output-dir", default=".out/tenants")
    parser.add_argument("--check", action="store_true", help="Validate and render the spec without writing files.")
    parser.add_argument(
        "--apply", action="store_true", help="kubectl apply the rendered tenant manifests (self-service end-to-end)."
    )
    args = parser.parse_args()

    spec_path = ROOT / args.spec
    spec = load_spec(spec_path)
    validate_spec(spec)
    tenant_yaml, agent_yaml, readme = render(spec)
    list(yaml.safe_load_all(tenant_yaml))
    yaml.safe_load(agent_yaml)

    tenant_id = nested(spec, "spec", "tenant", "id")
    if args.check:
        print(f"tenant onboarding OK: {spec_path.relative_to(ROOT)} ({tenant_id})")
        return 0

    output_dir = ROOT / args.output_dir / str(tenant_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "tenant-lab.yaml").write_text(tenant_yaml)
    (output_dir / "agent-workspace-values.yaml").write_text(agent_yaml)
    (output_dir / "README.md").write_text(readme)
    print(f"wrote {display_path(output_dir / 'tenant-lab.yaml')}")
    print(f"wrote {display_path(output_dir / 'agent-workspace-values.yaml')}")
    print(f"wrote {display_path(output_dir / 'README.md')}")

    if args.apply:
        # Self-service end-to-end: apply the namespace, quota, limit range, and network
        # policies. The agent workspace is a Helm release, so its install command is printed.
        if shutil.which("kubectl") is None:
            raise SystemExit("--apply requires kubectl on PATH")
        manifest = output_dir / "tenant-lab.yaml"
        result = subprocess.run(["kubectl", "apply", "-f", str(manifest)], check=False)
        if result.returncode != 0:
            raise SystemExit(f"kubectl apply failed with exit code {result.returncode}")
        print(f"applied tenant '{tenant_id}': namespace, quota, limit range, and network policies")
        print(
            "next (agent workspace): helm upgrade --install agent-workspace "
            f"deploy/charts/agent-workspace -f {display_path(output_dir / 'agent-workspace-values.yaml')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
