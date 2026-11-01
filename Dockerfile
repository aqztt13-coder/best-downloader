
FROM python:3.12-slim

# Python environment
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=10000 \
    HOME=/home/app

# Install FFmpeg and SSL certificates
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt ./

RUN python -m pip install --no-cache-dir -r requirements.txt

# Prepare application directories
RUN mkdir -p /home/app \
    && chown 10001:10001 /home/app /app

# Copy application files with correct permissions
COPY --chown=10001:10001 . .

# Run as non-root user
USER 10001:10001

# Render HTTP port
EXPOSE 10000

# Production server
# Single worker keeps progress data shared between requests
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-10000} --workers 1 --worker-class gthread --threads 8 --timeout 300 --graceful-timeout 30 --access-logfile - --error-logfile - app:app"]
