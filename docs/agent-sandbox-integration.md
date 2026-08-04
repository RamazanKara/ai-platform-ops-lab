# Agent-sandbox integration

Agent workspaces run as `agents.x-k8s.io/v1beta1` `Sandbox` resources managed by the
vendored `kubernetes-sigs/agent-sandbox` controller. This is the only workspace runtime in
release `v0.28.0`.

The decisions are recorded in [ADR 0009](adr/0009-adopt-agent-sandbox-workspace-runtime.md)
and [ADR 0010](adr/0010-agent-sandbox-standard-runtime.md). This page describes the current
contract rather than the implementation history.

## Installation

The controller manifests are vendored under `deploy/vendor/agent-sandbox/v0.5.0/` with
recorded SHA-256 checksums. Both Argo CD profiles contain an
`agent-sandbox-controller` application. The direct quickstart path installs the same files with:

```bash
make agent-sandbox-install
```

The install uses server-side apply because the CRDs are too large for the client-side
last-applied annotation.

## Workspace resource

`deploy/charts/agent-workspace` always renders one `Sandbox`. Its name is
`sandbox.id`, and the controller gives the managed pod the same name. The chart also creates:

- the workspace namespace, service account, RBAC, quota, and limit range;
- a writable PVC mounted at `/workspace`;
- a configuration map with the gateway and RAG service addresses;
- default-deny ingress and egress policies plus explicit DNS, gateway, RAG, and catalog-approved
  CIDR rules.

The pod template runs as UID/GID `10001`, uses `RuntimeDefault` seccomp, drops all Linux
capabilities, disables privilege escalation, and makes the root filesystem read-only. The
default Kubernetes service-account token is not mounted.

## Kernel isolation

The controller-managed pod is a lifecycle and policy boundary. It is not automatically a separate
kernel boundary. `sandbox.runtimeClassName` is empty in the checked-in local and customer values.
Set it to a cluster-provided runtime such as gVisor or Kata when that isolation is required, and
verify that the runtime exists on every node that may host a workspace.

## Platform credential

The chart projects a service-account token at `/var/run/platform/token`. It is scoped to the
`inference-gateway` audience, expires after 600 seconds by default, and is rotated by the kubelet.
The gateway only accepts it when JWT/JWKS verification is configured against the cluster issuer.

This token is still a credential. Audience binding and expiry reduce its usefulness elsewhere but
do not make the workspace trusted.

## Network boundary

The direct `Sandbox` resource does not create an upstream NetworkPolicy. The chart's
NetworkPolicies are therefore the network boundary for this path. They require a CNI that enforces
NetworkPolicy; the local profile uses Calico. The smoke check refuses to treat kindnet as valid
egress evidence.

Every external CIDR entry needs a `catalogRef` from
`platform/network/egress-catalog.yaml`. Approved destinations can still receive data, so the
catalog must stay narrow and be reviewed as an exfiltration boundary.

## Updates and lifecycle

`workspace.shutdownTime` and `workspace.shutdownPolicy` can bound a workspace lifetime. They are
unset by default.

The v0.5.0 controller does not replace its singleton pod when the `Sandbox` pod template changes.
After a chart update, delete the managed pod so the controller recreates it from the current
template. `make agent-sandbox-smoke` detects image or volume drift and performs that refresh.

Warm pools, `SandboxClaim`, multi-cluster scheduling, and workspace snapshot/restore are not
implemented by this chart.

## Validation

After the controller and workspace application are synced, run:

```bash
make agent-sandbox-smoke
```

The check verifies controller readiness, the `Sandbox` and pod state, non-root execution, the
read-only root filesystem, writable workspace storage, absence of the ambient token, the projected
token audience, working DNS, and blocked non-catalog egress.

`make agent-sandbox-demo` adds the governed model-call and audit-receipt walkthrough used by the
README animation.

## Limits

- A missing isolation `RuntimeClass` means the workspace shares the node kernel.
- NetworkPolicy restricts connections but does not encrypt them.
- Namespace RBAC does not restrict actions performed through an approved external service.
- The gateway audit chain records governed model calls, not every process or file operation inside
  the workspace.
- PVC availability, backup, retention, and secure deletion depend on the cluster storage system.
