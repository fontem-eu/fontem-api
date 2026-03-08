# ──────────────────────────────────────────────────────────────────────────────
# edgar-gmr-etl  —  GMR Stock Analysis API
# ──────────────────────────────────────────────────────────────────────────────
# Build:   docker build -t edgar-gmr-etl:latest .
# Run:     docker run -p 8000:8000 edgar-gmr-etl:latest
# Swagger: http://localhost:8000/docs
# ──────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# --- OS-level dependencies (minimal) ----------------------------------------
RUN apt-get update -y \
 && apt-get install -y --no-install-recommends gcc \
 && rm -rf /var/lib/apt/lists/*

# --- Non-root user for security -----------------------------------------------
RUN useradd --create-home --shell /bin/bash appuser
WORKDIR /app

# --- Python dependencies -------------------------------------------------------
COPY Requirements.txt .
RUN pip install --no-cache-dir -r Requirements.txt && rm Requirements.txt

# --- Application source -------------------------------------------------------
COPY src/ ./src/
COPY main.py .

# Switch to non-root
USER appuser

# --- Runtime ------------------------------------------------------------------
EXPOSE 8000

# Workers=1 keeps memory predictable per pod; scale horizontally via replicas.
CMD ["uvicorn", "src.api.app:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--no-access-log"]
