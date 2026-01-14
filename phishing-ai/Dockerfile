# Phishing Detection API - Docker Build
# Multi-stage build for optimized image size

# =============================================================================
# Stage 1: Builder - Install dependencies
# =============================================================================
FROM python:3.11-slim as builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Create cache directory (will be empty if model download fails)
RUN mkdir -p /root/.cache/huggingface

# Pre-download the HuggingFace model during build (faster container startup)
# This step is optional - if it fails due to network issues, the model will be downloaded at runtime
RUN python -c "from transformers import AutoTokenizer, AutoModelForSequenceClassification; \
    AutoTokenizer.from_pretrained('ElSlay/BERT-Phishing-Email-Model'); \
    AutoModelForSequenceClassification.from_pretrained('ElSlay/BERT-Phishing-Email-Model')" \
    || echo "Model pre-download skipped - will download at runtime"

# =============================================================================
# Stage 2: Runtime - Minimal production image
# =============================================================================
FROM python:3.11-slim as runtime

WORKDIR /app

# Install runtime dependencies only
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Required for DNS lookups
    dnsutils \
    # Required for whois lookups
    whois \
    # Required for psycopg2
    libpq5 \
    # Useful for debugging
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash appuser

# Copy pre-downloaded model from builder (may be empty if download failed)
COPY --from=builder /root/.cache/huggingface /home/appuser/.cache/huggingface
RUN chown -R appuser:appuser /home/appuser/.cache

# Copy application code
COPY --chown=appuser:appuser . .

# Ensure appuser owns everything
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=5000 \
    HF_HOME=/home/appuser/.cache/huggingface

# Expose port
EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:5000/health || exit 1

# Run the application
CMD ["python", "app.py"]
