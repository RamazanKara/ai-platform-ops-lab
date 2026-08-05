# Security overview

This page summarizes the security-relevant defaults in release `v0.28.1`. The [threat model](threat-model.md) has the detailed trust boundaries and residual risks. The [production readiness matrix](production-readiness.md) lists validation commands.

## Defaults that matter

- The local profile uses the public key `local-development-only`. It is for the local lab only.
- In-cluster application traffic is plaintext HTTP. NetworkPolicy controls reachability, not encryption.
- The output guardrail and stored Responses state are off in the base chart.
- The local RAG profile disables tenant isolation because it uses one shared demo corpus.
- Without a tenant-bound key record or verified JWT claim, `X-Sandbox-ID` is caller-supplied.
- The agent-sandbox pod template is restricted, but a separate kernel boundary exists only when the cluster supplies and the values select an isolation `RuntimeClass` such as gVisor or Kata.
- The bundled Redis, Qdrant, and Loki footprints are single-node reference services.
- Sample reports under `results/` are not current security evidence.

Do not deploy the customer values unchanged.

## Gateway controls

The gateway supports API-key hashes and JWT/JWKS verification. Key records can add scopes, expiry, sandbox binding, and budget overrides. JWT configuration can bind a verified tenant claim to the sandbox. A contradictory `X-Sandbox-ID` is rejected when a binding exists.

Before forwarding an inference request, the gateway can enforce:

- allowed model IDs and routing policy;
- message, prompt, tool, completion, and batch size limits;
- per-sandbox rate and estimated-token budgets;
- input credential-pattern detection;
- a per-process concurrency limit and load shedding.

The optional output guardrail can flag, redact, or block configured credential, PII, and denied-content patterns. Redact and block behavior require a non-streaming response. Streaming can only be flagged after the stream has already been emitted.

These are deterministic application checks. They do not make model output trustworthy or prevent prompt injection.

## Tenant and RAG identity

The customer RAG values enable owner-based tenant filtering. That filtering is only a security boundary when the tenant identity is trustworthy.

The RAG service can verify its own JWT and derive the tenant from a claim. When JWT verification is off, it trusts `X-Sandbox-ID`; a caller with the shared key can assert another sandbox ID. Put direct RAG access behind a trusted identity-stamping path or enable RAG-side JWT verification for multi-tenant use.

Tenant NetworkPolicies default to deny and add explicit DNS, gateway, RAG, and reviewed external CIDR rules. Enforcement depends on the cluster CNI. The local cluster uses Calico by default because kindnet does not enforce NetworkPolicy.

## Workspace isolation

Agent workspaces use the vendored `kubernetes-sigs/agent-sandbox` controller and a restricted pod template: non-root user, read-only root filesystem, dropped capabilities, no ambient service-account token, resource limits, namespace RBAC, and default-deny egress.

The projected platform token is short-lived and audience-bound, but it is still a credential. The workspace can also exfiltrate through any approved destination. Keep the egress catalog narrow and treat files, retrieved text, model output, and tool arguments as untrusted.

## Audit records

The gateway audit event stores request metadata and hashes rather than raw prompt or completion text. Records are linked into a per-process hash chain.

The chain detects edits and reordering in an exported sequence. It does not by itself prevent deletion, survive a lost log stream, join replicas into one chain, or prove that the first and last records are complete. Export logs, retain the `chain_id`, and commit chain-head anchors to a separate trusted system. Use `make audit-verify` and follow the [audit-chain runbook](https://github.com/RamazanKara/private-ai-platform-kit/blob/main/runbooks/audit-chain.md).

Also review runtime, ingress, proxy, RAG, object-store, and application logs. Gateway redaction does not control what another component logs.

## Supply chain

Release workflows build the two first-party images, create SBOM and vulnerability-scan artifacts, sign image/chart digests with Cosign, and publish provenance. GitHub Actions are pinned by commit.

That boundary does not cover the integrity or license of customer model weights, customer base images, external Helm charts, private mirrors, or the target cluster. Verify those separately. Forks that publish their own images must update the Kyverno image reference and signing identity or admission will reject them.

## Production work left to the operator

At minimum:

- connect gateway and RAG auth to the customer identity boundary;
- source secrets from the customer secret system;
- enable transport encryption where required;
- select and test a kernel-isolation runtime for higher-risk agent workspaces;
- replace bundled stateful services with an appropriate availability and backup design;
- configure log export, retention, chain-head anchoring, alerts, and incident response;
- validate model artifacts, RAG ingestion, tenant binding, and egress rules;
- generate current eval, load, restore, policy, and supply-chain evidence.

## Related documents

- [Threat model](threat-model.md)
- [OWASP Top 10 for LLM Applications 2025 mapping](owasp-llm-top-10-mapping.md)
- [AI governance crosswalk](ai-governance-crosswalk.md)
- [Security policy](https://github.com/RamazanKara/private-ai-platform-kit/blob/main/SECURITY.md)
- [External stores](https://github.com/RamazanKara/private-ai-platform-kit/blob/main/runbooks/external-managed-stores.md)
- [Guardrails](https://github.com/RamazanKara/private-ai-platform-kit/blob/main/runbooks/guardrails.md)
