# NanoClaw 生产镜像
#
# 设计要点：
# 1. 非 root 运行 —— Agent 能执行 Shell 命令，用 root 跑等于把容器 root 交出去
# 2. 分层缓存 —— 先装依赖再拷代码，改代码不用重装依赖
# 3. 不内置密钥 —— .env 在 .dockerignore 里，运行时用 env_file 注入
# 4. 带健康检查 —— 便于 compose / k8s 判断就绪，而不是"进程还活着"就算好

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 系统依赖：curl 供健康检查用；其余按需精简
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# ---- 依赖层（利用缓存）----
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# ---- 代码层 ----
COPY . .

# ---- 运行用户 ----
# UID/GID 固定，挂载卷时权限才不会错乱
RUN groupadd -g 10001 nanoclaw \
 && useradd -u 10001 -g nanoclaw -s /bin/bash -m nanoclaw \
 && mkdir -p /app/workspace \
 && chown -R nanoclaw:nanoclaw /app

USER nanoclaw

# Web 渠道默认监听 127.0.0.1（本机安全）；容器内必须改 0.0.0.0 才能被外部访问。
# 通过环境变量覆盖，不改代码。
ENV NANOCLAW_WEB_HOST=0.0.0.0 \
    NANOCLAW_WEB_PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/ >/dev/null || exit 1

# CLI 渠道需要 TTY；容器里主要走 Web / 飞书 / QQ，因此用 -u 无缓冲
ENTRYPOINT ["python", "main.py"]
