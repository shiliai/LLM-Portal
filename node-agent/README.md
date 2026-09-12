# Private node metrics agent

This bundle runs VictoriaMetrics `vmagent` beside the private LLM service. It
scrapes the service's Prometheus endpoint every 15 seconds, buffers samples on
disk while the VPS is unavailable, and sends Prometheus remote_write samples to
the Portal VictoriaMetrics instance.

Copy `.env.example` to `.env`, set the site name, instance, local metrics port,
and WireGuard reachable `VM_REMOTE_WRITE_URL`, then run `docker compose up -d`.
The model server must expose `/metrics` on localhost.

Keep labels low cardinality (`site`, `instance`, and exporter labels). Never
add request IDs, API keys, or user identifiers to metric labels.
