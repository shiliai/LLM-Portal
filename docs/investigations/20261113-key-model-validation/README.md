# Key model scope validation — 2026-11-13

Baseline v1: user's Tokyo portal error report and follow-up about the missing
`mao` plaintext key. Delivery: reviewed code candidate; production remains unchanged.

- KEY-MODEL-1: As an administrator, save a valid model scope so the key can use
  the intended model. Given `DeepSeek-V4.1-Flash-1017`, saving and reopening the
  edit dialog retains that scope. Explicit `[]` still restores all models;
  invalid shapes and non-string elements must not reach LiteLLM.
- KEY-VAULT-1: Explain why the existing key cannot be revealed without replacing
  a credential used by existing clients. Read-only Tokyo inspection found `mao`
  created 2026-08-24 with no matching vault row (6 total vault rows, 28 key rows).
  No plaintext or token hash was retained in this report. Existing key and
  model scope were not changed. Original plaintext can be imported via the
  existing verified-import endpoint; otherwise a replacement needs user action.

Root cause: `/console/api/keys/update` used the site-name `NAME_RE` (32 characters,
no dots) rather than the existing model-name `MODEL_RE` (64 characters, dots).
It also stringified non-string items and treated invalid falsey shapes as `[]`.

Validation on the local candidate:

- Before fix: new API regression cases produced 9 failures and 8 passes,
  including the exact dotted model-name rejection.
- After fix: 18 targeted cases including historical key import passed.
- `python -m pytest console/test_console.py`: 114 passed. Existing Starlette
  TestClient deprecation warning; no failure.
- Existing `keys-e2e.js`, real installed Chrome, real console at loopback port
  18399, synthetic LiteLLM at 18410: saved exact reported model name, reopened
  the dialog and checked the returned scope. Empty-scope and malformed-input
  boundaries verified by API tests. Browser workflow exited 0. Existing script
  printed background 502/404 console resource warnings; these are not a claim
  that all external/background services were exercised successfully.
- `git diff --check`: passed.

Playback: KEY-MODEL-1 Complete for the local browser/console + mocked LiteLLM
boundary; Tokyo deployment NOT_VERIFIED. KEY-VAULT-1 Complete as read-only
diagnosis. Drift 0 for code-review scope; no production completion claim.

Production source is packaged without `.git` at
`/home/chriswang/docker/LLM-Portal`; running console image during inspection:
`sha256:eddb00e0454a2bccc2a8d6801943e8be889baee7b7e15cfdb42c61fde064259b`.
Release requires merge authority, CI, integrated full tests, recovery capture,
and target acceptance per `docs/runbook.md` §2.1. Do not deploy this unmerged
branch or mutate the user's `mao` key as a probe.
