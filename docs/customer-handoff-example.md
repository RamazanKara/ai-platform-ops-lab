# Customer handoff example

This example uses a fake customer organization, `acme-ai`, to show the expected handoff flow without assuming a cloud provider.

## Inputs

- Customer Git mirror: `https://github.com/acme-ai/private-ai-platform-kit.git`
- Target revision: `v0.28.0`
- GPU profile: `nvidia`
- Runtime model: `Qwen/Qwen3-Coder-Next`
- Secret integration: customer-operated External Secrets or an equivalent mechanism. The
  checked-in `external-secrets.yaml` is an example and is not synced by the customer application
  set.

## Configure the overlay

```bash
make customer-overlay \
  CUSTOMER_REPO_URL=https://github.com/acme-ai/private-ai-platform-kit.git \
  CUSTOMER_REVISION=v0.28.0 \
  CUSTOMER_GPU_PROFILE=nvidia
```

The command edits tracked Argo CD manifests. Run it in the customer fork or a deployment branch,
then review and commit the diff before syncing.

Review the generated changes in:

- `deploy/gitops/argocd/root-app-customer.yaml`
- `deploy/clusters/customer/apps.yaml`
- `deploy/clusters/customer/values/inference-gateway.yaml`
- `deploy/clusters/customer/values/vllm-nvidia.yaml`
- `deploy/clusters/customer/values/rag-service.yaml`

## Customer decisions

- Confirm the active kubeconfig points at the intended cluster and that Argo CD, Kyverno, and the
  required CRDs are already installed.
- Confirm GPU nodes expose `nvidia.com/gpu` and have `platform.ai/node-pool=gpu`.
- Confirm API-key hashes are sourced from the customer secret backend; do not apply the example
  secret manifest unchanged.
- Replace model provenance source-reference digests with customer model-store artifact digests.
- Replace sample RAG knowledge with approved customer documents and matching Qdrant dimensions.
- Review agent workspace egress against `platform/network/egress-catalog.yaml`.

## Handoff evidence

After sync, run the in-cluster smoke checks. Point the eval and load test at the customer ingress or
an approved port-forward:

```bash
make smoke RUNTIME_BACKEND=vllm
make rag-smoke
GATEWAY_URL=https://gateway.example.test PLATFORM_API_KEY=<key> make eval
GATEWAY_URL=https://gateway.example.test PLATFORM_API_KEY=<key> \
  MODEL_ID=Qwen/Qwen3-Coder-Next RUNTIME_BACKEND=vllm make loadtest
make evidence LIVE=1
make release-gate-strict
```

Replace the example URL, key, and model. Attach the generated Markdown reports and keep the JSON
evidence with the release or handoff record.
