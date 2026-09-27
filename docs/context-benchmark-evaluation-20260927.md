# Context optimization offline evaluation

This report records the first offline comparison for issue #127. It uses three
OPF-redacted snapshots collected on nasubuntu during the current benchmark run. No request body,
response body, credential, or PII is included here.

The evaluator compares the stored redacted body (`raw`), an unchanged control
(`off`), `safe` rules (ANSI removal and consecutive duplicate-line folding in
tool results), and `bounded` rules (safe plus head/tail truncation for tool
results above 8 KiB). Candidates that are not smaller than the input fall back
to the original body.

| Dataset | Samples | Raw bytes | Safe saved | Safe total | Bounded saved | Bounded total | Bounded tool output |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| dsh-benchmark | 64 | 17,197,508 | 816 | 0.005% | 368,574 | 2.143% | 7.110% |
| dsh-macmini tool-v2 | 78 | 15,336,114 | 35,013 | 0.228% | 2,194,337 | 14.308% | 35.345% |
| portal-context | 87 | 19,218,546 | 4,892 | 0.025% | 387,907 | 2.018% | 7.019% |
| **Total** | **229** | **51,752,168** | **40,721** | **0.079%** | **2,950,818** | **5.702%** | **17.441%** |

The evaluator recomputed tool counts from `replay.body`, because older snapshots
have incomplete aggregate tool fields. The combined set contains 12,560 tool
calls and 12,560 tool results. `safe` matched 2,586 ANSI sequences and folded
196 repeated-line groups. `bounded` truncated 304 oversized tool results.

All 229 samples evaluated successfully. Message shape, tool-call structure and
IDs, structural fields, JSON/code/system content, and image placeholders passed
all invariants; there were no invariant failures. The input-size estimate is
`UTF-8 bytes / 4`, not a model tokenizer count: the combined estimate falls
from 12,937,952 to 12,200,244, a reduction of about 737,628 estimated tokens.

This offline pass does not measure TTFT, total latency, cache behavior, model
quality, tool-call success, or recovery needs. The result supports keeping
`safe` as a low-risk candidate with limited expected savings. `bounded` has
meaningful savings in the tool-heavy macmini set, but its truncation policy
requires tokenizer-aware replay and task-quality checks before any canary or
default activation.

Reproduce the report with `tools/context-benchmark/evaluate.py`; keep the
redacted snapshots outside the repository as described in the collector README.
