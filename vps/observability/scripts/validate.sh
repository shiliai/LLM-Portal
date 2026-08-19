#!/bin/sh
set -eu

OBSERVABILITY_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
RENDER="$OBSERVABILITY_DIR/scripts/render-prometheus-config.sh"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT HUP INT TERM

CONFIG_A="$TMP_DIR/prometheus.yml"
CONFIG_B="$TMP_DIR/prometheus-second.yml"
TARGETS_A='https://10.77.0.12:8890/health,http://10.77.0.11:8004/health'
TARGETS_B='http://10.77.0.11:8004/health,https://10.77.0.12:8890/health'

"$RENDER" --instance ci --targets "$TARGETS_A" --output "$CONFIG_A"
"$RENDER" --instance ci --targets "$TARGETS_B" --output "$CONFIG_B"
cmp "$CONFIG_A" "$CONFIG_B"
grep -F 'portal_instance: "ci"' "$CONFIG_A" >/dev/null
[ "$(grep -c '^          - "http' "$CONFIG_A")" -eq 2 ]
if "$RENDER" --instance ci --targets 'not-a-url' --output "$TMP_DIR/invalid.yml" >/dev/null 2>&1; then
    echo "renderer accepted an invalid target" >&2
    exit 1
fi

cp "$OBSERVABILITY_DIR/prometheus/rules.yml" "$TMP_DIR/rules.yml"
docker run --rm \
    --volume "$TMP_DIR:/etc/prometheus:ro" \
    --entrypoint=/bin/promtool \
    prom/prometheus:v2.53.0 check config /etc/prometheus/prometheus.yml
docker run --rm \
    --volume "$TMP_DIR:/etc/prometheus:ro" \
    --entrypoint=/bin/promtool \
    prom/prometheus:v2.53.0 check rules /etc/prometheus/rules.yml

DASHBOARD="$OBSERVABILITY_DIR/grafana/dashboards/portal-overview.json"
jq -e '
    (.panels | type == "array" and length > 0)
    and ([.panels[].id] | length == (unique | length))
    and ([.panels[].targets[]? | select((.expr? | type) != "string" or (.expr | length) == 0)] | length == 0)
    and ([.panels[].targets[]?.expr] | all(index("protocol") | not))
    and ([.panels[].targets[]?.expr] | any(
        contains("litellm_requests_total[1h]")
        and contains("- sum(increase(litellm_errors_total[1h])")
    ))
    and ([.panels[].targets[]?.expr] | all(contains("litellm_requests_total[5m])) + sum(rate(litellm_errors_total[5m])") | not))
    and ([.panels[].targets[]?.expr] | any(contains("container_cpu_usage_seconds_total{container!=\"\"}")))
' "$DASHBOARD" >/dev/null

{
    printf '%s\n' 'groups:' '  - name: dashboard-promql' '    rules:'
    jq -r '
        [.panels[] | .id as $panel_id | .targets[]? | select(.expr? != null)
         | {panel_id: $panel_id, expr: (.expr | gsub("\\$__rate_interval"; "5m"))}]
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
jq -e '
    .services["node-exporter"].network_mode == "host"
    and (.services["node-exporter"].command | index("--web.listen-address=127.0.0.1:9100"))
    and .services.cadvisor.network_mode == "host"
    and (.services.cadvisor.command | index("--listen_ip=127.0.0.1"))
    and (.services.cadvisor.command | index("--port=8080"))
    and .services["blackbox-exporter"].network_mode == "host"
    and (.services["blackbox-exporter"].command | index("--web.listen-address=127.0.0.1:9115"))
' "$TMP_DIR/compose.json" >/dev/null

echo "observability configuration validation passed"
