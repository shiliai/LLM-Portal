# Claude Provider Routing

Use this transport only when the approved Work Item or reviewer assignment names one of these profiles.
All four profiles use model `claude-fable-5` and effort `xhigh` by default:

| Profile | `CLAUDE_DEFAULT_PROVIDER` | Model | Effort |
|---|---|---|---|
| `claude_claude` | `claude` | `claude-fable-5` | `xhigh` |
| `claude_glm` | `glm` | `claude-fable-5` | `xhigh` |
| `claude_kimi` | `kimi` | `claude-fable-5` | `xhigh` |
| `claude_minimax` | `minimax` | `claude-fable-5` | `xhigh` |

The profile names are interactive shell functions created by `shell/claude.sh`; they are not
executables and are not host-native tool names. Never issue a tool call named `claude_claude` or run
`zsh -lic 'claude_claude ...'`. A host whose native agent schema cannot select the approved Claude
provider and model must use the CLI transport below.

## Preflight

Use the exact toolchain root supplied by the user or frozen Contract. If neither names one, require
`ORCHESTRATE_CLAUDE_TOOLCHAIN`; do not select among duplicate checkouts by recency. The executable is
`$ORCHESTRATE_CLAUDE_TOOLCHAIN/bin/claude`.

Before dispatch, verify without printing credential values:

1. The toolchain root and `bin/claude` are real, readable, and executable.
2. The selected provider is one of `claude`, `glm`, `kimi`, or `minimax`.
3. Its `CLAUDE_<PROVIDER>_TOKEN` is configured in the toolchain environment.
4. `bin/claude --version` succeeds. Version output is preflight evidence, not completion evidence.

An unavailable provider is an exact blocker. Do not call bare `claude`, switch provider, change model,
or fall back to a Codex subagent without an approved Contract amendment.

## Canonical Invocation

Create the bounded Capsule or review prompt as a file inside the authorized workspace. Run from the
assigned repository/worktree and pass that file on standard input:

```bash
env CLAUDE_DEFAULT_PROVIDER=<provider> \
  "$ORCHESTRATE_CLAUDE_TOOLCHAIN/bin/claude" \
  --print \
  --output-format json \
  --model claude-fable-5 \
  --effort xhigh \
  < <prompt-file>
```

Keep the prompt on stdin. Do not append it after variadic `--add-dir`, `--allowedTools`, `--tools`, or
`--mcp-config` arguments. The default transport does not use those options: run in the assigned
worktree, place all authorized context in the Capsule, and inherit the user's normal Claude permission
configuration. Never add `--dangerously-skip-permissions` or a fallback model.

Use `--output-format json` for one bounded request. `stream-json` is reserved for a consumer that parses
JSONL incrementally and must include `--verbose`; never infer success from an empty stdout stream.

## Result Validation

Capture stdout and stderr separately. A dispatch is valid only when all of the following hold:

- process exit status is zero;
- stdout contains exactly one parseable JSON result object;
- `type=result`, `subtype=success`, and `is_error=false`;
- `session_id` is a non-empty UUID;
- `modelUsage` contains `claude-fable-5`;
- `result` is non-empty and satisfies the bounded response contract.

Record the profile, provider selector, exact model, effort, toolchain executable identity, session ID,
and result artifact identity. The selector and executable prove the requested route; the JSON result
proves the observed model and session. Never log tokens, base URLs, inherited environment dumps, or the
full toolchain `.env`.
