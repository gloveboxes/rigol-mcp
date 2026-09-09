# syntax=docker/dockerfile:1

ARG PYTHON_IMAGE=python:3.12-alpine
FROM ${PYTHON_IMAGE} AS builder

WORKDIR /app
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir uv==0.12.11
COPY pyproject.toml uv.lock LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHON_DOTENV_DISABLED=1 \
    RIGOL_DATA_DIR=/data/captures \
    RIGOL_SCREENSHOT_DIR=/data/screenshots
WORKDIR /app
RUN addgroup -S -g 10001 rigol \
    && adduser -S -D -H -u 10001 -G rigol -h /app rigol \
    && mkdir -p /data/captures /data/screenshots \
    && chown -R rigol:rigol /data
COPY --from=builder /app/.venv /app/.venv
USER 10001:10001
ENTRYPOINT ["rigol-mcp"]