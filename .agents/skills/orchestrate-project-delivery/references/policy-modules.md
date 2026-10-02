# Policy Modules

Policy modules are side-effect-free decisions. Only `opd.py` mutates state or performs delivery action.

Execution chooses, in order: a known idempotent observable command; a trusted versioned runbook; a thin
script for reversible delivery-specific multi-step work; or full automation for repeated, fragile,
cross-system, or difficult recovery. Bind every external operation to commit, artifact, command, or
runbook identity. Prefer a real canary over an elaborate fake harness.

Persistence becomes durable before external mutation, multiple agents, work over 30 minutes, or multiple
repositories/integration subjects. It never downgrades. Review activity itself activates persistence
only when review is actually required.

`evaluate-policies` is a one-time transition for a frozen Contract version. Its decisions and exact
`journal_triggers` are immutable until a material amendment. First Evidence, Work Item creation, worker
dispatch, and Journal activation reject absent or caller-invented policy decisions. A second worker or
any reviewer is dispatched only after the policy-backed Journal commit.

Route implementation by capability and context; use strongest independent reviewers for activated risk;
use tools for E2E commands and agents only for anomaly analysis. Record actual provider/model/session.
Never substitute a user-locked provider or model without approval.

Set standard `review_required` only for P0/identity/approval/security/credential/protected-data/
irreversible mutation; a cross-service shared contract affecting independent consumers; changed
release rollback/restore/data compatibility; or explicit standard broad review. Risk follows mutation,
data, authority, irreversibility, and blast radius rather than task size.

Preparation limit is `min(critical_path_minutes * 0.2, 20)`. Once spent, reduce preparation, reuse valid
evidence, or reroute. Do not weaken Contract authority or activated controls.

Default reporting is material-only. An unchanged wait is not a user update. A configured heartbeat only
reports a structured snapshot when it changes a decision.
