# Orbit8 Agent API — container image for Fly.io.
#
# Two-stage so the runtime image carries no build tooling. uv is used to
# install from uv.lock, so the deployed dependency set is the one the
# test suite ran against rather than whatever resolves today.

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Dependencies first, as their own layer: source changes then rebuild in
# seconds instead of re-resolving the whole tree.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --extra server

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra server


FROM python:3.12-slim AS runtime

# Non-root: the sandbox already confines generated adapters, but the
# process that spawns them should not be root either.
RUN useradd --create-home --uid 10001 orbit8

WORKDIR /app
COPY --from=builder --chown=orbit8:orbit8 /app/.venv /app/.venv
COPY --from=builder --chown=orbit8:orbit8 /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    ORBIT8_ENV=production \
    ORBIT8_JOBS_ROOT=/data/jobs \
    ORBIT8_HOST=0.0.0.0 \
    ORBIT8_PORT=8080

# The volume mounts here. Created so the image runs without one too.
RUN mkdir -p /data/jobs && chown -R orbit8:orbit8 /data

USER orbit8
EXPOSE 8080

# One worker on purpose: the job runner keeps run-state in memory, so a
# second worker would answer /jobs/{id} from a different view of what is
# running. The durable state is the artifact tree either way, but the UI
# would flicker between two answers. Concurrency comes from the runner's
# threads, not from web workers.
CMD ["python", "-m", "uvicorn", "orbit8.server.asgi:app", \
     "--host", "0.0.0.0", "--port", "8080", "--workers", "1", \
     "--timeout-keep-alive", "75"]
