# Agent compatibility experiment

This experiment checks a model behind the LiteLLM endpoint with Claude Code,
pi, Hermes Agent, and DeepSeek Harness. It separates protocol failures from
model reasoning failures and stores no API credentials. The default model is
`deepseek-v4-flash-0731`; use `--model` for model onboarding and regressions.

## Matrix

| Layer | Cases | Pass condition |
|---|---|---|
| OpenAI-compatible API | non-stream text, streaming text, automatic and forced tool frames, non-stream tool call/result continuation, multi-tool forced-choice rejection | valid response framing, stable tool-call ID, correct final value, stable 400 `forced_tool_choice_unsupported` for multi-tool `required` |
| Anthropic-compatible API | non-stream text, streaming text, automatic and forced tool frames, non-stream tool use/result, inline `system` preservation, token count | valid Anthropic framing; inline system marker must survive (US-13 us13-v1 compat layer); `count_tokens` must count the marker; multi-tool `any` must yield a stable Anthropic-format 400 |
| Real agent, first turn | inspect three files, calculate a candidate, call the verifier, consume structured feedback if rejected, write `result.json` | at least one tool call, verifier success, exact artifact |
| Real agent, resumed turn | remember the first-turn result, inspect `adjustment.json`, calculate and verify the adjusted result, write `final_summary.json` | same session resumes, at least one new tool call, exact artifact |

The inline-system case is a hard conformance requirement since the issue #9
compat layer: `/v1/messages` must merge inline `messages[].role=system` entries
into the nearest preceding user message (US-13 us13-v1), and a dropped or
indeterminate marker fails the case. Before the compat layer, LiteLLM 1.96.2
silently dropped the marker; that gap motivated the gateway-side normalizer.

Automatic and forced tool selection are separate cases. Real coding agents
normally use automatic selection; a forced-selection failure is still reported
because clients that send OpenAI `required` or Anthropic `any` will encounter it.
Single-tool forced choice is repaired by the compat layer (rewritten to name the
tool); multi-tool forced choice must return a stable 400 instead of a malformed
200, because the gateway refuses to pick a tool on the client's behalf.

## Run

From the repository root:

```bash
set -a
source ../.env
set +a
python3 execution/proto-remote-access/agent-compat/run_experiment.py
```

The runner expects `PRIVATELLM_BASE_URL` and `PRIVATELLM_API_TOKEN`. It writes
timestamped, redacted evidence under `results/`, which is ignored by git. Agent
workspaces are created below the result directory and contain fixture data only.

Each run writes `report.json` and `report.md` under a timestamped result
directory. The command exits nonzero if any selected case fails, so it can be
used as a CI or release gate. Forced tool selection is intentionally included:
a model or provider that supports ordinary automatic tool calls but rejects
OpenAI `required` or Anthropic `any` is reported as partially compatible.

## New model onboarding

Run the complete matrix for a new endpoint model:

```bash
python3 execution/proto-remote-access/agent-compat/run_experiment.py \
  --model <provider-model-id>
```

Run only the wire protocols while iterating on the gateway:

```bash
python3 execution/proto-remote-access/agent-compat/run_experiment.py \
  --model <provider-model-id> \
  --protocol-only
```

Run one or more agents without repeating protocol traffic:

```bash
python3 execution/proto-remote-access/agent-compat/run_experiment.py \
  --model <provider-model-id> \
  --agents-only \
  --agent claude \
  --agent pi
```

Valid agent selectors are `claude`, `pi`, `hermes`, and `dsh`. Model selection
can also be supplied through `PRIVATELLM_MODEL`.

Hermes is installed into a test-local uv tool directory when it is not already
available. Set `HERMES_BIN` to use an existing executable, or pass
`--skip-hermes` to run the other layers only.

DeepSeek Harness defaults to `~/project/Agents/deepseek-harness` and receives a
test-local profile patch. Override it with `--dsh-repo <path>` or
`DEEPSEEK_HARNESS_REPO`. Its headless CLI accepts one user task, so both artifact
phases run in one task while the persisted session proves the repeated
model/tool/result rounds. The Harness checkout itself is not modified.

`report.html` is the checked-in visual baseline for the
`deepseek-v4-flash-0731` validation performed on 2026-08-15. Timestamped runtime
evidence stays ignored because it can be large and environment-specific.
