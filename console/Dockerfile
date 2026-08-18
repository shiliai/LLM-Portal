# consoled 镜像（#7 容器化）
# docker-cli：容器化部署下经 docker.sock 执行 `docker exec private-llm-wireguard wg …`
# 与 `docker restart private-llm-mcp-hub`（宿主机 wg0 / mcp-hub 生命周期管理），
# 命令前缀由 compose 注入 WG_EXEC / MCP_RESTART_CMD，默认值保留宿主机直跑语义。
FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && chmod a+r /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
    > /etc/apt/sources.list.d/docker.list \
 && apt-get update && apt-get install -y --no-install-recommends docker-cli \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY console.py .
COPY static/ static/
EXPOSE 8300
CMD ["python", "console.py"]
