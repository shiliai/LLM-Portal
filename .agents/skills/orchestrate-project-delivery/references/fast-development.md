# Fast Development

Fast is an explicit user choice for isolated non-production, low-risk work. Route by risk, authority,
collaboration, and subject topology, never by task label. It supports interactive confirmation and
task-scoped YOLO confirmation. An explicit Fast Development request selects Outcome Kernel orchestration
for this task; a second mode-selection question is unnecessary. It must still satisfy Fast eligibility
and the approval requirements below. A bounded task selected for direct execution, including a generic
YOLO instruction without Kernel/Fast selection, bypasses the kernel and does not need this profile.

## YOLO Confirmation

Treat phrases such as "YOLO", "use your recommendation and execute", or "do not ask me again" as YOLO
only when they clearly authorize the current task. Do not infer a persistent preference. The coordinator
must still write an auditable user-story baseline, compact design, observable acceptance, and Contract;
the user's YOLO message authorizes the coordinator-recommended bundle, so do not stop for baseline,
design, Plan, or Contract confirmation and do not ask reversible technical questions.

At initialization, have the trusted task/runtime boundary bind the immutable `user_message` identity for that instruction into
`task_authorizations`. The Contract references exactly that identity and `freeze-contract` rejects any
coordinator-authored substitute. The public CLI `init` cannot populate `task_authorizations`; only a host
integration may call the initialization API with authenticated identities. The kernel proves identity
continuity but relies on that host to authenticate the original message. If a YOLO exit fact appears after initial policy evaluation, record it
immediately with `record-safety-facts`; this enters amendment before any further delivery mutation.

YOLO changes confirmation, not authority. It is eligible only when Fast is eligible and the requested
work needs no external mutation or destructive authority. Exit before further mutation and ask one
minimal decision upon production, sensitive data, shared persistent state, irreversible mutation,
direct release, scope expansion, risk acceptance, or an unavailable user-locked provider/model. A
second writer, overlapping generation, multiple integration subjects, or a standard review trigger also
exits Fast through the normal amendment path. Never auto-approve an amendment or accepted risk.

At freeze, prove: no production target, real sensitive data, shared persistent state, irreversible
external mutation, direct release, production compatibility/deploy/rollback/operations contract, or
standard review trigger; exactly one writer lineage, active generation, Work Item, and integration
subject; approved outcome, observable acceptance, minimum sufficient evidence, isolation boundary, and
exit conditions.

Flow: approved compact Contract/design, or a YOLO-authorized coordinator recommendation; uninterrupted single-worker delivery; immutable candidate;
minimum sufficient candidate check; activate minimal Journal; one strongest independent outcome review;
zero or one original-worker fix batch; rebuild only invalidated evidence; final Work Item alignment and
receipt. Development checks are worker feedback, not intermediate gates.

The reviewer judges only approved outcome, observable acceptance, and evidence sufficiency. Freeze check
failures and blocking findings into one batch. There is no plan approval or closure review. A changed core
acceptance method preserves old/new evidence identities and reason. A changed goal or observable result
requires amendment.

Fixed invariants are `review_count <= 1`, `fix_batch_count <= 1`, and `closure_count = 0`. Failure after
final verification enters degraded, blocked, or amendment; it cannot start another review/fix.

Production/shared/sensitive/irreversible/release scope, second lineage, overlapping active generation,
multiple integration subjects, or any standard trigger enters `amendment_pending` before more delivery.
Preserve evidence only when valid under the new standard Contract; never inherit Fast exemptions.
