#!/bin/sh
set -eu

OBSERVABILITY_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
RENDER="$OBSERVABILITY_DIR/scripts/render-prometheus-config.sh"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT HUP INT TERM
chmod 0755 "$TMP_DIR"

CONFIG_A="$TMP_DIR/prometheus.yml"
CONFIG_B="$TMP_DIR/prometheus-second.yml"
TARGETS_A='site-b=https://10.77.0.12:8890/health,site-a=http://10.77.0.11:8004/health'
TARGETS_B='site-a=http://10.77.0.11:8004/health,site-b=https://10.77.0.12:8890/health'

"$RENDER" --instance ci --targets "$TARGETS_A" --output "$CONFIG_A"
"$RENDER" --instance ci --targets "$TARGETS_B" --output "$CONFIG_B"
cmp "$CONFIG_A" "$CONFIG_B"
grep -F 'portal_instance: "ci"' "$CONFIG_A" >/dev/null
[ "$(grep -c '^      - targets: \["http' "$CONFIG_A")" -eq 2 ]
grep -F 'site: "site-a"' "$CONFIG_A" >/dev/null
grep -F 'source_labels: [site]' "$CONFIG_A" >/dev/null
if "$RENDER" --instance ci --targets 'site-a=not-a-url' --output "$TMP_DIR/invalid.yml" >/dev/null 2>&1; then
    echo "renderer accepted an invalid target" >&2
    exit 1
fi
if "$RENDER" --instance ci --targets 'site-a=http://user:pass@host/health' --output "$TMP_DIR/userinfo.yml" >/dev/null 2>&1; then
    echo "renderer accepted URL userinfo" >&2
    exit 1
fi
if "$RENDER" --instance ci --targets 'site-a=http://a/health,site-a=http://b/health' --output "$TMP_DIR/duplicate.yml" >/dev/null 2>&1; then
    echo "renderer accepted duplicate site labels" >&2
    exit 1
fi

cp "$OBSERVABILITY_DIR/prometheus/rules.yml" "$TMP_DIR/rules.yml"
cp "$OBSERVABILITY_DIR/prometheus/rules.test.yml" "$TMP_DIR/rules.test.yml"
chmod 0644 "$TMP_DIR"/*.yml
docker run --rm \
    --volume "$TMP_DIR:/etc/prometheus:ro" \
    --entrypoint=/bin/sh \
    prom/prometheus:v2.53.0 -c \
    'test -r /etc/prometheus/prometheus.yml && test -r /etc/prometheus/rules.yml'
docker run --rm \
    --volume "$TMP_DIR:/etc/prometheus:ro" \
    --entrypoint=/bin/promtool \
    prom/prometheus:v2.53.0 check config /etc/prometheus/prometheus.yml
docker run --rm \
    --volume "$TMP_DIR:/etc/prometheus:ro" \
    --entrypoint=/bin/promtool \
    prom/prometheus:v2.53.0 check rules /etc/prometheus/rules.yml
docker run --rm \
    --volume "$TMP_DIR:/etc/prometheus:ro" \
    --workdir=/etc/prometheus \
    --entrypoint=/bin/promtool \
    prom/prometheus:v2.53.0 test rules /etc/prometheus/rules.test.yml

DASHBOARD="$OBSERVABILITY_DIR/grafana/dashboards/portal-overview.json"
jq -e '
    (.panels | type == "array" and length > 0)
    and ([.panels[].id] | length == (unique | length))
    and ([.panels[].targets[]? | select((.expr? | type) != "string" or (.expr | length) == 0)] | length == 0)
    and ([.panels[].targets[]?.expr] | all(index("protocol") | not))
    and ([.templating.list[].name] | sort == ["model", "site"])
    and ([.panels[].targets[]?.expr] | any(contains("compat_requests_total{status_class=\"2xx\"}")))
    and ([.panels[].targets[]?.expr] | any(. == "ll:error_ratio_by_status"))
    and ([.panels[].targets[]?.expr] | any(startswith("ll:ttft_p50")))
    and ([.panels[].targets[]?.expr] | any(startswith("ll:ttft_p95")))
    and ([.panels[].targets[]?.expr] | any(startswith("ll:ttft_p99")))
    and ([.panels[].targets[]?.expr] | any(startswith("ll:total_p50")))
    and ([.panels[].targets[]?.expr] | any(startswith("ll:total_p95")))
    and ([.panels[].targets[]?.expr] | any(startswith("ll:total_p99")))
    and ([.panels[].targets[]?.expr] | any(contains("container_cpu_usage_seconds_total{container!=\"\"}")))
' "$DASHBOARD" >/dev/null

{
    printf '%s\n' 'groups:' '  - name: dashboard-promql' '    rules:'
    jq -r '
        [.panels[] | .id as $panel_id | .targets[]? | select(.expr? != null)
         | {panel_id: $panel_id, expr: (.expr
             | gsub("\\$__rate_interval"; "5m")
             | gsub("\\$model"; ".*")
             | gsub("\\$site"; ".*"))}]
        | to_entries[]
        | "      - record: dashboard_validation_\(.key)\n        expr: \(.value.expr | @json)"
    ' "$DASHBOARD"
} > "$TMP_DIR/dashboard-rules.yml"
docker run --rm \
    --volume "$TMP_DIR:/etc/prometheus:ro" \
    --entrypoint=/bin/promtool \
    prom/prometheus:v2.53.0 check rules /etc/prometheus/dashboard-rules.yml

docker compose --env-file "$OBSERVABILITY_DIR/.env.example" \
    -f "$OBSERVABILITY_DIR/docker-compose.yml" config --format json > "$TMP_DIR/compose.json"
docker compose --env-file "$OBSERVABILITY_DIR/../.env.example" \
    -f "$OBSERVABILITY_DIR/../docker-compose.yml" config --format json > "$TMP_DIR/core-compose.json"
jq -e '
    .services["node-exporter"].network_mode == "host"
    and (.services["node-exporter"].command | index("--web.listen-address=127.0.0.1:9100"))
    and .services.cadvisor.network_mode == "host"
    and (.services.cadvisor.command | index("--listen_ip=127.0.0.1"))
    and (.services.cadvisor.command | index("--port=8080"))
    and .services["blackbox-exporter"].network_mode == "host"
    and (.services["blackbox-exporter"].command | index("--web.listen-address=127.0.0.1:9115"))
    and (.services["postgres-exporter"].networks | has("backend"))
' "$TMP_DIR/compose.json" >/dev/null
jq -e -s '
    .[0].networks.backend.name == .[1].networks.default.name
' "$TMP_DIR/compose.json" "$TMP_DIR/core-compose.json" >/dev/null

grep -F -- '- proxy.observability_callback.obs_hook' "$OBSERVABILITY_DIR/../litellm/config.yaml" >/dev/null
grep -F -- "--exclude 'vps/observability/.env'" "$OBSERVABILITY_DIR/../../docs/runbook.md" >/dev/null
grep -F -- "--exclude 'vps/observability/prometheus/prometheus.yml'" "$OBSERVABILITY_DIR/../../docs/runbook.md" >/dev/null

echo "observability configuration validation passed"
