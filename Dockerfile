# syntax=docker/dockerfile:1

FROM python:3.14-slim

LABEL org.opencontainers.image.title="kaufland-bons" \
      org.opencontainers.image.description="Kaufland Card receipts to Paperless-ngx" \
      org.opencontainers.image.licenses="GPL-3.0-only" \
      org.opencontainers.image.source="https://github.com/BlackDark/kaufland-api"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git tini gosu tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
RUN playwright install --with-deps chromium

# Upstream client, pinned. It declares python>=3.15; the code only needs PEP 758
# (3.14), so the constraint is relaxed for this image.
ARG KAUFLAND_REF=66e39463a4c7bf0d0adc1d2b4474548fbd6f7461
RUN git clone --quiet https://github.com/BlackDark/kaufland-api /opt/kaufland-src \
 && cd /opt/kaufland-src \
 && git checkout --quiet "${KAUFLAND_REF}" \
 && sed -i 's/requires-python = ">=3.15"/requires-python = ">=3.14"/' pyproject.toml \
 && rm -f uv.lock .python-version \
 && pip install --no-cache-dir . \
 && rm -rf /opt/kaufland-src

WORKDIR /app
COPY app/ /app/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/config", "/receipts", "/import"]
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["daemon"]
