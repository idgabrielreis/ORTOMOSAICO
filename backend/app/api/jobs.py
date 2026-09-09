"""Jobs: enfileiramento, acompanhamento em tempo real e cancelamento."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import SessionLocal, get_db
from ..models import Image, Job, JobStatus, Project, ProjectStatus
from ..processing.engines import resolve_engine
from ..processing.queue import enqueue
from ..schemas import JobCreate, JobOut
from ..utils.sysinfo import resource_usage

router = APIRouter(prefix="/api", tags=["jobs"])


@router.post("/projects/{project_id}/jobs", response_model=JobOut, status_code=202)
def create_job(project_id: str, payload: JobCreate, db: Session = Depends(get_db)) -> Job:
    """Processa o VOO INTEIRO: todas as imagens do projeto, de todas as subpastas."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "projeto não encontrado")

    valid = db.scalar(
        select(Image).where(
            Image.project_id == project_id, Image.is_valid.is_(True), Image.is_duplicate.is_(False)
        )
    )
    if valid is None:
        raise HTTPException(400, "o projeto não tem imagens válidas; rode a varredura primeiro")

    active = db.scalars(
        select(Job).where(
            Job.project_id == project_id,
            Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
        )
    ).first()
    if active is not None:
        raise HTTPException(409, f"já existe um job em andamento ({active.id})")

    engine = resolve_engine(payload.engine)
    available, reason = engine.availability()
    if not available:
        raise HTTPException(400, f"motor {engine.name} indisponível: {reason}")

    job = Job(
        project_id=project_id,
        kind="orthomosaic",
        engine=engine.name,
        options={
            "quality": payload.quality,
            "fast_orthophoto": payload.fast_orthophoto,
            "multispectral": payload.multispectral,
            **payload.options,
        },
    )
    db.add(job)
    project.status = ProjectStatus.PROCESSING
    db.commit()
    enqueue(job.id, "orthomosaic")
    return job


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: str, db: Session = Depends(get_db)) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job não encontrado")
    return job


@router.post("/jobs/{job_id}/cancel", response_model=JobOut)
def cancel_job(job_id: str, db: Session = Depends(get_db)) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job não encontrado")
    if job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELED):
        return job
    job.cancel_requested = True
    db.commit()
    return job


@router.get("/jobs/{job_id}/log", response_class=PlainTextResponse)
def job_log(job_id: str, db: Session = Depends(get_db), tail: int = 200) -> str:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(404, "job não encontrado")
    path = settings.project_dir(job.project_id) / "logs" / f"{job_id}.log"
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return "\n".join(lines[-tail:])


def _job_payload(job: Job) -> dict:
    elapsed = None
    if job.started_at:
        end = job.finished_at or __import__("datetime").datetime.now(job.started_at.tzinfo)
        elapsed = (end - job.started_at).total_seconds()
    remaining = None
    if elapsed and 0 < job.progress < 100:
        remaining = elapsed * (100 - job.progress) / job.progress
    return {
        "id": job.id, "status": job.status, "stage": job.stage,
        "stage_label": job.stage_label, "progress": job.progress,
        "images_done": job.images_done, "images_total": job.images_total,
        "warnings": job.warnings, "error": job.error,
        "elapsed_seconds": round(elapsed) if elapsed else None,
        "eta_seconds": round(remaining) if remaining else None,
        "resources": resource_usage(),
    }


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    """Progresso por Server-Sent Events: sem polling agressivo do frontend."""

    async def stream():
        last = None
        while True:
            with SessionLocal() as db:
                job = db.get(Job, job_id)
                if job is None:
                    yield "event: error\ndata: {}\n\n"
                    return
                payload = _job_payload(job)
                status = job.status
            if payload != last:
                yield f"data: {json.dumps(payload, default=str)}\n\n"
                last = payload
            if status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELED):
                yield "event: done\ndata: {}\n\n"
                return
            await asyncio.sleep(1.0)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
