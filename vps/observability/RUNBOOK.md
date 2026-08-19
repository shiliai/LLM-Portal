# private-llm 可观测性运行手册（issue #62）

面向 VPS / NAS 两实例的观测栈启停、查询、保留、容量与故障降级。配套决策见 `README.md`。

> 前置：本套与模型路径完全隔离（pull 模型），观测组件故障不会阻断模型请求（README D8）。
> 部署范围为各实例「本地自成一套」；跨实例横向对比可作为后续（README D1）。

## 0. 交付内容与「尚未部署」说明

本 PR 交付：观测栈 compose/配置/仪表盘/探针、compat 与 litellm 的 instrumentation、request_id 贯穿、
以及本手册。**未在任何实例部署**。issue 的「连续采集 24h 基线」「P95 额外开销 <= 5ms 实测」等为部署后验收项。

## 1. 部署（各实例逐台执行）

### 1.1 准备 .env

```bash
cd vps/observability
cp .env.example .env && vi .env
```

必填：
- `NGINX_SHARED_NETWORK`：与 `vps/.env` 同值（探测：`docker inspect nginx-sub2api | grep -i networks`）。
- `POSTGRES_PASSWORD`：复用 private-llm 的 postgres 密码；**建议**建只读账号再填（见 §5.2）。
- `GF_ADMIN_PASSWORD`：Grafana 初始管理员密码。
- `OBSERVABILITY_INSTANCE`：本机稳定名称（`vps` 或 `nas`），作为 Prometheus 的 `portal_instance` 外部标签。
- `SITE_TARGETS`：WS 站点 health 目标（逗号分隔），如 `http://10.77.0.11:8890/health,http://10.77.0.11:8004/health`。

### 1.2 启用 compat 指标（compat 镜像已含 /metrics）

compat 已内建 `/metrics`（compat_proxy.py + compat_metrics.py）。重新构建 compat 镜像后即生效：

```bash
cd vps
docker compose build compat && docker compose up -d compat
```

### 1.3 启用 litellm 回调（分段 TTFT/生成/token/错误）

- 文件已就位于 `vps/litellm/observability_callback.py`（挂载点 `/app/proxy`，可导入 `proxy.observability_callback`）。
- 编辑 `vps/litellm/config.yaml`：把 `callbacks` 里被注释的 `- proxy.observability_callback.obs_hook` 取消注释。
- 重启 litellm 容器。回调自带指标端点 `127.0.0.1:48400/metrics`（`LITELLM_OBS_PORT` 可改）。
- 验证：`curl -s 127.0.0.1:48400/metrics | head` 出现 `litellm_ttft_seconds` / `litellm_total_seconds`。

### 1.4 起观测栈

```bash
cd vps/observability
# Prometheus 不展开环境变量；此步骤读取 .env 中的两个非敏感监控值，
# 验证并排序去重后生成 prometheus/prometheus.yml。
./scripts/render-prometheus-config.sh --env-file .env
sudo docker compose --env-file .env up -d
```

验证：

```bash
# Prometheus target 健康（全 UP）
curl -s 127.0.0.1:9090/api/v1/targets | python3 -m json.tool | grep -E '"health"|"job"'
# Grafana 仪表盘
open http://127.0.0.1:3000   # admin / ${GF_ADMIN_PASSWORD}
```

### 1.5 nginx request_id 贯穿

`vps/nginx/private-llm.conf` 已：注入 `X-Request-Id $request_id`、access log 带 `rid=`。
部署时经 deploy.sh 渲染后 nginx -t 并 reload 生效。

## 2. 启停 / 状态

```bash
cd vps/observability
sudo docker compose ps            # 状态
sudo docker compose restart       # 重启观测栈（不影响 litellm/compat）
sudo docker compose stop          # 停止观测栈（模型请求照常）
sudo docker compose logs -f prometheus
```

## 3. 查询（Grafana 已预置仪表盘）

打开 `http://127.0.0.1:3000` → 左侧 Dashboard → `private-llm/Portal 全链路分段指标`。

关键查询（Explore）：

| 问什么 | PromQL |
|--------|--------|
| TTFT P95（5m） | `histogram_quantile(0.95, sum(rate(litellm_ttft_seconds_bucket[5m])) by (le))` |
| 各模型请求量 | `sum(rate(litellm_requests_total[5m])) by (model)` |
| 错误率 | `sum by (model) (rate(litellm_errors_total[5m])) / clamp_min(sum by (model) (rate(litellm_requests_total[5m])), 1e-9)` |
| 活跃请求 | `sum(litellm_active_requests) by (model)` |
| compat→LL 头耗时 P95 | `histogram_quantile(0.95, sum(rate(compat_upstream_header_seconds_bucket[5m])) by (le,proto))` |
| 站点可达 | `probe_success{job="blackbox-sites"}` |
| 站点 HTTP RTT | `probe_http_duration_seconds{job="blackbox-sites"}` |
| PG 连接数 | `sum(pg_stat_database_numbackends{datname="litellm"})` |
| 容器 OOM/重启 | `increase(container_oom_events_total[1h])` / `changes(container_start_time_seconds{container!=""}[1h])` |

