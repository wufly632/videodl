FROM python:3.11-slim

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_DEFAULT_TIMEOUT=120
ARG PIP_RETRIES=10

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

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
