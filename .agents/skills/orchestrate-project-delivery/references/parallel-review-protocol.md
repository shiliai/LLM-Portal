# Parallel Review Protocol

This protocol applies only when Risk Policy sets `review_required=true` on a stable standard subject.

Start exactly three isolated reviewers on the same immutable identity: business/story/boundary behavior;
quality/security/data integrity; and docs/deploy/rollback/operations. Reviewers do not see peer reports.
Record actual provider/model/session and keep one campaign for the delivery.

Reviewer identities and sessions are pairwise distinct and cannot overlap any worker lineage/session.

After all three reports arrive, the Coordinator deduplicates/conflict-resolves and atomically freezes one
finding batch while moving `collecting -> plan_pending`. Every finding binds subject, path/evidence,
affected story/invariant, severity, and closure condition.

The original worker submits one complete fix plan mapping every finding to code, tests, docs, and expected
evidence. The same reviewers assess only that plan. One rejected plan may be revised once; a second
rejection STOPs. The original lineage then performs one complete batch. It records `record-fix`, binding
the new immutable subject, one diff identity, and an exact finding-to-change/evidence mapping. Each mapping
must name one frozen finding, at least one change reference, and valid evidence for the new subject.

No reviewer closure is dispatched, resumed, or accepted after `begin-fix`. `record-closure` is retired and
rejected, so reviewer reports cannot self-certify a repair. The campaign becomes `fixed` only from the
deterministic mapping. A legacy durable campaign already at `closure_pending` resumes at `record-fix`; it
cannot manufacture historical closure reports.

E2E starts directly from the fixed subject. Every failure binds suite, subject, and real failure IDs. The
original lineage maps each repair diff to those IDs. A P0-touching repair records an auditable deterministic
P0 evidence gate on invariant, failure IDs, diff identity, current subject, and valid evidence before
recheck; it does not dispatch a reviewer. Other repairs proceed directly to recheck. Final closeout binds
the campaign subject, fixed subject, every repair diff, P0 evidence, and final E2E subject continuously.
Any committed report or evidence carrier must already be included in the subject before final E2E. A later
Git commit is a new subject and cannot be closed using the earlier E2E result.
