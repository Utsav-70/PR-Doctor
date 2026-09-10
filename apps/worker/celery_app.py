"""Celery application.

Two queues from the start: `reviews` (long, expensive) and `posting` (short,
latency-sensitive). Separating them now means a backlog of reviews can never delay
posting a finished one — retrofitting that split later means re-routing live tasks.
"""

from __future__ import annotations

from celery import Celery

from settings import get_settings

settings = get_settings()

celery_app = Celery(
    "prguard",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_RESULT_URL,
    include=["apps.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Acknowledge only after the task finishes, so a worker killed mid-review has its
    # job redelivered rather than silently dropped. Requires idempotent tasks — ours
    # are, because a review is keyed on the head SHA.
    task_acks_late=True,
    worker_prefetch_multiplier=1,  # long tasks: do not hoard messages
    task_reject_on_worker_lost=True,
    task_soft_time_limit=settings.CELERY_TASK_SOFT_TIME_LIMIT,
    task_time_limit=settings.CELERY_TASK_TIME_LIMIT,
    task_track_started=True,
    result_expires=3600,
    task_default_queue="reviews",
    task_routes={
        "review.pull_request": {"queue": "reviews"},
        "review.post": {"queue": "posting"},
    },
)
