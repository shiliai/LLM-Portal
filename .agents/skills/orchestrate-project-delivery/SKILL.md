---
name: orchestrate-project-delivery
description: "Carry complex, multi-stage, multi-repository, long-running, deployment, or E2E work through implementation, verification, and handoff. Use lightweight planning and outcome evidence by default; load the optional Outcome Kernel only when explicitly required or resuming its existing state. Clear bounded work can proceed directly."
---

# Project Delivery

Deliver the user's intended result with the least process that keeps the work correct and recoverable.
Treat the model as a capable collaborator: specify the outcome, real constraints, and acceptance;
let it choose the implementation, task breakdown, and tools. User instructions take precedence over
this skill's workflow preferences.

## Start from the task

Read enough project context to establish the requested outcome, current state, relevant constraints,
and how success can be observed. Reuse a current approved Spec/Plan and story baseline. A clear user
request can supply this authority; missing formal documents alone do not require a design phase.

Proceed with reasonable assumptions for reversible technical choices. Ask only when missing information
would materially change the outcome, exceed authorization, or accept a known deviation. Complete
independent authorized work while a needed decision is pending. Do not ask the user to select a delivery
mode, approve an unchanged plan, or confirm routine fixes. If product intent genuinely needs design,
use the bundled `brainstorming` skill for that decision and carry its approval forward.

## Plan and execute proportionally

For bounded work, implement and verify directly. For coupled or longer work, keep a short plan of
observable outcomes, dependencies, and required checks. Update it when facts change. Use existing
project planning files; create a durable note only when interruption or handoff would otherwise lose
important state. Record completed work, evidence, unresolved decisions, and the next useful action.
A fixed phase sequence, DAG, time budget, JSON receipt, or state machine is not required by default.
Keep the plan linked to its authoritative request/Spec; resolve reversible implementation conflicts
from that authority and current project facts, recording consequential changes.

Delegate when permitted and an independent subtask would improve speed or confidence. Give each worker
an outcome, ownership boundary, relevant context, and acceptance evidence; prevent overlapping writers.
Keep dependent work local and retain ownership of integration. Use the user's selected provider/model
and inherit host defaults unless the task explicitly assigns another. The lightweight route does not
force a role count, model tier, or review campaign; an active Kernel follows its own contract.
Batch small changes with shared context when useful. A child report is a claim to check against the actual result.

Before external changes, confirm the target and existing authority, prepare the change and any necessary
recovery path, and verify the result afterward. When authorization is still needed, present the concrete
prepared result for that action. Existing authorization persists; a new phase is not a new permission gate.
After interruption, inspect actual files, Git, tests, and external state before resuming. Check whether a
previous action succeeded before repeating it.

## Continue until outcome or blocker

For a request to change, build, fix, deploy, or review, treat an acknowledgement as progress context,
not completion. If required work remains and there is no material blocker or user decision, immediately
take the next useful tool action in the current turn when the runtime permits. Do not end the turn with a
final response that only says “收到”, “我会继续”, “处理中”, or otherwise promises future work.

Before ending a turn, check that either the requested success criteria are met with current evidence, or
a concrete blocker/decision is being reported. If neither is true, keep working. For work that must span
turns, preserve a concise next action and resume the active host goal when available; compact context at
major milestones so repeated status history does not displace the actual task.

## Verify the outcome

Run checks appropriate to the changed behavior and complete project-required checks. Add regression
tests for meaningful behavior or risk, not assertions that merely mirror implementation or prompt wording.
Once the necessary checks pass, broaden or repeat them only for a new change, failure, or unresolved
concern. Choose independent review when the scope, risk, or user warrants it. Check reviewer claims against
the code, requirements, and runtime before changing anything. Reuse readable, still-valid evidence;
refresh evidence affected by a fix. The lightweight route has no fixed reviewer count or correction
rounds; an active Kernel retains its review and correction limits.

Use `user-story-alignment` before closing each bounded Task and stage: preserve 1-3 stable story IDs,
role/goal/value, Given/When/Then, and current observable evidence. Show a compact playback with
Complete/Partial/Missing and drift score. The coordinator checks real diffs and results independently.
Drift zero is non-blocking evidence; all required stories must be Complete for `ALIGNED`. Fix deviations
within the authorized scope autonomously. Ask only when the baseline cannot decide the issue or the user
must accept a specific deviation. Never substitute passing internal tests for an unverified user result.

## Git Delivery Default

When finishing development or modifications, first detect whether the workspace is already a Git
repository, including a worktree. In an existing repository, after required verification passes,
commit and push the task changes and open or update a ready PR by default. Carry this handoff into
bounded direct delivery too; do not stop at a local diff or ask again unless the user opts out or project
policy requires it. Inspect the branch (including detached HEAD), upstream, default base, remote, and PR.

- Stage and commit only task-owned changes; preserve unrelated work.
- If the current branch already has an open pull request, push the same branch to update that pull request.
  Do not open a duplicate pull request.
- Otherwise use a task feature branch (`codex/<task-slug>` by default); create one before committing from
  a default/protected branch or detached HEAD. Push and open a ready pull request against the intended base
  (the repository default unless the task specifies another). Never force-push by default.
- Do not hand off as ready while required verification is failing or an observable defect remains.
  Missing remote, authentication, or forge support: keep a verified local commit where possible and
  report the exact blocker. No task changes means no empty commit or PR.
- Preserve the branch/worktree for review iteration and unrelated work. Merge only with user authority.
  Outside a Git repository, deliver the files; do not initialize a repository or create a remote by default.

Report what changed, why it matters, the verification evidence, any real limitation, and the PR or artifact.
Keep updates concise and decision-relevant; process artifacts are not outcomes.

## Optional protocols

Read only the guidance needed for the current task:

- **Outcome Kernel:** For an explicit kernel/Fast Development request, a project requirement for its
  deterministic audit state, or an existing active kernel delivery, read
  [kernel-workflow.md](references/kernel-workflow.md). `scripts/opd.py` remains its sole state writer.
  Keep its contracts, journal, revision checks, and closeout gates intact. A generic request to
  "orchestrate" or a task spanning repositories does not alone require the kernel.
- **Claude provider aliases:** If assigned `claude_claude`, `claude_glm`, `claude_kimi`, or
  `claude_minimax`, read [claude-provider-routing.md](references/claude-provider-routing.md) for the
  actual transport and provider identity. Do not silently replace a locked provider/model.
- **Session diagnostics:** `scripts/analyze_rollout.py` and `scripts/session_recap.py` are read-only
  diagnostics, never proof of delivery. Base skill improvements on observed evidence and review them
  before versioning; do not automatically rewrite skills after a recap.

This default follows the [GPT-6 Astra prompting guidance](https://developers.openai.com/api/docs/guides/latest-model#prompting-best-practices):
autonomous follow-through, clear instruction authority, purposeful delegation, and proportional verification.
