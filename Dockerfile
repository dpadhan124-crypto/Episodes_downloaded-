# --- Pocket FM Telegram Bot ------------------------------------------------
FROM python:3.12-slim

# Make Python fast and quiet, don't buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install FFmpeg (and CA certs) in a single layer, then clean up
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Verify ffmpeg is present at build time (fails the build early if not)
RUN ffmpeg -version && ffprobe -version

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py ./

# Workers don't bind ports; just run the bot
CMD ["python", "-u", "bot.py"]