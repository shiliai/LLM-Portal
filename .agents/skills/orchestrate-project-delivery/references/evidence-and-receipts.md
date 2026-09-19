# Evidence And Receipts

Every evidence record has ID, immutable subject kind/value, source system, collection time, verification,
result, valid-when, invalid-when, and reuse scope. Invalid evidence stays in history but cannot support a
decision, alignment, or receipt.

Commit, manifest, artifact, file/directory hash, normalized runbook/command hash, deployment revision, and
external revision are valid subject representations. Plans, scripts, environment instructions, token
counts, model metadata, and document counts are not First Evidence unless the document/artifact itself is
the contracted subject.

For a large artifact, perform a full check once. Reuse manifest SHA, file count, total bytes,
configuration identity, and fixed sample while validity holds. Re-run the full check only when identity
or validity changes. A fix rebuilds only evidence invalidated by its diff.

Work Item receipts bind outcome, current subject, focused evidence refs, alignment, and remaining risk.
Final receipts bind Contract version/hash, final subject and identity chain, valid evidence,
`story_alignment`, risk disposition, recovery status, actual execution identity, and deferred items.
Committed reports and evidence carriers are part of the final subject: create them before final E2E, or
treat the later commit as a new subject that requires a continuous identity edge and new final evidence.
Do not create a report-only Git commit after E2E and close it with evidence from its parent.

Alignment records the Contract baseline revision and the exact required story IDs. Every story includes
role, goal, value, Given/When/Then, subject-bound evidence refs, and `Complete|Partial|Missing`. Any drift
or incomplete story requires exact accepted-deviation authorization. `passed` requires every story
`Complete` and drift zero. Final receipts must include the required E2E/Fast evidence, match their
disposition and restore point, and Fast receipts must state `production_ready=false` with the frozen
isolation boundary.

`passed` allows only out-of-acceptance maintenance/readability/enhancement deferrals. Known Contract
variance or activated safety/data/operations risk requires explicit accepted risk, degraded, or blocked.
Fast receipts state isolation and never imply production readiness.
