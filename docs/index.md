# Private AI Platform Kit

This project is a Kubernetes reference implementation for a governed LLM gateway, retrieval service, and coding-agent workspaces. It has two deployment paths:

- a local `kind` cluster with Ollama for development and evaluation;
- a customer-cluster template with Ollama, vLLM, Qdrant, and agent workspaces.

The repository contains service code, Helm charts, Argo CD applications, policy, tests, and runbooks. It does not provision a cluster or replace the operator's identity, secrets, ingress, observability, or backup systems.

!!! note "Maturity"
    Current release `v0.28.1`: reference implementation and customer lab. Treat the customer values as a template. Production use requires customer integration, capacity tests, backup and restore evidence, and the strict validation gate.

## Start here

- [Local quickstart](quickstart.md) explains the downloads, side effects, and completion checks for the local lab.
- [Feature inventory](feature-inventory.md) lists the implemented API and platform features, including defaults and exclusions.
- [Architecture](architecture.md) describes the local and customer profiles and the restricted-egress tenant example.
- [Decision guide](decision-guide.md) covers fit, poor-fit cases, and the work a customer still owns.
- [Production readiness](production-readiness.md) maps platform controls to their validation commands.

## Operating and reviewing the project

- [Getting started](getting-started.md) collects the validation, local-cluster, eval, and evidence commands.
- [Runbooks](https://github.com/RamazanKara/private-ai-platform-kit/blob/main/runbooks/README.md) cover deployment, incidents, upgrades, data stores, and release checks.
- [Security overview](security-overview.md) states the important defaults and links to the [threat model](threat-model.md).
- [Release verification](release-verification.md) covers published images, charts, signatures, and checksums.
- [Scope and non-goals](scope-and-non-goals.md) defines the supported boundary.

The source repository also has a compact [documentation map](https://github.com/RamazanKara/private-ai-platform-kit/blob/main/docs/README.md) for browsing on GitHub.
