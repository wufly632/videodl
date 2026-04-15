FROM python:3.11-slim

ARG APT_DEBIAN_MIRROR=https://mirrors.aliyun.com/debian
ARG APT_SECURITY_MIRROR=https://mirrors.aliyun.com/debian-security
ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_DEFAULT_TIMEOUT=120
ARG PIP_RETRIES=10

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|http://deb.debian.org/debian|${APT_DEBIAN_MIRROR}|g; s|https://deb.debian.org/debian|${APT_DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
        sed -i "s|http://security.debian.org/debian-security|${APT_SECURITY_MIRROR}|g; s|https://security.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" /etc/apt/sources.list.d/debian.sources; \
    elif [ -f /etc/apt/sources.list ]; then \
        sed -i "s|http://deb.debian.org/debian|${APT_DEBIAN_MIRROR}|g; s|https://deb.debian.org/debian|${APT_DEBIAN_MIRROR}|g" /etc/apt/sources.list; \
        sed -i "s|http://security.debian.org/debian-security|${APT_SECURITY_MIRROR}|g; s|https://security.debian.org/debian-security|${APT_SECURITY_MIRROR}|g" /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends ffmpeg ca-certificates; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt README.md setup.py MANIFEST.in LICENSE json_repair.py /app/
COPY videodl /app/videodl

RUN python -m pip install \
        --index-url "${PIP_INDEX_URL}" \
        --default-timeout "${PIP_DEFAULT_TIMEOUT}" \
        --retries "${PIP_RETRIES}" \
        -r requirements.txt \
    && python -m pip install \
        --index-url "${PIP_INDEX_URL}" \
        --default-timeout "${PIP_DEFAULT_TIMEOUT}" \
        --retries "${PIP_RETRIES}" \
        -e .

RUN mkdir -p /data

EXPOSE 19050

CMD ["python", "-m", "videodl.api_server", "--host", "0.0.0.0", "--port", "19050"]
