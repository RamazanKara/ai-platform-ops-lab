# Customer-owned Kubernetes deployment

This overlay is for customers who already operate Kubernetes. It does not create cloud infrastructure and does not assume a specific managed Kubernetes service.

## What Customers Provide

- Kubernetes cluster with a default StorageClass.
- Ingress or an approved port-forwarding path.
- Argo CD installed in the target cluster.
- Kyverno and, if the supplied secret examples are used, External Secrets Operator.
- A secret backend compatible with the selected secret integration.
- Optional GPU nodes that expose `nvidia.com/gpu` or `amd.com/gpu`.
- Customer-owned Git repository, fork, or mirror for this repo.
- Existing logging, metrics, alerting, backup, and incident-management integrations.

## 1. Configure the GitOps overlay

Set every Argo CD `repoURL` to the customer fork or mirror, pin the revision to the branch or tag you want to deploy, and choose the active vLLM GPU profile:

```bash
make customer-overlay \
  CUSTOMER_REPO_URL=https://github.com/<customer>/<repo>.git \
  CUSTOMER_REVISION=v0.28.0 \
  CUSTOMER_GPU_PROFILE=nvidia
```

Run this in the fork or deployment branch. The command edits tracked Argo CD manifests; review and
commit the resulting diff before the cluster can reconcile it.

Use `CUSTOMER_GPU_PROFILE=amd` for AMD ROCm clusters. Use `CUSTOMER_GPU_PROFILE=default` to keep `deploy/clusters/customer/values/vllm.yaml`.

Validate the overlay without changing files:

```bash
make customer-overlay-check
```

The configurator updates:

- `deploy/gitops/argocd/root-app-customer.yaml`
- all child Applications in `deploy/clusters/customer/apps.yaml`
- the `runtime-vllm` value file selection

## 2. Prepare secrets

Gateway and RAG business endpoints read SHA-256 API-key hashes from existing Kubernetes Secrets. The
checked-in customer values do not create those Secrets. `external-secrets.yaml` is an example and is
not part of the customer Argo CD application list; install External Secrets Operator and add the
reviewed manifest to the customer GitOps path, or create the Secrets through the customer's existing
secret system. Do not store plaintext API keys in Helm values.

Create a hash for each customer API key:

```bash
printf '%s' "$PLATFORM_API_KEY" | sha256sum | awk '{print $1}'
```

Publish the comma-separated hashes as `api-key-sha256s` in the customer secret backend. If the Kubernetes provider example in `external-secrets.yaml` is used for a lab, the backing secret must live in the `platform-secrets` namespace under the `ai-platform-customer-secrets` key.

vLLM model pulls use `hf-token` from the same External Secrets path when the selected model requires Hugging Face access.

## 3. Confirm GPU scheduling

NVIDIA clusters should expose:

```text
nvidia.com/gpu
```

AMD clusters should expose:

```text
amd.com/gpu
```

Label GPU nodes:

```bash
kubectl label node <node> platform.ai/node-pool=gpu platform.ai/gpu-vendor=<nvidia|amd>
```

If GPU nodes are tainted, keep the matching tolerations in `deploy/clusters/customer/values/vllm-nvidia.yaml` or `deploy/clusters/customer/values/vllm-amd.yaml`.

The default customer model profile targets `Qwen/Qwen3-Coder-Next` and requests four GPUs per vLLM replica. Reduce `accelerator.count`, `model.maxModelLen`, tensor parallelism, replica counts, or the model itself for smaller clusters.

## 4. Review customer values

Review these before applying the overlay:

| File | Decision |
| --- | --- |
| `values/inference-gateway.yaml` | Runtime backend, allowed models, API-key secret, budgets, KEDA limits |
| `values/vllm-nvidia.yaml` or `values/vllm-amd.yaml` | Model, image, GPU count, autoscaling, node selectors, tolerations |
| `values/rag-service.yaml` | Qdrant URL, collection name, vector dimensions, API-key secret |
| `values/qdrant-vector-store.yaml` | Storage class, storage size, resources, backup expectations |
| `values/agent-workspace.yaml` | Tenant labels, PVC size, quotas, approved external egress |
| `external-secrets.yaml` | Secret store provider and remote secret keys |
| `gpu-scheduling.yaml` | GPU resource-name and node-label contract |

