# kb-agent 生产镜像
#
# 三个关键点：
#
# 1. **构建期就把 embedding 模型拉下来**
#    fastembed 默认首次用时才下载（约 100 MB）。放到运行期意味着
#    第一个用户要等 20-30 秒，而且容器重启后如果缓存没挂载就要重下。
#    在镜像层里下好，启动即用。
#
# 2. **HF_ENDPOINT 走镜像站**
#    构建机在国内时 huggingface.co 基本拉不动。
#    hf-mirror.com 是同步镜像，内容和官方一致。
#
# 3. **libgl1 / libglib2.0-0 是 PyMuPDF 的运行时依赖**
#    python:slim 里没有，缺了会报 ImportError: libGL.so.1
#    这类错误很隐蔽 —— 表现为"代码在本地好好的，容器里起不来"。

FROM python:3.12-slim

WORKDIR /app

# ---- 系统依赖 ----
# 构建机在国内：deb.debian.org 直连极慢（单请求就要十几秒，Packages 索引直接超时），
# apt 重试十几分钟后整层构建失败。换阿里云源，和下面 pip 走腾讯源、HF 走镜像站同理。
# 实测 2026-11-10：阿里云 0.1s / 清华 403 / deb.debian.org 16.6s。
# Debian 12+ 把源写在 sources.list.d/debian.sources 里，老版本是 sources.list，两处都兜一下。
RUN for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
        [ -f "$f" ] && sed -i 's|http://deb.debian.org|http://mirrors.aliyun.com|g' "$f"; \
    done; \
    apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ---- 环境变量 ----
# HF_HUB_DISABLE_XET=1：huggingface_hub 新版默认走 Xet 传输，hf-mirror 不支持，
# 实测会停在 3/5 超过 10 分钟不报错也不前进。禁掉后走普通 HTTP，速度慢但能跑完。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple \
    HF_HOME=/models \
    HF_ENDPOINT=https://hf-mirror.com \
    HF_HUB_DISABLE_XET=1 \
    # 容器里必须监听 0.0.0.0，否则宿主机访问不到
    PYTHONPATH=/app

# ---- 依赖（单独一层，改代码时不用重装）----
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- 模型预热 ----
# 放在 COPY 代码之前：代码改动不会让模型层失效
RUN python -c "\
from fastembed import TextEmbedding; \
TextEmbedding(model_name='BAAI/bge-small-zh-v1.5'); \
print('embedding model cached')"

# fastembed 默认缓存到 tempfile.gettempdir()/fastembed_cache。构建期是 root，
# 运行期是 appuser —— 缓存目录不可写，拿不到锁就会被判成“缓存无效”重新下 95MB。
# 实测：容器启动后 /tmp/fastembed_cache 里明明有 model_optimized.onnx，
# 但 touch 直接 Permission denied，于是 /health 卡在启动阶段十几分钟。
# 搬到 /models 下并把路径写死，下面那条 chown -R /models 顺便把属主交给 appuser。
RUN mv /tmp/fastembed_cache /models/fastembed_cache
ENV FASTEMBED_CACHE_PATH=/models/fastembed_cache

# ---- 代码 ----
COPY harness/ ./harness/
COPY app/ ./app/
COPY scripts/ ./scripts/

# ---- 非 root 运行 ----
# 容器里用 root 跑，一旦被拿下就是宿主机级风险
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app /models
USER appuser

EXPOSE 8000

# start-period 给足 90 秒：首次启动要加载 BGE 模型（约 2 秒）
# 加上可能的自动入库（解析 PDF + embedding）
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
