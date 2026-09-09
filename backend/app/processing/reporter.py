"""Publicação de progresso de job no banco, com throttle.

Um job de 3.000 imagens emite milhares de eventos; gravar todos seria escrita
inútil. O reporter só persiste a cada `min_interval` segundos, e sempre nas
transições de etapa e no fim.
"""
from __future__ import annotations

import time
from pathlib import Path

from ..db import session_scope
from ..models import Job
from .stages import STAGE_LABEL, overall_progress


class JobReporter:
    def __init__(self, job_id: str, log_path: Path, min_interval: float = 0.5):
        self.job_id = job_id
        self.log_path = log_path
        self.min_interval = min_interval
        self._last_write = 0.0
        self._last_stage = -1
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.log_path.open("a", encoding="utf-8")

    def log(self, line: str) -> None:
        self._log_handle.write(f"{time.strftime('%H:%M:%S')} {line}\n")
        self._log_handle.flush()

    def progress(
        self,
        stage: int,
        fraction: float,
        message: str = "",
        *,
        images_done: int | None = None,
        images_total: int | None = None,
        force: bool = False,
    ) -> None:
        now = time.monotonic()
        changed_stage = stage != self._last_stage
        if not (force or changed_stage) and now - self._last_write < self.min_interval:
            return
        self._last_write = now
        self._last_stage = stage
        with session_scope() as db:
            job = db.get(Job, self.job_id)
            if job is None:
                return
            job.stage = stage
            job.stage_label = message or STAGE_LABEL.get(stage, "")
            job.progress = overall_progress(stage, fraction)
            if images_done is not None:
                job.images_done = images_done
            if images_total is not None:
                job.images_total = images_total
        if changed_stage:
            self.log(f"[etapa {stage}] {STAGE_LABEL.get(stage, '')} — {message}")

    def is_canceled(self) -> bool:
        with session_scope() as db:
            job = db.get(Job, self.job_id)
            return bool(job and job.cancel_requested)

    def close(self) -> None:
        try:
            self._log_handle.close()
        except Exception:
            pass
