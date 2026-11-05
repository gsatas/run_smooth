FROM python:3.12-slim
# curl: all HTTP (Smashrun, Overpass) goes through curl via subprocess.
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY smoothrun ./smoothrun
COPY server.py ./
ENV PYTHONUNBUFFERED=1 MPLCONFIGDIR=/tmp/matplotlib HOST=0.0.0.0 PORT=8000 \
    TOKEN_FILE=/run/secrets/mile_marker/smashrun_token \
    PASSPHRASE_FILE=/run/secrets/mile_marker/smooth_passphrase CACHE_DIR=/data/cache BACKUP_DIR=/data/backups \
    MIRROR_DIR=/data/mirror \
    OUTPUT_DIR=/tmp/run-smooth
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fsS http://127.0.0.1:8000/healthz >/dev/null || exit 1
CMD ["python", "server.py"]
