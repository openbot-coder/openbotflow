FROM python:3.13-slim AS builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Set work directory
WORKDIR /app

# Copy dependency files first for caching
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-dev --no-install-project

# Copy source code
COPY src/ ./src/

# Install project
RUN uv sync --frozen --no-dev

# Runtime stage
FROM python:3.13-slim

WORKDIR /app

# Copy installed dependencies and source from builder
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

# Add venv to PATH
ENV PATH="/app/.venv/bin:$PATH"

# Set default workspace
ENV BOTFLOW_WORKSPACE=/app/data

# Create data directory
RUN mkdir -p /app/data

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Run botflow
CMD ["botflow", "run", "--host", "0.0.0.0", "--port", "8000"]
