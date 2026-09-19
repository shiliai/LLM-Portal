# Work Items And Worker Capsules

A Work Item is one observable capability or cohesive operation. It includes owner/lineage, starting and
current immutable subject, allowed/excluded scope, 1-3 stable story IDs, acceptance and focused evidence,
mutation authority, generation, dependencies, and exact next action.

States are `pending -> claimable -> active -> evidence_ready -> integrated -> aligned -> completed`.
`active|evidence_ready -> blocked -> active` preserves lineage, scope, authority, and budgets unless an
approved amendment changes them. Dependencies alone decide claimability. Alignment requires drift zero
or user acceptance of the exact deviation.

Only one active writer may own a subject. Return failures to the original lineage. Fast has exactly one
Work Item and one writer lineage; its read-only outcome reviewer is not a writer.

Each Work Item keeps a distinct lineage history. A claim never overwrites an existing lineage. Rotation
requires recorded correction/compaction counters or explicit trigger evidence, releases the old
generation, and records the new generation's actual provider/model/session. Standard convergence names
one active `integration_owner`; that lineage alone performs frozen-finding and E2E repair even when
multiple Work Items have different owners.

Create a Capsule after material subject revision, approved review plan, or failure attribution. Include
Contract version/hash, story IDs, subject, scope, frozen findings/fix plan, modified subjects/files,
valid evidence, blockers, safety constraints, and exact next action. A new generation reads only this
Capsule plus current subject/workspace.

Rotate after two correction/verification/failure-attribution rounds, two compactions, 45 minutes without
material progress, repeated exploration/Contract misunderstanding, or proactively after fix-plan
approval. Release the old generation claim before activating the next. Keep the lineage and all budgets,
findings, attempts, and authority. Maximum three generations per Work Item; then split, amend acceptance,
or STOP.

Standard convergence atomically binds every completed Work Item subject, one immutable convergence
subject, integration evidence for that subject, and the integration owner. Review, closure, E2E, repair,
alignment, and receipts must follow this subject chain; arbitrary replacement identities are rejected.
