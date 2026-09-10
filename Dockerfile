# Multi-stage: one dependency layer shared by both services, two thin entrypoint
# targets. The api and worker images differ only in CMD, so the expensive pip layer
# is built and cached once.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Copy requirements alone first so a source change does not invalidate the pip layer.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Run as non-root. The API holds the GitHub App private key in memory; there is no
# reason for that process to be able to write to its own image.
RUN useradd --create-home --uid 10001 prguard
COPY --chown=prguard:prguard . .
USER prguard


FROM base AS api
EXPOSE 8000
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]


FROM base AS worker
# --without-gossip/--without-mingle cut startup chatter; -Q pins the queues this
# worker serves so review backlog cannot delay posting later.
CMD ["celery", "-A", "apps.worker.celery_app:celery_app", "worker", \
     "--loglevel=INFO", "--concurrency=4", "-Q", "reviews,posting", \
     "--without-gossip", "--without-mingle"]
