# litellm 可观测性回调

LiteLLM 分段指标（TTFT/生成/token/错误 + request_id 对账）由自定义回调提供，**部署时的实体文件**在：

```text
execution/proto-remote-access/vps/litellm/observability_callback.py
```

原因：private-llm 的 litellm 容器把 `vps/litellm` 挂到 `/app/proxy`，回调必须在该卷内才能以
`proxy.observability_callback.obs_hook` 导入（与 `proxy.group_routing.group_routing_hook` 同机制）。

挂载（部署时，见 RUNBOOK.md §启用回调）：
1. 确认文件在 `vps/litellm/observability_callback.py`（本仓库已到位）。
2. `vps/litellm/config.yaml` 的 `litellm_settings.callbacks` 追加 `proxy.observability_callback.obs_hook`。
3. 回调自带独立 Prometheus 端点（LITELLM_OBS_PORT，默认 127.0.0.1:48400），被本观测栈 prometheus 拉取。

说明：`prometheus_client` 由 LiteLLM main-stable 镜像自带，无需额外 pip。
