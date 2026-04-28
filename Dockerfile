FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_SYSTEM_PYTHON=1

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock* /app/
RUN uv sync --frozen --no-install-project || uv sync --no-install-project

COPY src /app/src
COPY policies /app/policies
COPY models /app/models
RUN uv sync --frozen || uv sync

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "aegis.main:app", "--host", "0.0.0.0", "--port", "8000"]
