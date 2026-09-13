FROM python:3.12-slim

# curl for the healthcheck; build-essential is needed by some wheels on ARM
# (Oracle Ampere / Graviton), which is where the free demo VMs live.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# HF_HOME deliberately sits outside /app/data: that path is a mounted volume at
# runtime, which would shadow the model baked in below and send the first
# request off to re-download it.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/opt/hf-cache

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the embedding model into the image so the first request doesn't stall
# on a ~90MB download, and so the container works without outbound HF access.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

COPY app ./app
COPY frontend ./frontend
COPY scripts ./scripts

# SQLite file and Milvus Lite database live here; mount a volume to persist.
RUN mkdir -p /app/data

# PORT is injected by some PaaS hosts (Railway, Render, Fly) and must be obeyed
# rather than assumed, so both the server and the healthcheck read it. Shell
# form is required for the expansion — exec form passes ${PORT} through
# literally.
ENV PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
    CMD curl -fsS "http://localhost:${PORT}/health" || exit 1

# `exec` so uvicorn replaces the shell as PID 1 and receives SIGTERM directly:
# without it the shell swallows the signal and the platform resorts to SIGKILL,
# which is how a volume-backed SQLite file gets cut off mid-write.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}"
