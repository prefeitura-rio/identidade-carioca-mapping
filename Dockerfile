# Multi-stage Dockerfile for Identidade Carioca Mapping Service
# Based on SPEC.md Section 5 Docker Architecture

# Stage 1: Base image with dependencies
FROM python:3.11-slim AS base

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_NO_CACHE=1

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create app user for security
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

# Set working directory
WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install uv for package management
RUN pip install uv

# Install dependencies
RUN uv sync --frozen --no-dev

# Copy application code
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY alembic.ini ./

# Change ownership to app user
RUN chown -R appuser:appuser /app
RUN mkdir -p /tmp/uv-cache && chown -R appuser:appuser /tmp/uv-cache

# Switch to non-root user
USER appuser

# Stage 2: API service
FROM base AS api

# Expose port for API service
EXPOSE 8080

# Health check for API service
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/healthz || exit 1

# Command to run FastAPI with uvicorn
CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]

# Stage 3: Background tasks service
FROM base AS background

# No exposed ports for background service
# Health check for background service (check if process is running)
HEALTHCHECK --interval=60s --timeout=10s --start-period=10s --retries=3 \
    CMD pgrep -f "python -m app.background_tasks" || exit 1

# Command to run background tasks with APScheduler
CMD ["/app/.venv/bin/python", "-m", "app.background_tasks"]
