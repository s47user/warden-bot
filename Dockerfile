# Production Dockerfile for Telegram Moderation / Warden Bot
FROM python:3.12-slim

# Prevent Python from writing .pyc files and enable unbuffered streaming logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_FILE=/app/data/group_moderator.db

WORKDIR /app

# Install system dependencies if required for build
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user and persistent data directory
RUN useradd -u 1000 -m -s /bin/bash appuser && \
    mkdir -p /app/data && \
    chown -R appuser:appuser /app

# Copy application source
COPY --chown=appuser:appuser main.py .

USER appuser

VOLUME ["/app/data"]

CMD ["python", "main.py"]
