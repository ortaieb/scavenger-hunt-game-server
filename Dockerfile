# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Builder: installs a standalone CPython and the project's runtime deps with uv.
# ---------------------------------------------------------------------------
FROM debian:bookworm-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Install the interpreter first so it is cached independently of dependency changes.
COPY .python-version ./
RUN uv python install \
    # Strip parts of the standalone CPython a headless server never uses.
    && cd /python/cpython-* \
    && rm -rf include share lib/*.a lib/pkgconfig \
    && cd lib/python3.* \
    && rm -rf ensurepip idlelib tkinter turtledemo test lib2to3 site-packages/pip*

# Dependencies only (no project code) for better layer caching.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project

# Now the project itself, installed non-editable so the runtime image needs no source tree.
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# Empty data directory; distroless has no shell to create it in the runtime stage.
RUN mkdir -p /app/data/images

# ---------------------------------------------------------------------------
# Runtime: distroless (no shell, no package manager), runs as non-root.
# ---------------------------------------------------------------------------
FROM gcr.io/distroless/cc-debian12:nonroot AS runtime

# Paths must match the builder: the venv's interpreter symlinks point into /python.
COPY --from=builder --chown=nonroot:nonroot /python /python
COPY --from=builder --chown=nonroot:nonroot /app/.venv /app/.venv
# Writable by the non-root user; mount a volume here to keep images across restarts.
COPY --from=builder --chown=nonroot:nonroot /app/data /app/data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    GAME_SERVER_HOST=0.0.0.0 \
    GAME_SERVER_PORT=8000 \
    GAME_SERVER_IMAGE_BASE_PATH=/app/data/images

WORKDIR /app
USER nonroot
EXPOSE 8000
VOLUME ["/app/data"]

ENTRYPOINT ["/app/.venv/bin/python", "-m", "game_server"]
