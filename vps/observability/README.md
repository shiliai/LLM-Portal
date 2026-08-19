# private-llm 全链路分段可观测性（issue #62）

为两套生产 Portal（VPS / NAS）建立**低开销、可关联、可按实例/模型/站点分析**的全链路分段指标监控基线，为后续网络、路由、连接池、模型与网关优化提供可比较证据。

> 状态：**开发/交付物已完成，尚未部署**（本 PR 仅提交配置、仪表盘、探针与文档；启停/采集见 `RUNBOOK.md`）。issue 中「连续采集至少 24 小时形成基线记录」「验证 P95 额外开销 <= 5ms」「确认两实例基线」等**运行时验收项**需部署后再执行，均不在本 PR 范围内。

## 架构决策

### D1. 指标存储与展示：每实例自建一套 Prometheus + Grafana（各自本地保存）

- VPS、NAS 各自运行一套轻量 Prometheus + Grafana，指标**各自本地保存**，互不依赖对方网络/实例存活。
- 理由：两实例跨地域、走不同上行/隧道，集中汇聚会把实例间联路变成采集依赖点；且与「监控组件故障不得阻断模型请求」的边界一致（采集只在实例内 pull）。
- 跨实例汇聚/横向对比：Grafana/Prometheus 原生支持一图多数据源，未来可按需加一处「只读汇聚实例」做 federation 或留存基线快照导出，作为后续（本 issue 不计入）。

### D2. 链路与分段口径（关键概念）

```text
client ──► nginx ──► compat ──► LiteLLM ──► deployment/site(vLLM)
           (edge)    (parse/transform + 等 LiteLLM 响应头)   (鉴权/路由)  (连接/首Token/生成)
```

| 阶段 | 位置 | 指标 | 说明 |
|------|------|------|------|
| edge 收到→转发上游 | nginx access log `$request_id` | nginx 日志（非时序） | 用 `X-Request-Id` 贯穿，供日志侧关联 |
| compat 解析/变换 | compat `/metrics` | `compat_parse_seconds` | 请求读取 + 规则解析/变换耗时 |
| compat 等 LiteLLM 响应头 | compat `/metrics` | `compat_upstream_header_seconds` | 请求发到 LiteLLM 到拿到响应头（含 LiteLLM 鉴权/路由/连接） |
| LiteLLM 网关开销 | 派生口径 | `litellm_total_seconds - litellm_ttft_seconds - compat_upstream_header` | 该段无法直接观测，用**可回归验证的推导口径**（见 D3） |
| 网关→站点 首 Token | litellm 回调 | `litellm_ttft_seconds`（**首 Token**，非首字节；首字节不可观） | 回调在首 chunk 打点 |
| 模型生成阶段 | litellm 回调 | `litellm_generation_seconds` = total − TTFT | |
| 请求总耗时 | litellm 回调 / LiteLLM `/metrics` | `litellm_total_seconds` | 含入流式首 Token 到结束 |

### D3. LiteLLM 阶段推导口径（避免伪精度）

LiteLLM 1.96.2 的 `/metrics` 原生给出**每模型**请求数/延迟/token 直方图，但不含首 Token 时间，也不能给出「网关侧开销 vs 上游」拆分。本方案用**自定义回调**（`litellm/observability_callback.py`，与 `group_routing_hook` 同机制挂载）在 LiteLLM 进程内：

- `async_log_stream_event` **首个**事件 → 打点 **TTFT**（start_time → 首 chunk）；
- `async_log_success_event` → 打点 **total / generation / token**；
- `async_log_failure_event` → 打点**错误**（按错误类型 + 状态类别）。

推导口径（文档化、可回归验证）：
- `generation = total − ttft`（流式与全响应一致口径）
- `litellm_gateway_overhead`：LiteLLM 网关开销无法单独观测，边界为 `[0, total−ttft]`；**报告时须与 compat_upstream_header 对比归因**（该段同时覆盖 LiteLLM 网关 + 上游连接），不单独宣称伪精度。

### D4. request_id 贯穿（edge → compat → LiteLLM → deployment/site）

- **nginx**：用内建 `$request_id`（32 hex）生成，注入 `X-Request-Id` 头传给上游，并写进 access log（`rid=$request_id`）。
- **compat**：优先透传入口 `X-Request-Id`（来自 nginx），否则自生成；原样转发给 LiteLLM（保持 `X-Request-Id`），并回写该头给客户端（`x-request-id` 响应头）。
- **LiteLLM 回调**：把 `X-Request-Id` 写入日志行与结构化事件，供按 request_id 对账各层。
- **不把 request_id 作为 Prometheus 标签**（高基数）；它只存在于：nginx access log、compat 结构化日志（stdout）、LiteLLM 日志、回调结构化日志。

