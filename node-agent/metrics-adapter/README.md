# Node metrics adapter (engine → dashboard contract)

Serving engines change faster than dashboards. This adapter freezes the
monitoring contract: every engine serves raw Prometheus metrics on a private
port, and `adapter.py` translates them into the stable `vllm:*` names the
console's 节点性能 page queries. vmagent points at the adapter port (`LLM_METRICS_PORT`)
permanently, so swapping engines never touches dashboards, alerts or scrape config.

```
engine :8890/metrics (raw, engine-specific)
        │
        ▼
adapter.py :8891/metrics  ← scrape target (LLM_METRICS_PORT=8891)
        │   maps engine names → vllm:* contract
        │   derives spec/kv series from the engine's request log
        ▼
vmagent → remote_write → Portal VictoriaMetrics → 节点性能 page
```

Deployed on `gb10-head` since 2026-09-30 (TensorFold cutover); the engine was
vLLM (B12X) before and only `MAP` changed.

## Install (per node, systemd user service)

```bash
mkdir -p ~/llm-metrics-adapter && cp adapter.py ~/llm-metrics-adapter/
mkdir -p ~/.config/systemd/user && cp llm-metrics-adapter.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now llm-metrics-adapter
# then in node-agent/: set LLM_METRICS_PORT=8891 in the node's env file
```

## Contract

Direct mappings (`MAP` in `adapter.py`, engine `tensorfold_*` → contract `vllm:*`):
`prompt_tokens_total`, `generation_tokens_total`, `request_success_total`,
`spec_decode_num_decode_steps_total`, `prefix_cache_hits_total` ← engine cached
tokens, `prefix_cache_queries_total` ← engine prompt tokens. Activity and
waiting gauges are sampled every second and exported as window maxima; the
TensorFold `requests_stalled` gauge is the only waiting signal in 0.3.x and is
used as a best-effort queue indicator. If a future engine exposes a real
queued/pending gauge, add its name to `WAITING_NAMES`.

Derived series (engine lacks native counters; sourced from the engine request
log, one JSON line per request):

| contract series | derivation | semantics |
|---|---|---|
| `vllm:spec_decode_num_accepted_tokens_total` | Σ request `decode_tokens` | acceptance **lower bound** (see below) |
| `vllm:spec_decode_num_draft_tokens_total` | Σ request `rounds × K_MAX` (7) | drafted at max draft width |
| `vllm:kv_cache_usage_perc` | `1 − kv_free/4097` from the newest log line | pool occupancy **at the last request's moment** (4097 = this deployment's 1M-token page count; adjust with `KV_POOL_TOKENS`) |

The MTP/TAR acceptance panel therefore shows a conservative, monotonic value:
`drafted` assumes every round verifies the maximum draft width, so the true
acceptance rate is always ≥ what the panel reports. If a future engine exposes
native accepted/drafted counters, add them to `MAP` and delete the log tail.

TensorFold profile compatibility is handled at the adapter boundary. Both the
legacy underscore names (`tensorfold_requests_running`) and the Prometheus
namespace names (`tensorfold:requests_running`) are normalized before mapping;
the same applies to the `tensorfold_health:*` counters. A profile switch does
not require a vmagent or dashboard change, and overlapping health counters are
treated as fallbacks so they are not counted twice.

## Engine swap checklist

1. New engine serves `:8890` (keep the historical port; LiteLLM deployments and
   clients key on it).
2. Update `MAP` (and the request-log tail field names, if the log schema moved)
   in `adapter.py`; `systemctl --user restart llm-metrics-adapter`.
3. `curl 127.0.0.1:8891/metrics | grep '^vllm:'` must list every contract series.
4. Dashboards and vmagent configs stay untouched.

## gb10-head deployment notes

- The vmagent must be recreated with **both** compose files
  (`-f docker-compose.yml -f docker-compose.portal-dualwrite.yml`); a plain
  `docker compose up -d vmagent` silently drops the portal dual-write and the
  节点性能 page goes dark while the VPS aggregator keeps filling.
- No traffic means `0.0` on the TPS cards (`irate` over an idle counter), which
  is the console's honest-idle semantics; missing series render as `—`.
