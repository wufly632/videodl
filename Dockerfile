FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt README.md setup.py MANIFEST.in LICENSE json_repair.py /app/
COPY videodl /app/videodl

RUN pip install -U pip setuptools wheel \
    && pip install -r requirements.txt \
    && pip install -e .

RUN mkdir -p /data

EXPOSE 19050

CMD ["python", "-m", "videodl.api_server", "--host", "0.0.0.0", "--port", "19050"]
