# ── build: venv + toolchain (C exts) + void42 CA + vendored deps ──────────────
FROM cgr.void42.internal/chainguard/python:latest-dev AS build
USER root
ENV PIP_INDEX_URL=https://nexus.void42.internal/repository/pypi-proxy/simple/ \
    PIP_TRUSTED_HOST=nexus.void42.internal
RUN apk add --no-cache build-base
COPY void42-ca.crt /tmp/void42-ca.crt
RUN cat /tmp/void42-ca.crt >> /etc/ssl/certs/ca-certificates.crt
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY vendor/*.whl /tmp/wheels/
RUN pip install --no-cache-dir /tmp/wheels/*.whl
COPY vendor/gmr-event-schemas/ /tmp/gmr-event-schemas/
COPY vendor/gmr-events/        /tmp/gmr-events/
RUN pip install --no-cache-dir /tmp/gmr-event-schemas /tmp/gmr-events

# ── runtime: distroless; app runs from /app via `python -m src.api.run` ───────
FROM cgr.void42.internal/chainguard/python:latest
WORKDIR /app
COPY --from=build /venv /venv
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
ENV PATH="/venv/bin:$PATH" \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
COPY vendor/geoip/dbip-country-lite.mmdb /app/vendor/geoip/dbip-country-lite.mmdb
COPY vendor/crawler_ranges/ /app/vendor/crawler_ranges/
COPY src/ ./src/
COPY data/ ./data/
COPY scripts/ ./scripts/
COPY main.py .
USER 65532
EXPOSE 8000
ENTRYPOINT ["/venv/bin/python", "-m", "src.api.run"]
CMD ["--verbosity", "3"]
