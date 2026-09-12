# Private node metrics agent

This bundle runs VictoriaMetrics `vmagent` beside the private LLM service. It
scrapes the service's Prometheus endpoint every 15 seconds, buffers samples on
disk while the VPS is unavailable, and sends Prometheus remote_write samples to
the Portal VictoriaMetrics instance.

Each private node runs one copy of this bundle. The current GB10 deployment has
two DGX hosts, so install the agent once on each host with a distinct instance
label:

| host | env file | `NODE_INSTANCE` | WireGuard IP |
|---|---|---|---|
| DGX A | `deployments/gb10-dgx-a.env.example` | `gb10-dgx-a` | `10.77.0.11` |
| DGX B | `deployments/gb10-dgx-b.env.example` | `gb10-dgx-b` | `10.77.0.14` |

On each DGX host, copy the matching env file to `.env` (and adjust the
remote-write URL if the site uses a different WireGuard address), then run
`docker compose up -d`. The model server must expose `/metrics` on localhost.
The two agents share the `site=gb10` label while `instance` keeps their
telemetry separate in VictoriaMetrics.

Keep labels low cardinality (`site`, `instance`, and exporter labels). Never
add request IDs, API keys, or user identifiers to metric labels.
