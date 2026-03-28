# ==============================================================
# Dockerfile — FIXED (Auth Service)
# ==============================================================

# Stage 1: Builder
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# 🔥 Install globally (NO --user)
RUN pip install --no-cache-dir -r requirements.txt


# Stage 2: Production
FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# 🔥 Copy global python packages
COPY --from=builder /usr/local /usr/local

# Copy application code
COPY shared/ ./shared/
COPY billing/   ./billing/

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser appuser
RUN chown -R appuser:appuser /app

USER appuser

# Environment
ENV PYTHONPATH=/app
ENV ENV=production

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/health')"

EXPOSE 8000

CMD ["uvicorn", "billing.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