客户端拿到的响应头：`x-request-id`（不含内部敏感信息）。

### D5. 站点链路观测：WireGuard 站点可达性 / RTT（低频，不扰动模型服务）

- blackbox-exporter（VPS/NAS 本机，host 网络可直达 10.77.0.0/24 WG 子网）对站点 **health 端点**做 **每 60s** HTTP probe（不在模型推理路径上打高频探针）。
- 采集：可达性（`probe_success`）、HTTP 阶段耗时（`probe_duration_seconds`、`probe_http_duration_seconds`）、错误（`probe_success`、即时错误）。
- WireGuard 握手次数/最近握手/连接错误取自 wg 命令 + `onboardd`（站点状态页），本套监控以 blackbox 的 HTTP 视角补充「HTTP RTT / 首字节」。

### D6. 实例系统与存储指标

- **node-exporter**（host）：CPU、RSS、磁盘、网络。
- **cadvisor**（host）：各容器 CPU/RSS/重启计数/**OOM 计数**（`container_oom_events_total`；重启使 `container_start_time_seconds` 归零）。
- **postgres-exporter**（shared 网络，只读）：连接数（`pg_stat_database_numbackends`）。
- **prometheus/grafana 自身**。

### D7. 安全 / 低基数约束

- 所有观测端点仅 **回环/内网** 可达（compose 只在 host 网络或 loopback 发布；grafana 绑 127.0.0.1）；**公网不可达**。
- **不记录**：API Key、Authorization、prompt/response 正文、完整 IP、session ID、request_id、模型生成文本。
- 标签**低基数**：protocol / stream / model group / deployment / site / status 类别。严禁把 request_id、调用方、时间戳大尾内容作标签。

### D8. 性能与故障降级

- **开销**：pull 模型 + 固定桶直方图 + 计数/量规，每请求 < 1μs 量级观测代码（compat 内 2 个直方图打点 + 1 原子计数）。**不需缓冲响应**（SSE 仍逐行转发）。
- **降级**：monitoring 栈与模型路径**完全独立**：compat/LiteLLM 的观测代码用 try/except 兜底、绝不因观测失败而中断请求；exporter 崩溃、Prometheus 宕机、磁盘满都不影响 litellm/compat 正常服务（pull 模型，监控侧故障不出现在请求路径）。

## 组件

| 组件 | 镜像 | 端口(回环) | 采集对象 |
|------|------|-----------|----------|
| prometheus | prom/prometheus:v2.53.0 | 9090 | 全部 |
| grafana | grafana/grafana:11.0.0 | 3000 | Prometheus 数据源（预置仪表盘） |
| node-exporter | prom/node-exporter:v1.8.0 | 9100 | 实例 OS |
| cadvisor | gcr.io/cadvisor/cadvisor:v0.49.0 | 8080 | 容器 CPU/RSS/重启/OOM |
| postgres-exporter | prometheuscommunity/postgres-exporter:v0.15.0 | 9187 | private-llm Postgres 连接数 |
| blackbox-exporter | prom/blackbox-exporter:v0.25.0 | 9115 | WS 站点 health（60s） |
| litellm 回调 | 无（挂进 litellm 容器） | 48400 | TTFT/生成/错误/token（自定义） |

## 目录

```text
vps/observability/
├── README.md                # 本文档：架构决策 + 口径 + 约束
├── RUNBOOK.md               # 部署/启停/查询/数据保留/容量/故障降级
├── docker-compose.yml       # prometheus + grafana + exporters + blackbox
├── .env.example             # 观测栈环境变量
├── prometheus/
│   ├── prometheus.yml       # 低基数 scrape 配置（含 blackbox/down 自检）
│   └── rules.yml            # 记录规则 + 告警规则（error/TTFT/站点可达等）
├── grafana/
│   ├── provisioning/datasources/datasource.yml
│   ├── provisioning/dashboards/dashboard.yml
│   └── dashboards/portal-overview.json   # 分段链路总览仪表盘
├── litellm/
│   └── README.md  # 指向 vps/litellm/observability_callback.py（LiteLLM 回调实体）
└── blackbox/
    └── blackbox.yml         # http_2xx 探针模块
```

另有对既有部署文件的增改（request_id 贯穿）：
- `vps/nginx/private-llm.conf`：注入 `X-Request-Id` + access log 带 rid。
- `compat/compat_proxy.py`：新增 `/metrics`、request_id 贯穿、compat 分段耗时。

## 快速启用（部署时执行，见 RUNBOOK.md）

```bash
cd vps/observability
cp .env.example .env && vi .env        # 填 NGINX_SHARED_NETWORK / POSTGRES_PASSWORD / GF_ADMIN_PASSWORD / 站点目标
sudo docker compose up -d
```
