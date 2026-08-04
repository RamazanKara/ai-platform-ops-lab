# 0014. Agent-action receipts on the audit chain

- Status: Accepted
- Date: 2026-08-04
- Deciders: Platform maintainer

## Context

The kit's claim is that you can prove what a coding agent did. What the audit chain actually
proved was narrower: every record carried `action_type: "model_call"`, and that was the only
value the gateway ever emitted. The chain is a complete, tamper-evident record of what an agent
asked a model, and says nothing about what the agent then did with the answer.

The gap shows up in the project's own demo. The most persuasive moment in
`make agent-sandbox-demo` is the blocked exfiltration attempt, and it produces a NetworkPolicy
packet drop: real enforcement, no receipt. An auditor asking "show me every time this agent tried
to reach the internet" gets a CNI log if the operator kept one, not something `make audit-verify`
can check offline. The same is true for the actions that matter most in a workspace: commands
executed, files written, credentials requested.

The enforcement for these already exists and is not in the gateway. Egress is denied by
default-deny networking, workload behavior is watched by the opt-in Falco/Tetragon application,
and the workspace is a hardened sandbox. What is missing is not a control. It is the evidence
trail that ties those controls to the same verifiable chain as the model calls.

## Decision

Add an authenticated receipt intake to the gateway: `POST /v1/receipts`, off by default
(`AGENT_RECEIPTS_ENABLED`). It accepts a typed agent action and links it into the same
per-process hash chain as model calls, with the same redaction discipline and the same operator
verifier.

The action taxonomy is closed and small: `egress_denied`, `egress_allowed`, `tool_exec`,
`file_write`, `credential_request`, `workspace_lifecycle`. An unrecognized `action_type` is
rejected rather than chained, so the taxonomy stays a reviewed vocabulary instead of drifting
into free text that no report can aggregate.

The **boundary is the important part of this decision**: the gateway accepts and chains
receipts. It does not enforce sandbox-internal policy, and a receipt is never treated as
permission for anything. A submitted receipt is a claim by the sandbox about something that
already happened, recorded so it can be audited; it is not the control that stopped it. Whether
an action was actually blocked is decided by the NetworkPolicy, the Kyverno policy, or the
runtime-security agent, exactly as before.

Because a receipt is an attributable claim, the intake is bound to the caller's identity the
same way every other endpoint is: the sandbox on the receipt comes from the caller's bound
sandbox (the audience-bound workspace token or an API-key record), and a receipt claiming a
different sandbox is rejected. A workspace can add to its own history and cannot write another
tenant's.

Producers are whatever the operator already runs. Falco and Tetragon alerts, CNI denied-flow
logs, and an agent's own tool hooks are all just callers of this endpoint; the kit ships the
intake, the taxonomy, the chaining, and the verification, not a collection agent.

## Consequences

- "Prove what it did" becomes checkable offline for more than model calls: `make audit-verify`
  verifies agent actions and model calls in one chain, and a deleted action receipt breaks the
  chain like any other record.
- The chain's value now depends partly on producers the kit does not control. A receipt stream is
  only as complete as what the operator wired into it, and the docs say so rather than implying
  full coverage. Completeness is an operator property; integrity is the kit's.
- Receipts are self-reported by the sandbox, so they are evidence of claims, not proof of
  behavior. This is why the boundary above matters: an agent that never reports a `tool_exec`
  simply has no receipt for it, and only the out-of-band producers (Falco, CNI) close that. The
  chain makes tampering with reported history detectable; it does not make unreported history
  appear.
- One more opt-in endpoint on the gateway, with a bounded body, its own rate limit path, and a
  closed vocabulary, so an enabled intake cannot become an unbounded log sink.

## Alternatives considered

- **A separate receipts service.** Cleaner separation, but the chain is per-process and the
  operator verifier groups by `chain_id`; a second service means a second chain to anchor and
  correlate for no gain in the single-cluster topology this kit targets. Rejected. (The RAG
  service does run its own chain, but it is an independently deployed service with its own
  lifecycle; a receipts sink would exist only to hold receipts.)
- **Deriving receipts from Falco alerts inside the gateway.** Would make the gateway parse and
  poll another system's event format and turn a governance service into a log collector, coupling
  the kit to a specific runtime-security stack it deliberately ships as optional. Rejected in
  favor of an intake anything can post to.
- **Free-form `action_type` strings.** Simpler to accept, but the crosswalk and evidence pack
  aggregate by action type; free text makes every report a best-effort string match and makes the
  taxonomy unreviewable. Rejected.
- **Trusting a submitted `sandbox_id`.** Would let any workspace write into another tenant's
  history, which is precisely the property that makes the chain worth anything. Rejected.
- **Leaving it as an operator concern (status quo).** The receipts already exist as logs; the kit
  could keep documenting that operators forward them to a SIEM. Rejected because the differentiator
  is verifiable evidence, and a log an auditor cannot verify offline is the thing this project
  exists to improve on.
