"""Worker Celery (opcional).

    celery -A app.processing.celery_app worker --loglevel=info --concurrency=1

Concorrência 1 por worker é proposital: o ODM já usa todos os núcleos da
máquina, e dois jobs simultâneos competiriam por CPU e memória. Para processar
vários voos ao mesmo tempo, suba mais workers em máquinas diferentes.
"""
from __future__ import annotations

from celery import Celery  # type: ignore[import-not-found]

from ..config import settings

celery = Celery("ortomosaico", broker=settings.celery_broker_url,
                backend=settings.celery_broker_url)
celery.conf.update(task_track_started=True, worker_prefetch_multiplier=1,
                   task_acks_late=True)


@celery.task(name="ortomosaico.run_job")
def run_job_task(job_id: str, kind: str) -> None:
    from .pipeline import run_job

    run_job(job_id, kind)
