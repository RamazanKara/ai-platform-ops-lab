# Audit Chain Verification & SIEM Forwarding Runbook

Use this runbook to verify the gateway's tamper-evident audit hash chain, to anchor its head so
a wholesale rewrite is detectable, and to forward the audit stream to a SIEM for long-term,
independent retention.

## What the audit events are

Every sandbox-bound gateway request emits one redacted audit event (`event: inference_request`;
batch calls emit `event: batch_request`). Three more event types share the same chain: an
`agent_action` receipt for what a workspace did beyond calling a model (denied egress, tool
execution, file writes; see [ADR 0014](https://github.com/RamazanKara/private-ai-platform-kit/blob/main/docs/adr/0014-agent-action-receipts.md)),
a `rag_query` retrieval receipt from the RAG service on its own chain, and a `chain_start`
record opening each chain. These events are the **tamper-evident receipts**: each
is linked into a per-process SHA-256 hash chain (see
[ADR 0006](https://github.com/RamazanKara/private-ai-platform-kit/blob/main/docs/adr/0006-tamper-evident-audit-hash-chain.md)):

- `h_0 = SHA-256("genesis")`
- `record_hash = SHA-256(prev_hash || canonical(record))`, where `canonical` is
  `json.dumps(record, sort_keys=True, separators=(",", ":"))` over the event **before** the
  `prev_hash`/`record_hash` fields are stamped on.

Each event carries a `chain_id` (`HOSTNAME:process_start:random`, hash-covered) identifying its
per-replica chain, and a chain-covered `ts`. The random suffix is load-bearing: a pod that
restarts twice within one second would otherwise mint the same id twice and the verifier would
splice two unrelated chains together and report the seam as tampering. Events are logged twice per request (once to the
audit logger `ai_platform_ops_lab.audit` and once to `uvicorn.error`), so a pod-log stream (and
Loki) carries two byte-identical copies of every record. The verifier deduplicates them.

Any edit, insertion, deletion, or reordering of emitted records breaks the chain and is detected
by recomputation. A *wholesale* re-chain (every record rewritten from genesis so the embedded
hashes stay self-consistent) is only detectable against an externally committed head, which is
what anchoring provides.

## Verify a log

Export the gateway pod logs (or a Loki export) to a file, then:

    make audit-verify AUDIT_LOG=/path/to/gateway-audit.log

or pipe on stdin:

    kubectl -n inference logs deploy/inference-gateway | python3 scripts/audit-verify.py -

The verifier deduplicates the double-logged copies, groups records by `chain_id` (older logs
without it are split at genesis-restart boundaries), and reports per chain: the record count and
`OK`, or the first broken position and reason (`record_hash_mismatch`, `broken_link_or_reordered`,
`prev_hash_not_genesis`). It exits non-zero on any break. A per-process chain per gateway replica
and a fresh chain after each restart are expected; verification is per chain.

Self-test (also wired into `make validate`):

    python3 scripts/audit-verify.py --selftest

## Anchor the head (detect wholesale re-chaining)

Compute and store each chain's head so a later verify can detect a rewrite/rollback:

    make audit-anchor AUDIT_LOG=/path/to/gateway-audit.log AUDIT_ANCHOR=/path/to/anchor.json

The anchor file records, per `chain_id`, `{count, head record_hash}`. Later, compare a freshly
observed log against it:

    python3 scripts/audit-verify.py /path/to/new-export.log --anchor /path/to/anchor.json

This flags a **shrunk chain** (truncation/rollback), a **changed head without growth**
(re-chain/edit), or a **missing chain**. Because only the head is committed, the anchor file is
tiny and append-safe: honest appends advance a chain's head and count; they never rewrite a
previously anchored head. Store the anchor externally (a ConfigMap, an object-store bucket, or a
SIEM index) so it is outside the reach of whoever could rewrite the log.

### Scheduled anchoring in-cluster

A ready-to-adapt CronJob that pulls the gateway audit stream from Loki, anchors it, and stores the
head in a ConfigMap ships as a documented example at
[`docs/examples/audit-anchor-cronjob.yaml`](https://github.com/RamazanKara/private-ai-platform-kit/blob/main/docs/examples/audit-anchor-cronjob.yaml). It is an
example rather than a rendered manifest because the log source (Loki URL, LogQL selector, tenant
header, lookback) is environment-specific; adapt those fields before applying. The scripts are
stdlib-only, so any `python:3-alpine` with `scripts/audit-anchor.py` and `scripts/audit-verify.py`
mounted works.

## Forward the audit stream to a SIEM

The audit receipts should not live only in Loki (which is single-tenant and retention-bounded by
default; see [data retention](data-retention.md)). Ship them onward to a SIEM for independent,
long-term, ideally write-once retention. Two supported shapes:

1. **Add a second Promtail/Grafana Alloy sink.** Point an additional `clients[]` entry (Promtail)
   or a second `loki.write` / `otelcol` exporter (Alloy) at the SIEM's ingest endpoint, scoped to
   the gateway audit stream (match on the `record_hash` field or the audit logger name). This
   duplicates the stream to the SIEM without disturbing the in-cluster Loki path.
2. **Export from Loki.** Run a scheduled LogQL query
   (`{app_kubernetes_io_name="inference-gateway"} |= "record_hash"`) and forward the results to
   the SIEM (for example via a Logstash/Vector/Fluent Bit Loki source, or the CronJob pattern
   above adapted to write to the SIEM instead of a ConfigMap).

**Also export the anchor.** Ship the anchor file / ConfigMap (`audit-chain-anchor`, key
`head.json`) to the SIEM alongside the events. The events prove internal consistency; the anchor
is what proves the events were not wholesale-rewritten. Without the externally held anchor, a
re-chain is undetectable.

If Loki `auth_enabled` is turned on for multi-tenant isolation, add the `X-Scope-OrgID` tenant
header on **every** path: the Promtail push (`clients[].tenant_id`), the Grafana datasource, and
any anchor/export query. Otherwise pushes and reads will 401. The bundled reference keeps
`auth_enabled: false` (single-tenant); see the comment in
[`deploy/observability/applications.yaml`](https://github.com/RamazanKara/private-ai-platform-kit/blob/main/deploy/observability/applications.yaml).

## Chain continuity across restarts

A chain covers one process lifetime. On its own that left a seam: a restarting pod began a
fresh chain at genesis with nothing tying it to the one before, so an attacker who deleted a
whole replica's chain produced something indistinguishable from an ordinary restart.

Each process now opens its chain with a `chain_start` receipt naming the previous chain and
the head it had reached, which makes the chains into a chain of their own. That record is
hash-linked like any other, so the claim it makes about its predecessor is covered by the
chain it opens.

It only works if the head outlives the pod, so pick a store:

    # File on a mounted volume
    AUDIT_CHAIN_STORE_BACKEND=file
    AUDIT_CHAIN_STORE_PATH=/var/lib/inference-gateway/audit-chain-head.json

    # Or Redis, if the deployment already runs one
    AUDIT_CHAIN_STORE_BACKEND=redis
    AUDIT_CHAIN_STORE_KEY=inference-gateway:audit-chain-head

The default is `memory`, which keeps the pre-continuity behavior: chains still verify
individually, and each `chain_start` honestly reports no predecessor rather than claiming one
it cannot support. The RAG service has the same settings under `RAG_AUDIT_CHAIN_STORE_*`.

The head is written on an interval (`AUDIT_CHAIN_PERSIST_INTERVAL_SECONDS`, default 5) and on
graceful shutdown, not on every record, so a head-store write never sits on the request path.
The cost is that a hard crash can leave the persisted head a few records behind the true tail;
the verifier accounts for this by checking the named head appears in the predecessor rather
than requiring it to be the predecessor's last record.

`audit-verify` reports continuity alongside the per-chain result:

    python3 scripts/audit-verify.py exported-audit.log

- A predecessor that is **present but disagrees** (truncated, or rewritten so its head
  changed) is a failure.
- A predecessor that is simply **absent** is a note, not a failure, because verifying a
  window of rotated logs legitimately starts mid-history. Pass `--strict-continuity` when the
  input is meant to be a complete history and a missing predecessor should fail.

## Offline auditor path

An auditor does not need cluster access. Hand them the exported audit log and the anchor file;
the verifier is stdlib-only Python:

    python3 scripts/audit-verify.py exported-audit.log --anchor exported-anchor.json

A clean exit (code 0) with every chain `OK` and no anchor problems means the receipts are intact
and were not rewritten since the anchor was taken. The same tooling runs against the checked-in
sample (`results/sample-gateway-audit.log`) as a smoke reference.

## Related

- [Data retention](data-retention.md): retention/redaction policy for the audit logs.
- [Traceability sandbox](traceability-sandbox.md): request correlation and the sandbox trace contract.
- [Evidence pack](evidence-pack.md): customer-facing evidence generation.
