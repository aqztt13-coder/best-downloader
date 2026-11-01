FROM python:3.12-slim

# Python, Deno and application environment
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=10000 \
    HOME=/home/app \
    DENO_INSTALL=/usr/local \
    DENO_DIR=/home/app/.cache/deno \
    PATH=/usr/local/bin:${PATH}

# Install FFmpeg, certificates and tools required by the official Deno installer
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        unzip \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install the official Deno runtime (required for modern YouTube JS challenges)
RUN curl -fsSL https://deno.land/install.sh -o /tmp/deno-install.sh \
    && sh /tmp/deno-install.sh \
    && rm -f /tmp/deno-install.sh \
    && deno --version

WORKDIR /app

# Install Python dependencies, including yt-dlp's default EJS components
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && python -c "import yt_dlp; print('yt-dlp:', yt_dlp.version.__version__)"

# Prepare writable runtime/cache directories
RUN mkdir -p /home/app/.cache/deno \
    && chown -R 10001:10001 /home/app /app

# Copy application files with correct permissions
COPY --chown=10001:10001 . .

# Run as non-root user
USER 10001:10001

EXPOSE 10000

# One process keeps in-memory progress jobs shared across request threads
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-10000} --workers 1 --worker-class gthread --threads 8 --timeout 300 --graceful-timeout 30 --access-logfile - --error-logfile - app:app"]
