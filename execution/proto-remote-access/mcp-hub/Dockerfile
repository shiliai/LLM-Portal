# mcp-hub 镜像（#7 容器化）：无宿主机操作，纯 HTTP + 文件状态
FROM python:3.14-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mcp_hub.py .
COPY homepage.html .
EXPOSE 8200
CMD ["python", "mcp_hub.py"]
