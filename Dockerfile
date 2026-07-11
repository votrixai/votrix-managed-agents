FROM python:3.12-slim AS builder

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra sandbox-e2b

COPY . .
RUN uv sync --frozen --no-dev --extra sandbox-e2b

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/app /app/app
COPY --from=builder /app/votrix_managed_agents /app/votrix_managed_agents
COPY --from=builder /app/alembic /app/alembic
COPY --from=builder /app/alembic.ini /app/alembic.ini
COPY --from=builder /app/pyproject.toml /app/pyproject.toml
COPY --from=builder /app/scripts /app/scripts

EXPOSE 8080

CMD ["sh", "scripts/entrypoint.sh"]
