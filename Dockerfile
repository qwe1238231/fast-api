FROM rust:1.85-slim AS rust-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/build-venv \
    && /opt/build-venv/bin/pip install --no-cache-dir maturin

WORKDIR /build
COPY ticket_secrets/ ./ticket_secrets/

WORKDIR /build/ticket_secrets
RUN /opt/build-venv/bin/maturin build --release --out /wheels

FROM python:3.12-slim AS python-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

COPY --from=rust-builder /wheels/*.whl /tmp/
RUN /opt/venv/bin/pip install --no-cache-dir /tmp/*.whl

FROM python:3.12-slim AS runtime

RUN groupadd -r app && useradd -r -g app app

WORKDIR /app

COPY --from=python-builder /opt/venv /opt/venv

COPY --chown=app:app app/ ./app/
COPY --chown=app:app alembic/ ./alembic/
COPY --chown=app:app alembic.ini ./

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]