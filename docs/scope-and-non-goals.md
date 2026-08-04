# Scope and non-goals

This page defines the boundary of release `v0.28.0`. For per-feature defaults, use the [feature inventory](feature-inventory.md).

## In scope

The repository contains and tests:

- the inference gateway and RAG service under `src/`;
- Helm charts for those services, Ollama, vLLM, Redis, Qdrant, and agent workspaces;
- local and customer Argo CD application manifests;
- Kubernetes policy, tenant templates, model catalog records, eval definitions, and SLO inputs;
- API and configuration contracts;
- validation, evidence, release, and supply-chain scripts;
- operational runbooks and customer handoff documentation.

The gateway implements these protocol families:

- OpenAI-style chat completions, legacy completions, embeddings, moderations, models, Files, Batch, and Responses;
- the project-specific synchronous `/v1/batch-inference`, usage, and sandbox-budget endpoints;
- a non-streaming Anthropic Messages translation endpoint.

The generated [OpenAPI contract](https://github.com/RamazanKara/private-ai-platform-kit/blob/main/platform/api-contracts/inference-gateway.openapi.json) is the route-level reference.

## Protocol limits

This is not a complete OpenAI or Anthropic API implementation.

- Chat completions support streaming. Legacy completions, Anthropic Messages, and Responses do not.
- Responses supports the synchronous request shape. Optional stored state supports `store`, `previous_response_id`, retrieve, delete, and input-items routes. Background responses are not implemented.
- The asynchronous Batch implementation accepts chat completions, completions, and embeddings. `completion_window` is treated as an expiry bound, not a scheduling or pricing commitment.
- Audio, image generation, fine-tuning, and a general training API are not implemented.
- Translated Messages and Responses payloads preserve supported text and tool fields but do not promise byte-for-byte parity with upstream services.

## Operator-owned work

The project does not:

- create or upgrade a Kubernetes cluster;
- provision networks, load balancers, GPU nodes, or cloud databases;
- run an identity provider or secret manager;
- provide a production ingress, certificate authority, logging service, backup destination, or incident team;
- select, host, license, or validate customer model weights;
- classify customer data or decide whether a use case is regulated;
- size replicas, GPU memory, context windows, storage, retention, or SLOs for a customer workload;
- operate the resulting platform.

The customer values are examples that must be reviewed. Their placeholders, large GPU defaults, and single-node stateful services are not production recommendations.

## Out of scope

The project is not intended to become:

- a hosted AI gateway or managed Kubernetes service;
- a desktop Ollama application;
- a cloud-infrastructure provisioning framework;
- a distributed training platform;
- a general multi-node serving operator;
- a full billing system;
- a multi-tenant administration product with user management and write operations.

The read-only `/console` is an optional view over health, models, usage, and budget data. It is not a control plane.

## Security and compliance boundary

The repository includes controls and crosswalks, not certifications. NetworkPolicy does not encrypt traffic. A container sandbox without a configured gVisor, Kata, or equivalent `RuntimeClass` does not provide a separate kernel boundary. Hash-chained logs are not durable or rollback-resistant until their heads are anchored outside the process. Checked-in sample evidence proves report shape, not the state of a release or deployment.

Whether a deployment meets a law, standard, or internal policy depends on its use case, configuration, operations, and current evidence. See the [security overview](security-overview.md), [threat model](threat-model.md), and [production readiness matrix](production-readiness.md).
