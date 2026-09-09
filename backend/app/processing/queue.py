"""Fila de execução.

Duas implementações atrás da mesma função `enqueue`:

* **local**: thread daemon no processo da API. Serve para desenvolvimento e
  instalação de máquina única, sem Redis.
* **celery**: worker separado, escalável e com GPU. Ativado por
  `ORTO_QUEUE_BACKEND=celery`.

O trabalho pesado nunca roda no request: em ambos os casos a API responde
imediatamente com o job enfileirado.
"""
from __future__ import annotations

import threading

from ..config import settings

_local_threads: dict[str, threading.Thread] = {}


def _run_local(job_id: str, kind: str) -> None:
    from .pipeline import run_job

    thread = threading.Thread(target=run_job, args=(job_id, kind), daemon=True,
                              name=f"job-{job_id[:8]}")
    _local_threads[job_id] = thread
    thread.start()


def enqueue(job_id: str, kind: str) -> str:
    if settings.queue_backend == "celery":
        from .celery_app import run_job_task

        run_job_task.delay(job_id, kind)
        return "celery"
    _run_local(job_id, kind)
    return "local"