For regulated or offline teams, start from `tenants/onboarding/regulated-offline-coding-agents.yaml`. It renders confidential labels, no external CIDR egress, and no default job-management RBAC.

## 5. Apply

After committing the configured overlay to the customer repo, bootstrap or sync Argo CD:

```bash
ENVIRONMENT=customer make bootstrap-argocd
ENVIRONMENT=customer make sync
```

Both commands use the active kubeconfig context. Confirm it points to the intended customer cluster
before running them. Customer sync does not fall back to direct Helm apply when the Git repository is
unreachable.

If Argo CD cannot reach the repository, fix the `repoURL` values with `make customer-overlay` and sync again.

## 6. Smoke test

Port-forward or use the customer ingress path for the inference gateway, then run:

```bash
GATEWAY_URL=http://127.0.0.1:8080 make eval
GATEWAY_URL=http://127.0.0.1:8080 make loadtest
```

For live validation against the active customer-cluster context:

```bash
make evidence LIVE=1
make release-gate-strict
```

## Handoff Checklist

- API-key hashes are sourced from the customer secret backend, and the required Kubernetes Secrets exist before gateway/RAG rollout.
- Kyverno and any selected secret operator are installed before their custom resources or policies are synced.
- Gateway and RAG business endpoints require `X-API-Key` or Bearer auth.
- `runtime.allowedModels` contains only approved model IDs.
- Model provenance is replaced with customer model-store digests before production use.
- RAG knowledge and vector collections contain only approved customer content.
- Agent egress uses reviewed entries from `platform/network/egress-catalog.yaml`.
- Backups for the stateful stores are wired before go-live. The customer overlay ships no backup of its own. Use `deploy/backup/velero/schedule.yaml` as the template and protect the data-bearing namespaces (Qdrant in `vector`, agent workspace PVCs in `ai-agents`, GitOps state in `argocd`); a metadata-only backup restores empty data stores, so PVC contents must be captured (CSI volume snapshots, or `defaultVolumesToFsBackup`). Velero (or an equivalent) with a configured `BackupStorageLocation` and `VolumeSnapshotLocation` is a prerequisite: the restore-drill app presupposes a backup source the customer must provide, and without one the restore drill validates nothing.
- Restore-drill evidence is generated and retained under the customer policy.
- SLO, quota, retention, egress, model, eval, load, and evidence reports pass strict release gates without falling back to checked-in samples.

## Security policy customization for forks

If you fork or mirror this repo, review these guardrails before deploying. They are intentionally restrictive and hardcode upstream identities, so forks that republish artifacts must update them.

- **GitOps source is pinned and locked.** Set `CUSTOMER_REVISION` to an immutable tag (for example `v0.28.0`), not `HEAD`: `make customer-overlay-check` rejects `HEAD` or a branch so every sync is reproducible and revertible. The `private-ai-platform` AppProject in `deploy/clusters/customer/appprojects.yaml` locks `spec.sourceRepos` to the upstream repo and the destination to the in-cluster API server, giving blast-radius control. If you fork, update its `sourceRepos` to your repo (running `make customer-overlay CUSTOMER_REPO_URL=...` rewrites it for you) so Argo CD will accept syncs from your fork.
- **Kyverno image verification is Enforce.** The `ai-platform-verify-project-images` policy in `deploy/policies/kyverno/policies.yaml` is set to `Enforce` and hardcodes the upstream registry plus a keyless signing identity. Forks that republish images to their own registry MUST update its `imageReferences` and the keyless `subject`/`issuer` to their own GitHub org/repo and registry, or admission will reject your images.
- **Egress is governed by a reviewed catalog.** `platform/network/egress-catalog.yaml` is the source of truth for approved external egress. The CI check only scans the reviewed tenant spec files; at render time the agent-workspace chart rejects any `allowedEgressCidrs` entry that lacks a matching `catalogRef`; and at admission the Kyverno `ai-platform-restrict-egress-cidrs` policy denies broad CIDRs (`0.0.0.0/0` and broad RFC1918 ranges). Add new destinations to the catalog and reference them by `catalogRef` rather than widening CIDRs.
