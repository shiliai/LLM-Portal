# Delivery Contract

The Contract freezes only decisions that alter outcome, authority, or recovery semantics.

Required regions are `intent`, `subjects`, `authority`, `outcome`, `recovery`, `budgets`, and
`reporting`. Intent contains hashed authority sources, baseline revision, profile, confirmation mode,
goal, and non-goals. Confirmation mode is `interactive` unless the user explicitly grants task-scoped
`yolo` authority.
Subjects contain repository/base identity, allowed and excluded paths, artifacts, integration subjects,
and external targets. Authority covers local write, external mutation, destructive boundaries, and
provider/model locks. Outcome defines First Evidence, observable acceptance, and final evidence.
Recovery defines restore point, maintenance window, and failure strategy. Budgets include critical-path,
preparation, host-attempt, and review limits. Reporting defaults to material-only.

Freeze immutable `contract_version` and content hash after approval. New Git SHAs, test results, and host
facts are evidence, not amendments. Create and approve an amendment for changed goal/non-goal/observable
outcome; repository, remote, PR/deploy target, mutation surface, paths, or protected data; recovery
semantics; an unsatisfied locked provider/model; or accepted risk.

An invalidating amendment returns to First Evidence and invalidates affected evidence. A reporting-only
amendment may return to the saved state. Terminal deliveries are never revived.

Local and external Work Item authority must be explicitly enabled by the Contract. Every external
mutation binds a Contract target ID and immutable identity, an allowed reversible/destructive operation,
an immutable automation identity, and a unique idempotency key. Destructive operation additionally
matches the frozen boundary authorization exactly.

Fast adds explicit user choice, observable acceptance, isolation boundary, one writer, one integration
subject, minimum sufficient evidence, and automatic exit conditions. Fast exemptions never carry into a
standard amendment.

YOLO is valid only with Fast. Its Contract also records the exact immutable `user_message` authorization
identity supplied by the trusted task/runtime boundary at `init`, the
coordinator's recommended baseline/design/Contract identities, the four auto-approved artifacts
(`user_story_baseline`, `compact_design`, `delivery_contract`, `reversible_execution`), and automatic
exit conditions. `freeze-contract` consumes that same identity instead of accepting coordinator-authored
approval evidence. The local kernel validates identity continuity, not the host application's message
authentication. The public CLI `init` intentionally has no authorization argument; only a trusted host
integration may call the initialization API with authenticated task authorizations. Until a host supplies
that integration, CLI-created deliveries cannot freeze YOLO Contracts. YOLO never grants external
mutation, destructive authority, risk acceptance, scope expansion, or authority beyond the user's request.
