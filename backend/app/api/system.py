"""Informações do sistema: motores, recursos, navegação de pastas e dashboard."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..geo.crs import describe, utm_epsg
from ..models import Image, Job, JobStatus, Product, Project, ProjectStatus
from ..processing.engines import available_engines
from ..utils.images import make_thumbnail
from ..utils.sysinfo import resource_usage

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/system/info")
def system_info() -> dict:
    return {
        "engines": available_engines(),
        "queue_backend": settings.queue_backend,
        "storage_root": str(settings.storage_root),
        "default_engine": settings.default_engine,
        "resources": resource_usage(),
    }


@router.get("/system/resources")
def resources() -> dict:
    return resource_usage()


@router.get("/system/browse")
def browse(path: str = Query(default="")) -> dict:
    """Navegador de pastas do servidor, para escolher a raiz do voo."""
    allowed = [Path(p) for p in settings.scan_allowed_roots.split(":") if p]
    base = Path(path).expanduser() if path else (allowed[0] if allowed else Path.home())
    base = base.resolve()
    if allowed and not any(str(base).startswith(str(root.resolve())) for root in allowed):
        raise HTTPException(403, "caminho fora das raízes permitidas")
    if not base.is_dir():
        raise HTTPException(400, "caminho não é uma pasta")

    entries = []
    try:
        for entry in sorted(os.scandir(base), key=lambda e: e.name.lower()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir(follow_symlinks=False):
                entries.append({"name": entry.name, "path": entry.path, "type": "dir"})
    except PermissionError:
        raise HTTPException(403, "sem permissão de leitura nessa pasta") from None
    return {"path": str(base), "parent": str(base.parent), "entries": entries[:500]}


@router.get("/system/crs/suggest")
def suggest_crs(lat: float, lon: float) -> dict:
    epsg = utm_epsg(lat, lon)
    return {"suggested": describe(epsg), "wgs84": describe(4326)}


@router.get("/stats")
def dashboard_stats(db: Session = Depends(get_db)) -> dict:
    projects = db.scalars(select(Project)).all()
    processing = [p for p in projects if p.status == ProjectStatus.PROCESSING]
    completed = [p for p in projects if p.status == ProjectStatus.COMPLETED]
    total_images = db.scalar(select(func.count(Image.id))) or 0
    area = sum(
        (p.summary or {}).get("area_ha") or 0
        for p in completed
    )
    active_jobs = db.scalars(
        select(Job).where(Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
    ).all()
    return {
        "projects_total": len(projects),
        "projects_processing": len(processing),
        "projects_completed": len(completed),
        "images_total": total_images,
        "area_ha_total": round(area, 1),
        "orthomosaics": db.scalar(
            select(func.count(Product.id)).where(Product.kind == "orthomosaic")
        ) or 0,
        "active_jobs": [
            {"id": j.id, "project_id": j.project_id, "progress": j.progress,
             "stage_label": j.stage_label, "kind": j.kind}
            for j in active_jobs
        ],
        "recent_projects": [
            {"id": p.id, "name": p.name, "status": p.status,
             "images": (p.summary or {}).get("valid_images"),
             "area_ha": (p.summary or {}).get("area_ha"),
             "updated_at": p.updated_at}
            for p in sorted(projects, key=lambda p: p.updated_at, reverse=True)[:8]
        ],
    }


@router.get("/images/{image_id}/thumbnail.jpg")
def image_thumbnail(image_id: str, db: Session = Depends(get_db)):
    image = db.get(Image, image_id)
    if image is None:
        raise HTTPException(404, "imagem não encontrada")
    cache = settings.project_dir(image.project_id) / "thumbs" / f"{image.id}.jpg"
    if not cache.exists() and not make_thumbnail(Path(image.path), cache):
        raise HTTPException(404, "não foi possível gerar a miniatura")
    return FileResponse(cache, media_type="image/jpeg")
