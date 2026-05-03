# ──────────────────────────────────────────────────────────────────────────────
# edgar-gmr-etl  —  GMR Stock Analysis API
# ──────────────────────────────────────────────────────────────────────────────
# Build:   docker build -t edgar-gmr-etl:latest .
# Run:     docker run -p 8000:8000 edgar-gmr-etl:latest
# Swagger: http://localhost:8000/docs
# ──────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

COPY void42-ca.crt /usr/local/share/ca-certificates/void42-ca.crt

# --- OS-level dependencies (minimal) ----------------------------------------
RUN apt-get update -y \
 && apt-get install -y --no-install-recommends gcc ca-certificates \
 && update-ca-certificates \
 && rm -rf /var/lib/apt/lists/*

ENV PIP_INDEX_URL=https://nexus.void42.internal/repository/pypi-proxy/simple/ \
    PIP_TRUSTED_HOST=nexus.void42.internal

# --- Non-root user for security -----------------------------------------------
RUN useradd --create-home --shell /bin/bash appuser
WORKDIR /app

# --- Python dependencies -------------------------------------------------------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && rm requirements.txt

# --- eforms-parser (TED contract loader dependency) ---------------------------
COPY vendor/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl

# --- Application source -------------------------------------------------------
COPY src/ ./src/
COPY data/ ./data/
COPY scripts/ ./scripts/
COPY main.py .

# Switch to non-root
USER appuser

# --- Runtime ------------------------------------------------------------------
EXPOSE 8000

# Workers=1 keeps memory predictable per pod; scale horizontally via replicas.
# --verbosity 3 = INFO (default). Pass --verbosity 4 for DEBUG during debugging.
CMD ["python", "-m", "src.api.run", "--verbosity", "3"]
