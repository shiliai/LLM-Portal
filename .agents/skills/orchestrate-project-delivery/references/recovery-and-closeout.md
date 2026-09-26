# Recovery And Closeout

Durable layout is `execution/delivery-v2/<id>/` with immutable Contract versions, append-only journal,
replayable snapshot, evidence index, Work Items, worker generations, reviews, and receipts. All paths are
project-relative and secret-free.

Activation validates ephemeral canonical state, writes Contract plus one `journal_bootstrap` containing
pre-state, valid evidence refs, from revision/hash, activation ID, and target hash into a temporary
directory, independently replays it, fsyncs files/directory, and renames once to the final path. Rename is
the commit point. Pre-rename failure leaves ephemeral state; post-rename recovery adopts the one existing
activation idempotently. Replay never depends on transcript or invented pre-activation events.

All later mutations require expected revision. Rejection is zero-write. Journal revision and snapshot
revision agree; snapshot is rebuilt by replay. `record-event` accepts only non-state material events.
Repeated `resume` returns the same exact action and preserves claims.

After activation, the append-only Journal is authoritative. `status`, `resume`, and `validate` replay it
without repairing files and report whether derived files are current. If a crash commits a Journal event
before snapshot/index materialization, recovery returns the replayed revision and the next checkpoint or
state mutation deterministically rematerializes derived files. Activation crash adoption requires the
same activation ID and original expected revision; another ID cannot adopt the directory.

Any nonterminal state may save `resume_state` and enter amendment or blocked. Invalidating amendment
returns to First Evidence; reporting-only amendment resumes. Authorized retry records actor, reason,
scope, and evidence. E2E deterministic-P0-evidence STOP cannot be bypassed by retry. A durable journal
written by the retired implementation may replay `review.state=closure_pending`; recovery treats that as
an explicit compatibility state whose only forward action is `record-fix`. It never asks for or accepts
invented reviewer closure reports. Historical `review.state=passed` remains replayable read-only for
already completed campaigns, but no new mutation can create it. External waits persist a
recovery condition and end the active turn without polling.

Closeout dispositions are mutually exclusive. `passed` has no unresolved Contract variance, activated
risk, or observable defect. `accepted_risk` records explicit user authorization for exact difference,
scope, and evidence. `degraded` safely closes unmet outcome. `blocked` is a resumable pause, not closeout.
Terminal state and receipt are immutable; further work needs a new delivery or approved new Contract.

A material amendment invalidates evidence, releases claims, archives and removes active Work Items and
worker generations, and clears convergence/review/Fast/E2E/subject-chain/receipt state before returning
to First Evidence. A reporting-only amendment resumes all of those structures unchanged. A Fast exit
placeholder requests `amend-contract` until a complete replacement Contract exists.