时间窗：Grafana 右上角可选 最近 5 分钟 / 1 小时 / 24 小时对比。

### 3.1 基线记录（issue 验收项，部署后做）

连续采集 ≥24h 后，导出每模型 P50/P95/P99 与错误率，形成一份基线 markdown（建议存 `vps/observability/baseline/`），
并列出下一阶段最值得优化的 1-3 个瓶颈及其证据。本 PR 不产出该基线（需运行时数据）。

## 4. 数据保留与容量

- 保留：`--storage.tsdb.retention.time=PROM_RETENTION`（默认 14d）、`--storage.tsdb.retention.size=PROM_RETENTION_SIZE`（默认 10GB）。达到容量上限即滚动淘汰。
- 容量估算：本观测栈标签低基数，单实例 24h ≈ sub-GB 量级；10GB 足够数周。
- Grafana 元数据在 grafana-data volume，无大占用。
- 观察：`PrometheusDiskSpace` 告警在占用 >8GiB 触发（rules.yml），提示扩容或缩短 PROM_RETENTION。

## 5. 安全与账号

### 5.1 暴露面

- 全部端点仅回环/内网可达（host 网络的 Prometheus、Grafana、node-exporter、cAdvisor 和 blackbox 均显式绑定 `127.0.0.1`；postgres-exporter 以 loopback 发布）。**公网不可达**。
- 审计：确认 `ss -ltnp` 不显示对 9090/3000/8080/9100/9115/48400/9187 的公网监听。

### 5.2 Postgres 只读账号（建议）

为 postgres-exporter 建只读角色，避免用 litellm 账号：

```sql
-- 在 private-llm-postgres 执行（docker exec private-llm-postgres psql -U litellm -d litellm）
CREATE ROLE prometheus_ro LOGIN PASSWORD '强密码';
GRANT CONNECT ON DATABASE litellm TO prometheus_ro;
GRANT pg_read_all_stats TO prometheus_ro;
```

然后把 `PG_EXPORTER_USER=prometheus_ro`、`POSTGRES_PASSWORD` 换成该角色密码。

### 5.3 指标不落敏感数据

prometheus.yml 的 relabel 已只保留低基数指标（pg 连接数等）；compat/litellm 回调只在结构化日志带 request_id，
**绝不把 key/正文/IP/session 写入指标**。若需复查：`curl -s 127.0.0.1:9090/api/v1/label/__name__/values` 核对无异常高基数名。

## 6. 故障降级

- **监控侧故障不影响模型请求**（pull 模型，观测不在请求路径）：Prometheus/grafana/exporter 停机、磁盘满、回调异常，litellm/compat 照常服务。
- litellm 回调全程 try/except（`observability_callback.py` 的 `_safe` 装饰器）：指标打点失败只记日志，绝不断流。
- **停用观测栈**：compat 取消挂载即可；若要连 compat /metrics 也关，回滚 `compat_proxy.py` 中 /metrics Route（保留 request_id 透传不依赖 /metrics）。

## 7. 验收对照（issue #62）

| 验收项 | 状态 | 说明 |
|--------|------|------|
| request_id 对齐各层 + 阶段耗时与口径 | 已交付 | nginx/compat/litellm 贯穿；D3 推导口径文档化 |
| VPS/NAS 按模型/站点查请求量、活跃、错误率、TTFT/总耗时 P50/P95/P99 | 已交付配置/仪表盘 | Grafana 预置；运行时数据待部署后 |
| 区分客户端取消/入口超时/上游连接失败/首 Token 慢/生成慢 | 已交付 | compat status_class + litellm 错误分类 + TTFT/生成分段 |
| WireGuard 站点可达/RTT/连接错误趋势 | 已交付 | blackbox 60s 探针 + onboardd 站点状态页 |
| 标签低基数 + 公网不可达 | 已交付 | D7 / §5 |
| 采集开销 P95<=5ms 且不缓冲 | 代码满足，运行时验证待部署 | 固定小桶直方图 + SSE 逐行不改 |
| 连续采集 24h + 基线记录 | 待部署后 | §3.1 |
| 部署/运维文档 + 监控故障不阻断 | 已交付 | 本手册 + README D8 |
