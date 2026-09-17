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

# Ensure persistent data directory exists
RUN mkdir -p /app/data

# Copy application source
COPY main.py .

CMD ["python", "main.py"]
