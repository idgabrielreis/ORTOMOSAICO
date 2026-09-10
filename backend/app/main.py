"""Aplicação FastAPI."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import exports, jobs, projects, system
from .config import settings
from .db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    _recover_orphan_jobs()
    yield


def _recover_orphan_jobs() -> None:
    """Fecha jobs que morreram junto com o processo anterior.

    Com a fila local o processamento roda em uma thread da própria API: se o
    servidor cair no meio, o job fica marcado como em execução para sempre e o
    projeto trava, sem deixar iniciar outro. Aqui eles são marcados como falha
    logo na subida, com o motivo explícito.
    """
    from datetime import datetime, timezone

    from .db import session_scope
    from .models import Job, JobStatus, Project, ProjectStatus

    with session_scope() as db:
        orphans = (
            db.query(Job)
            .filter(Job.status.in_([JobStatus.RUNNING, JobStatus.QUEUED]))
            .all()
        )
        for job in orphans:
            job.status = JobStatus.FAILED
            job.error = "o processamento foi interrompido pela parada do servidor"
            job.finished_at = datetime.now(timezone.utc)
            project = db.get(Project, job.project_id)
            if project and project.status in (
                ProjectStatus.PROCESSING, ProjectStatus.SCANNING
            ):
                project.status = (
                    ProjectStatus.READY if job.kind == "orthomosaic" else ProjectStatus.CREATED
                )
        if orphans:
            logging.getLogger(__name__).warning(
                "%d job(s) interrompidos foram encerrados na inicialização", len(orphans)
            )


app = FastAPI(
    title="Ortomosaico",
    version="0.1.0",
    summary="Gerenciador de voos de drone e geração de ortomosaicos",
    description=(
        "Uma pasta raiz é um voo. Todas as subpastas abaixo dela entram no mesmo "
        "dataset e geram um único ortomosaico georreferenciado."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects.router)
app.include_router(jobs.router)
app.include_router(exports.router)
app.include_router(system.router)

# Importado por último: registra rotas que dependem do rio-tiler, opcional em
# ambientes sem GDAL.
try:
    from .api import tiles

    app.include_router(tiles.router)
except ImportError as exc:  # pragma: no cover
    logging.getLogger(__name__).warning("tiles indisponíveis: %s", exc)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}
