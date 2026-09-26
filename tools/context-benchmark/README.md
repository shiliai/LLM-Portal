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
