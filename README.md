# Private AI Platform Kit

[![CI](https://github.com/RamazanKara/private-ai-platform-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/RamazanKara/private-ai-platform-kit/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/RamazanKara/private-ai-platform-kit)](https://github.com/RamazanKara/private-ai-platform-kit/releases)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/RamazanKara/private-ai-platform-kit/badge)](https://scorecard.dev/viewer/?uri=github.com/RamazanKara/private-ai-platform-kit)
[![License](https://img.shields.io/github/license/RamazanKara/private-ai-platform-kit)](LICENSE)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.21039342.svg)](https://doi.org/10.5281/zenodo.21039342)

Run LLM APIs, retrieval, and controlled coding-agent workspaces on Kubernetes, with the deployment
and validation code kept in the same repository.

Private AI Platform Kit is a reference implementation for running an LLM gateway, retrieval, and coding-agent workspaces on Kubernetes. It includes a local `kind` profile and a template for customer-owned clusters built from the same service code and Helm charts.

Current release: `v0.28.1`. The project is suitable for evaluation and platform engineering work. It is not a managed service or a ready-made production environment. A production deployment still needs customer identity, secrets, storage, ingress, observability, backup, capacity planning, and current validation evidence.

[Documentation](https://ramazankara.github.io/private-ai-platform-kit/) · [Quickstart](docs/quickstart.md) · [Feature inventory](docs/feature-inventory.md) · [Production readiness](docs/production-readiness.md) · [Security](docs/security-overview.md)

<p align="center">
  <img src="docs/assets/private-ai-platform-kit-demo.gif" alt="Illustrated terminal walkthrough of a hardened agent workspace, blocked egress, gateway receipts, and evidence generation" width="100%">
</p>

The animation is a deliberately staged terminal cut, recorded with
[`scripts/demo.tape`](scripts/demo.tape). It shows the main workflow without making the README wait
on a live cluster. Run `make agent-sandbox-demo` for the current end-to-end result; the capture is
presentation, not release evidence.

## What is in the repository

- A FastAPI inference gateway with OpenAI-compatible chat, completions, embeddings, moderations, Files, Batch, and Responses endpoints, plus an Anthropic Messages endpoint with streaming. The exact route set is stored in [the OpenAPI contract](platform/api-contracts/inference-gateway.openapi.json).
- API-key and JWT/JWKS authentication, model allowlists, request limits, per-sandbox budgets settled against measured token usage, rate limiting, input secret detection, an optional output guardrail, and redacted audit records linked by a hash chain.
- Receipts covering model calls, RAG retrievals, and (opt-in) agent actions such as denied egress and tool execution, on chains that link across process restarts so a missing lifetime is detectable.
- Ollama values for the local CPU path and vLLM values for NVIDIA and AMD GPU clusters.
- A RAG service with a local lexical backend and a Qdrant-backed customer profile.
- Helm charts, Argo CD applications, Kyverno policies, tenant templates, and agent workspaces based on `kubernetes-sigs/agent-sandbox`.
- Tests and release checks for service behavior, rendered charts, API and configuration contracts, model governance, evidence reports, and supply-chain artifacts.

The [feature inventory](docs/feature-inventory.md) records what is implemented, what is disabled by default, and what remains the operator's responsibility.

## Local quickstart

The managed bootstrap supports Linux and WSL. It requires Docker, Python 3.12 or newer, Bash, and `curl`:

```bash
make bootstrap
```

`make bootstrap` downloads pinned command-line tools into `.tools/bin`, then runs the local quickstart. If `kind`, `kubectl`, and Helm are already installed, use:

```bash
make quickstart
```

The first run needs internet access. It downloads tools, container images, Kubernetes and Argo CD manifests, Helm dependencies, Python packages, and the Ollama model. It also creates Docker images and a `kind` cluster and updates your kubeconfig. Model requests use the in-cluster Ollama service after setup; the installation itself is not an offline process.

The quickstart leaves the cluster running. Remove it with:

```bash
make local-down
```

See [the quickstart guide](docs/quickstart.md) for the commands it runs, expected completion messages, options, and troubleshooting.

## Repository validation

The default gate does not create a cluster, but it does require Python and Helm. On a fresh checkout it creates local virtual environments and installs hashed Python dependencies.

```bash
make validate
```

Useful focused checks are:

```bash
make test-gateway
make test-rag
make quality
make repo-hygiene
make docs-build
make production-check
```

`make validate-full` additionally requires the tools in the `strict` profile. Run `make toolchain-doctor TOOLCHAIN_PROFILE=strict` to see what is missing.

## Customer-owned clusters

The customer profile assumes that Kubernetes and Argo CD already exist. Configure the Git source and GPU values in a fork or deployment branch:

```bash
make customer-overlay \
  CUSTOMER_REPO_URL=https://github.com/<customer>/<repo>.git \
  CUSTOMER_REVISION=v0.28.1 \
  CUSTOMER_GPU_PROFILE=nvidia
```

This command edits the customer Argo CD manifests. Review and commit those changes before syncing them. The default NVIDIA profile requests four GPUs per vLLM replica and is only a starting point; choose the model, GPU count, context length, storage, and replica limits for the target cluster.

The customer overlay does not install or configure ingress, an identity provider, a secret backend, a production observability stack, or a working backup destination. Read [the customer deployment guide](deploy/clusters/customer/README.md) before applying it.

## Request path

![Private AI Platform Kit architecture](docs/assets/architecture.svg)

Clients call the inference gateway. The gateway authenticates the request, applies the configured model and admission policy, accounts for the sandbox budget, and forwards the request to Ollama or vLLM. It emits metrics and a redacted audit record. RAG is a separate service; clients or applications call it to retrieve context and then submit grounded messages to the gateway.

The hash chain makes edits or reordering within an exported audit stream detectable. Each process opens its chain with a record naming the previous chain and the head it reached, so the chains form their own chain and a whole lifetime that went missing no longer looks like an ordinary restart. It still does not make logs durable by itself: detecting truncation or rollback of the most recent records requires a trusted, separately stored chain-head anchor. See [the audit runbook](runbooks/audit-chain.md).

## Documentation

| Task | Document |
| --- | --- |
| Run the local lab | [Quickstart](docs/quickstart.md) |
| Work through validation and operations | [Getting started](docs/getting-started.md) |
| Understand the deployed components | [Architecture](docs/architecture.md) |
| Check implemented features and defaults | [Feature inventory](docs/feature-inventory.md) |
| Decide whether the project fits | [Decision guide](docs/decision-guide.md) |
| Prepare a customer cluster | [Customer deployment](deploy/clusters/customer/README.md) |
| Review security boundaries | [Security overview](docs/security-overview.md) and [threat model](docs/threat-model.md) |
| Operate the platform | [Runbooks](runbooks/README.md) |
| Verify a release | [Release verification](docs/release-verification.md) |
| Contribute | [Contributing](CONTRIBUTING.md) |

## Repository layout

| Path | Contents |
| --- | --- |
| `src/` | Inference gateway and RAG service |
| `deploy/charts/` | Helm charts |
| `deploy/clusters/` | Local and customer values and Argo CD applications |
| `platform/` | API/config contracts, policies, model catalog, evals, and SLO inputs |
| `tenants/` | Tenant onboarding specifications |
| `runbooks/` | Operational procedures |
| `scripts/` | Validation, setup, and evidence tooling |
| `results/` | Checked-in sample report shapes; current generated reports are ignored |

Sample files under `results/` demonstrate report formats and gate behavior. They are not evidence for the current checkout or a customer deployment. Strict release checks require newly generated, non-sample artifacts.

Licensed under Apache-2.0. Kubernetes is a registered trademark of The Linux Foundation; this project is not affiliated with or endorsed by The Linux Foundation.
