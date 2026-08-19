# 协议兼容层镜像（issue #9）：无状态纯转发 + 确定性变换，不落盘、不挂 sock
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY compat_proxy.py compat_metrics.py ./
EXPOSE 8400
CMD ["python", "compat_proxy.py"]
