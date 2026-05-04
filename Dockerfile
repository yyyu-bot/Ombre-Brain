# ============================================================
# Ombre Brain Docker Build (2.0.3)
# Docker 构建文件
#
# Build:                       docker build -t ombre-brain .
# Build (skip model preload):  docker build --build-arg PRELOAD_MODEL=false -t ombre-brain:slim .
# Run:                         docker run -e OMBRE_DASHBOARD_PASSWORD=xxx -p 8000:8000 ombre-brain
# ============================================================

FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (leverage Docker cache)
# 先装依赖（利用 Docker 缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files / 复制项目文件
COPY src/ ./src/
COPY frontend/ ./frontend/
COPY VERSION ./VERSION
COPY config.example.yaml ./config.yaml

# --- Optional: preload local embedding model into the image ---
# --- 可选：构建时把本地 embedding 模型预拉进镜像 ---
#
# 默认 PRELOAD_MODEL=true → 构建产物 ~3GB（含 bge-m3 ONNX 权重，开箱即用，无需首次启动等待下载）。
# 如果要更小的镜像（仅 ~600MB），用：
#   docker build --build-arg PRELOAD_MODEL=false -t ombre-brain:slim .
# 然后首次启动时 server 会在后台从 huggingface.co（失败切 hf-mirror.com）下载，
# Dashboard 能看到实时进度（详见 frontend/dashboard.html 设置页 → 向量化）。
ARG PRELOAD_MODEL=true
RUN if [ "$PRELOAD_MODEL" = "true" ]; then \
        echo "[docker] preloading bge-m3 model into /app/models/bge-m3 ..." ; \
        mkdir -p /app/models /app/buckets/.logs ; \
        cd /app && python -c "from src.model_downloader import download_bge_m3, status_path_for; \
import sys; \
ok = download_bge_m3('/app/models/bge-m3', status_path_for('/app/buckets')); \
sys.exit(0 if ok else 1)" ; \
    else \
        echo "[docker] PRELOAD_MODEL=false, skipping model download (will download at first launch)" ; \
    fi

# Persistent mount point: bucket data
# 持久化挂载点：记忆数据
VOLUME ["/app/buckets"]

# Default to streamable-http for container (remote access)
# 容器场景默认用 streamable-http
ENV OMBRE_TRANSPORT=streamable-http
ENV OMBRE_BUCKETS_DIR=/app/buckets
# 默认走本地 embedding（fastembed + bge-m3，无需 API key）
ENV OMBRE_EMBED_BACKEND=local

EXPOSE 8000

CMD ["python", "src/server.py"]
