# syntax=docker/dockerfile:1.7
# ── Build stage ────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

# uv from the official distroless image
COPY --from=ghcr.io/astral-sh/uv:0.5.18 /uv /usr/local/bin/uv

WORKDIR /app

# Install deps first for better layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Now copy the project
COPY . .

# Collect static files (uses settings; needs a SECRET_KEY)
ENV DJANGO_SECRET_KEY=build-time-not-a-secret \
    DJANGO_DEBUG=False \
    DJANGO_ALLOWED_HOSTS=*
RUN uv run manage.py collectstatic --noinput || true

# ── Runtime stage ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    DJANGO_DEBUG=False

# Non-root user
RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app
COPY --from=builder --chown=app:app /app /app

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request, sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz/', timeout=3).status==200 else 1)" \
        || exit 1

# Run migrations then start Daphne (ASGI — serves both HTTP and WebSocket).
CMD ["sh", "-c", "python manage.py migrate --noinput && daphne -b 0.0.0.0 -p 8000 config.asgi:application"]
