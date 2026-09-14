# Private node metrics agent

This bundle runs VictoriaMetrics `vmagent` beside the private LLM service. It
scrapes the service's Prometheus endpoint every 15 seconds, buffers samples on
disk while the VPS is unavailable, and sends Prometheus remote_write samples to
the Portal VictoriaMetrics instance.

Each private node runs one copy of this bundle. The current deployments:

| host | env file | `NODE_INSTANCE` | WireGuard IP | LLM `/metrics` |
|---|---|---|---|---|
| GB10 Head | `deployments/gb10-head.env.example` | `gb10-head` | `10.77.0.11` | `:8080` |
| GB10 Worker | `deployments/gb10-worker.env.example` | `gb10-worker` | `10.77.0.15` | `:8080` |
| Dell Precision 7960 Tower | `deployments/dell-shili-7960.env.example` | `dell-shili-7960-llm` | `10.77.0.14` | `:8005` |
| M2S2VMUbuntuA6000 | `deployments/m2s2NasUbuntuVM-shili-dev.env.example` | `m2s2NasUbuntuVM-shili-dev-llm` | `10.77.0.13` | `:8006` |

On each host, copy the matching env file to `.env` (and adjust the
remote-write URL if the site uses a different WireGuard address), then run
`docker compose up -d`. The model server must expose `/metrics` on localhost.
The two GB10 agents share the `site=gb10` label while `instance` keeps their
telemetry separate in VictoriaMetrics; the Dell and M2S2 nodes use
`<site>-llm` for both `site` and `instance`.

The compose stack also runs NVIDIA DCGM exporter on port `9400`. vmagent
scrapes both the LLM endpoint and DCGM metrics, including GPU temperature,
power usage, and utilization, and forwards them with the node's external
`site` and `instance` labels. GPU access is requested through a Compose device
reservation instead of `runtime: nvidia`, so hosts whose
nvidia-container-toolkit runs in CDI mode (no `nvidia` runtime registered in
Docker) work unchanged.

Keep labels low cardinality (`site`, `instance`, and exporter labels). Never
add request IDs, API keys, or user identifiers to metric labels.
