# wireguard sidecar 镜像（#7 容器化）：host 网络运行，wg0 仍留在宿主机网络命名空间，
# 站点路由模型不变（litellm 容器 → 宿主机默认网关 → wg0 → 10.77.0.0/24）。
# 容器起 = wg-quick up；容器停 = wg-quick down（conf 里持久化的 peer 随之恢复/摘除）。
# 运行时 peer 增删由 onboardd/consoled 经 docker.sock `docker exec … wg set` 完成。
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends wireguard-tools iproute2 \
 && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
