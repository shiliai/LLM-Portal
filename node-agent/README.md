# Private node metrics agent

This bundle runs VictoriaMetrics `vmagent` beside the private LLM service. It
scrapes the service's Prometheus endpoint every 5 seconds, buffers samples on
disk while the VPS is unavailable, and sends Prometheus remote_write samples to
the Portal VictoriaMetrics instance.

Each private node runs one copy of this bundle. The current deployments:

| host | env file | `NODE_INSTANCE` | WireGuard IP | LLM `/metrics` |
|---|---|---|---|---|
| GB10 Head | deployments/gb10-head.env.example | gb10-head | 10.77.0.11 | :8891 (via [metrics-adapter](metrics-adapter/), engine :8890) |
| GB10 Worker | `deployments/gb10-worker.env.example` | `gb10-worker` | `10.77.0.15` | `:8080` |
| Dell Precision 7960 Tower | `deployments/dell-shili-7960.env.example` | `dell-shili-7960-llm` | `10.77.0.14` | `:8005` |
| M2S2VMUbuntuA6000 | `deployments/m2s2NasUbuntuVM-shili-dev.env.example` | `m2s2NasUbuntuVM-shili-dev-llm` | `10.77.0.13` | `:8006` |
| x570 workstation | `deployments/workstation.env.example` | `workstation-llm` | `10.78.0.14` | `:18001` |

The x570 workstation writes to the nasubuntu portal instance instead of the
Tokyo VPS: its `VM_REMOTE_WRITE_URL` points at `10.78.0.1:8428` (the
nasubuntu WireGuard gateway), and its `site` label matches the historical
`workstation` registration key. The portal displays that node as `x570`.

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

### nasubuntu dual-write

The GB10 worker can retain the existing Tokyo archive and send a second copy to
the nasubuntu Portal over the private LAN. On nasubuntu, set `LAN_VPS_IP` in
`vps/.env` and start the VictoriaMetrics service with
`vps/docker-compose.nasubuntu-vm-lan.yml`. On the worker, set
`PORTAL_REMOTE_WRITE_URL` and start vmagent with
`docker-compose.portal-dualwrite.yml` in addition to the base compose file.
The overlay keeps the original WireGuard target, and vmagent buffers each target
independently on its persistent queue when one endpoint is unavailable.

GB10 worker is a TensorFold rank-1 process and intentionally has no local LLM
HTTP metrics endpoint. Deploy it with `docker-compose.gb10-worker.yml` as well
as the dual-write overlay; that replaces the default scrape file with a
worker-specific DCGM-only file. The GB10 head agent owns the serving/LLM scrape,
so the Portal still gets one unified GB10 inference series without a permanent
worker `down` target.

## Metrics adapter (engine swap isolation)

 freezes the dashboard contract ( series) behind a
small translator so serving engines can be swapped without touching dashboards,
alerts or scrape configs. vmagent scrapes the adapter port, not the engine.
See [metrics-adapter/README.md](metrics-adapter/README.md) for the contract and
the engine-swap checklist.
