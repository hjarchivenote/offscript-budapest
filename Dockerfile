# syntax=docker/dockerfile:1
ARG PYTHON_BASE=python:3.12-alpine3.24@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89

FROM ${PYTHON_BASE} AS dependencies
ENV PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
RUN python -m venv /opt/venv
COPY requirements-runtime.lock ./
RUN --mount=type=cache,target=/root/.cache/pip \
    /opt/venv/bin/python -m pip install --disable-pip-version-check --no-compile \
    --only-binary=:all: -r requirements-runtime.lock \
    && /opt/venv/bin/python -m pip check

FROM ${PYTHON_BASE} AS runtime
ARG APP_COMMIT=local
LABEL org.opencontainers.image.source="https://github.com/hjarchivenote/offscript-budapest"
ENV PATH="/opt/venv/bin:${PATH}" \
    APP_COMMIT=${APP_COMMIT} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    HOST=0.0.0.0 \
    PORT=8000
WORKDIR /app
RUN apk upgrade --no-cache \
    && addgroup -S -g 10001 offscript \
    && adduser -S -D -H -u 10001 -G offscript -s /sbin/nologin offscript
COPY --from=dependencies /opt/venv /opt/venv
COPY app/ ./app/
COPY run.py ./
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/ready', timeout=2).close()"]
CMD ["python", "run.py"]
