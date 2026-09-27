# Context benchmark collection

This directory prepares the redacted JSONL baseline for issue #127. It does
not change `compat` and it does not read LiteLLM/Postgres logs. The repository
currently has no safe production request body source: the Console usage rows
contain operational identifiers and the checked-in E2E data is synthetic.

`collect.py` accepts one JSON object per line from a file or stdin. It accepts
either a direct OpenAI/Anthropic request body, an object with `body`, or an HTTP
fixture shaped as `request: {url, headers, body}`. Headers are never copied and
only the endpoint path is retained. A fixed set of top-level request envelope
fields (`user`, `metadata`, headers, authorization, request/session/trace IDs,
and IP/host fields) is removed before replay output; nested message content and
tool schema fields with the same names remain intact after OPF text redaction.
Text leaves are sent to OPF in batches of at
most 100; a text larger than 256 KiB is rejected locally. OPF 503 is retried
twice, while 413 and 422 fail the record without writing its input or spans.
Only redacted text, replay-safe body fields, and aggregate counts/sizes are
written. OPF response `text` and `detected_spans` are never persisted.

The output format is described by [`schema.json`](schema.json). Each JSONL
record has:

- `protocol`, `metadata`: protocol and whitelisted benchmark dimensions;
- `replay.body`: sanitized request body for offline raw replay;
- `aggregate`: message/role/tool-result/content-type counts and raw versus
  redacted byte totals. Token counts are intentionally absent until a chosen
  tokenizer is recorded by the replay runner.

OpenAI `tool_calls` and `role=tool` messages are counted separately in the
aggregate (`tool_call_count`, `tool_message_count`, and argument/result sizes);
Anthropic `tool_result` blocks remain covered by `tool_result_count`.

The collector does not fabricate production data. The checked-in fixture is
synthetic and may be run without network using `--assume-input-redacted`; use
that flag only for synthetic or independently verified redacted inputs.

## Commands

Run the synthetic offline preparation:

```bash
/usr/bin/python3 tools/context-benchmark/collect.py \
  --input tools/context-benchmark/fixtures/synthetic.jsonl \
  --output /tmp/context-benchmark.redacted.jsonl \
  --manifest /tmp/context-benchmark.manifest.json \
  --assume-input-redacted
```

Run against OPF (the default URL is `http://192.168.88.75:8765`):

```bash
/usr/bin/python3 tools/context-benchmark/collect.py \
  --input samples.ndjson \
  --output /tmp/context-benchmark.redacted.jsonl \
  --manifest /tmp/context-benchmark.manifest.json \
  --opf-url http://192.168.88.75:8765
```

Use an explicit `--opf-url` in other environments. Do not put credentials,
complete prompts, hostnames/IPs, raw headers, or production log exports in this
directory. To continue over bad records, add `--continue-on-error`; the error
line contains only the JSONL record number and exception class.

## Current OPF verification

On 2026-09-26, the configured OPF endpoint responded successfully to `/health`,
`/model-info`, and `/openapi.json`. The observed service version was `0.1.0`,
with `/redact/text` and `/redact/batch` available. The smoke request used only
synthetic contact placeholders; the response was consumed in memory and
its spans were not printed or stored. The collector records only the non-secret
health/model summary in each output manifest.

## Next input needed

To establish a real baseline, provide a deliberately selected NDJSON export
whose bodies are authorized for benchmarking and contain no credentials or
internal topology. Include opaque labels such as `deployment_label` and
`task_label` in the optional metadata object; do not include request IDs, IPs,
API keys, or raw logs. The next stage can then replay the redacted samples for
`off`/`safe`/`bounded` comparisons and add tokenizer, TTFT, total latency, cache,
protocol, and quality fields.

## Offline optimization evaluation

`evaluate.py` compares four deterministic modes using only `replay.body` from a
redacted snapshot. It never writes optimized request bodies to the report.

- `raw`: the stored redacted body, serialized compactly as the baseline.
- `off`: optimization disabled; it is intentionally identical to `raw`.
- `safe`: strips ANSI control sequences and folds consecutive repeated lines in
  tool-result text. User/assistant prose, system/developer content, structured
  JSON, code, tool-call arguments, image blocks, and structural IDs are kept.
- `bounded`: applies `safe`, then keeps the head and tail of a tool result when
  its UTF-8 size exceeds the configured limit. The truncation marker includes
  the omitted byte count.

Every candidate is accepted only when its compact JSON is smaller than the raw
body. Otherwise the complete body falls back to the raw body, so an evaluation
cannot report a negative saving. The evaluator recomputes message, tool-call,
and tool-result counts from the replay body because older snapshots may have
incomplete aggregate tool fields. Each sample reports byte savings, tool-result
byte savings, estimated input tokens as `bytes / 4`, rule hits, and structure
invariants; the report contains no request or response content. The token field
is a planning estimate, not an exact tokenizer count. Tokenizer-aware replay,
TTFT, total latency, cache behavior, and response-quality checks are a later
stage.

Run the local synthetic regression fixture:

```bash
/usr/bin/python3 -m unittest \
  tools/context-benchmark/test_collect.py \
  tools/context-benchmark/test_evaluate.py
```

Evaluate existing redacted snapshots from nasubuntu after copying them to a
local temporary directory (the snapshots themselves must remain outside the
repository):

```bash
/usr/bin/python3 tools/context-benchmark/evaluate.py \
  --input /tmp/context-benchmark-eval/dsh-benchmark-20260927.redacted.jsonl \
  --input /tmp/context-benchmark-eval/dsh-macmini-20260927.tool-v2.redacted.jsonl \
  --input /tmp/context-benchmark-eval/portal-context-20260927.redacted.jsonl \
  --output /tmp/context-benchmark-eval.report.json
```

The default bounded policy is an 8 KiB tool-result limit with 4 KiB head and
tail. Tune it explicitly for a comparison, for example:

```bash
/usr/bin/python3 tools/context-benchmark/evaluate.py \
  --input snapshot.redacted.jsonl \
  --max-tool-result-bytes 16384 \
  --head-bytes 8192 --tail-bytes 4096 \
  --output /tmp/context-benchmark-eval.report.json
```
