# ==============================================================================
# Multi-arch Lightweight Python 3.12 Slim Image for VPS Monitoring Dashboard
# ==============================================================================
FROM python:3.12-slim

WORKDIR /app

# Set environment flags
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install basic dependencies if needed (e.g. gcc for psutil if binary wheel not found)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Install python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app/ ./app/

# Expose internal FastAPI port
EXPOSE 8000

# Healthcheck for Docker engine
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/ping')" || exit 1

# Launch uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
