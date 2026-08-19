# onboardd 镜像（#7 容器化）
# docker-cli：经 docker.sock 执行 `docker exec private-llm-wireguard wg set …`
# （wg0 在宿主机网络命名空间，由 wireguard sidecar 托管；命令前缀 = compose 注入的 WG_EXEC）。
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
COPY onboardd.py .
EXPOSE 8100
CMD ["python", "onboardd.py"]
