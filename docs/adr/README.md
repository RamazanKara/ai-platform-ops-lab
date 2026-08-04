# Architecture Decision Records

This directory records the significant architecture decisions behind Private AI Platform Kit:
why a particular tool, pattern, or boundary was chosen, what was rejected, and what the choice
costs. Read an ADR when you want the reasoning behind a default in `deploy/`, `src/`, or
`platform/`, not just the configuration itself.

ADRs are not a manual. For setup follow [docs/getting-started.md](../getting-started.md); for
project fit and alternatives follow [docs/decision-guide.md](../decision-guide.md); for the
controls inventory follow [docs/production-readiness.md](../production-readiness.md). ADRs explain
the choices those documents assume.

## Status legend

- `Accepted`: the decision is in force and reflected in the repo today.
- `Superseded by NNNN`: replaced by a later ADR; kept for history.
- `Proposed`: under discussion, not yet reflected in the default configuration.
- `Deprecated`: no longer recommended, but not yet removed.

All ADRs in this set are `Accepted` and describe the current repository.

## Index

| ADR | Title | Status |
| --- | --- | --- |
| [0001](0001-gitops-with-argo-cd.md) | GitOps delivery with Argo CD | Accepted |
| [0002](0002-policy-engine-kyverno.md) | Policy engine: Kyverno | Accepted |
| [0003](0003-inference-runtime-vllm-and-ollama.md) | Inference runtimes: vLLM and Ollama | Accepted |
| [0004](0004-vector-store-qdrant.md) | Vector store: Qdrant | Accepted |
| [0005](0005-openai-compatible-gateway.md) | A thin self-built OpenAI-compatible gateway | Accepted |
| [0006](0006-tamper-evident-audit-hash-chain.md) | Tamper-evident audit hash chain | Accepted |
| [0007](0007-local-first-kind-then-customer-cluster.md) | Local-first kind, then customer cluster | Accepted |
| [0008](0008-helm-packaging-and-oci.md) | Helm packaging and OCI distribution | Accepted |
| [0009](0009-adopt-agent-sandbox-workspace-runtime.md) | Adopt kubernetes-sigs/agent-sandbox as the coding-agent workspace runtime | Accepted |
| [0010](0010-agent-sandbox-standard-runtime.md) | Agent-sandbox is the standard workspace runtime | Accepted |
| [0011](0011-async-files-and-batch-api.md) | Asynchronous Files and Batch API | Accepted |
| [0012](0012-stateful-responses-store.md) | Server-side response state for the Responses API | Accepted |
| [0013](0013-bundled-admin-console.md) | Opt-in bundled read-only admin console | Accepted |
| [0014](0014-agent-action-receipts.md) | Agent-action receipts on the audit chain | Accepted |

## Process

ADRs use the [MADR](https://adr.github.io/madr/)-style template captured below. They are short,
immutable once `Accepted`, and grounded in real files in this repo.

1. Copy the template into `docs/adr/NNNN-short-kebab-title.md`, using the next free four-digit
   number (zero-padded, never reused).
2. Fill in Status, Context, Decision, Consequences, and Alternatives considered. Cite concrete
   paths (charts, manifests, source modules) so a reviewer can check each claim.
3. Add a row to the index table above (ADR pages are reached through this index; only
   the index itself is listed in the `mkdocs.yml` nav).
4. Open it for review like any other change (see [CONTRIBUTING.md](https://github.com/RamazanKara/private-ai-platform-kit/blob/main/CONTRIBUTING.md)). Once
   merged and `Accepted`, do not rewrite it; if the decision changes, write a new ADR and mark the
   old one `Superseded by NNNN`.

### Template

```markdown
# NNNN. Title

- Status: Proposed | Accepted | Superseded by NNNN | Deprecated
- Date: YYYY-MM-DD
- Deciders: <roles or names>

## Context

What problem forces a decision? What constraints (local-first, provider-neutral, single
maintainer, regulated tenants) apply?

## Decision

What was chosen, stated plainly, with the concrete repo evidence (paths, defaults).

## Consequences

What this makes easy, what it makes harder, and what the operator now owns.

## Alternatives considered

Each realistic option, what it does better, and the specific reason it was not chosen here.
```
