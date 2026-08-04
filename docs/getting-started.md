# Getting started

Use the [local quickstart](quickstart.md) for the first cluster run. This page is the command reference for development, validation, and customer handoff.

## Tool requirements

| Task | Required tools |
| --- | --- |
| Repository validation | Python 3.12+, Helm |
| Local cluster | Docker, `kind`, `kubectl`, Helm, Python |
| Strict validation | The tools in the `strict` profile under `platform/tools/validation-toolchain.yaml` |

Check the installed tools:

```bash
make toolchain-doctor TOOLCHAIN_PROFILE=validate
make toolchain-doctor TOOLCHAIN_PROFILE=local
make toolchain-doctor TOOLCHAIN_PROFILE=strict
```

On Linux or WSL, `make toolchain-install` installs the default validation set under `.tools/bin`. The Makefile prepends that directory to `PATH`.

## Static validation

Run the default gate:

```bash
make validate
```

It runs service tests, parser fuzz checks, Ruff, mypy, chart lint/render checks, API and configuration contract checks, governance checks, and production-readiness checks. Optional external tools are reported and skipped. `make validate-full` fails when the strict tool set is incomplete.

Focused targets are faster while editing:

```bash
make test-gateway
make test-rag
make quality
make chart-docs
make api-contract
make config-contract
make repo-hygiene
make docs-build
make production-check
```

Use the corresponding `*-update` target only when a reviewed code or values change intentionally alters a generated contract.

## Local cluster, step by step

The manual equivalent of `make quickstart` is:

```bash
make local-up
make agent-sandbox-install
make bootstrap-argocd
make sync
make smoke RUNTIME_BACKEND=ollama
make rag-smoke
```

For the reduced direct-Helm path:

```bash
make local-up
make agent-sandbox-install
LOCAL_DIRECT_APPLY=1 make sync
make smoke RUNTIME_BACKEND=ollama
make rag-smoke
```

The direct path only covers the core runtime charts. Use the Argo CD path when testing GitOps, policy, observability, or backup applications.

## Tenant and workspace checks

```bash
make trace-smoke
make tenant-smoke
make agent-smoke
make agent-sandbox-smoke
```

Render tenant manifests without applying them:

```bash
make tenant-onboard
make tenant-onboard-regulated
make tenant-onboard-gpu
```

Generated files go under `.out/tenants`. Review them before applying them to any cluster.

## Evals, load tests, and evidence

`make eval-local` and `make loadtest-local` use an ephemeral mock runtime. They exercise the gateway and report pipeline; they do not measure Ollama or vLLM throughput.

```bash
make eval-local
make loadtest-local
```

For a running gateway:

```bash
GATEWAY_URL=http://127.0.0.1:8080 make eval
GATEWAY_URL=http://127.0.0.1:8080 make loadtest
```

Generate static or live evidence:

```bash
make evidence
make evidence LIVE=1
```

The non-strict release gate may use checked-in `sample-*` reports to test the gate configuration. It is not release evidence. Before a release or customer handoff, generate current reports and run:

```bash
make validate-full
make release-gate-strict
make release-report-strict
```

## Customer-owned Kubernetes

The customer path assumes an existing cluster and Argo CD installation. Work in the customer fork or deployment branch because the configurator edits tracked manifests:

```bash
make customer-overlay \
  CUSTOMER_REPO_URL=https://github.com/<customer>/<repo>.git \
  CUSTOMER_REVISION=v0.28.0 \
  CUSTOMER_GPU_PROFILE=nvidia
```

Use `CUSTOMER_GPU_PROFILE=amd` for the AMD values or `default` for the base vLLM values. Then review the diff and run:

```bash
make customer-overlay-check
ENVIRONMENT=customer make bootstrap-argocd
ENVIRONMENT=customer make sync
```

The customer profile is a template. Before syncing, replace the identity and secret placeholders, select storage classes, review every image and model source, size GPU and stateful resources, and connect the cluster's ingress, metrics, logs, alerts, and backups. The detailed checklist is in [the customer deployment guide](https://github.com/RamazanKara/private-ai-platform-kit/blob/main/deploy/clusters/customer/README.md).

## Cleanup

```bash
make local-down  # delete the kind cluster
make clean       # remove generated output, caches, build output, and service venvs
make clean-all   # also remove downloaded tools and tooling venvs
```
