"""Projetos (voos), ingestão do dataset e consulta de imagens."""
from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..geo.crs import utm_epsg
from ..geo.footprint import CameraGeometry, footprint_corners
from ..ingest.summary import build_summary
from ..models import Image, Job, JobStatus, Product, Project, ProjectStatus
from ..processing.quality import resolve_quality
from ..processing.queue import enqueue
from ..schemas import ImageOut, ProductOut, ProjectCreate, ProjectOut, ProjectUpdate, ScanRequest
from ..utils.images import make_thumbnail

router = APIRouter(prefix="/api/projects", tags=["projects"])


def _get_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "projeto não encontrado")
    return project


def _check_scan_root(path: Path) -> Path:
    path = Path(path).expanduser()
    if not path.exists():
        raise HTTPException(400, f"caminho não encontrado: {path}")
    if not path.is_dir():
        raise HTTPException(400, "o caminho precisa ser uma pasta (a raiz do voo)")
    allowed = [p for p in settings.scan_allowed_roots.split(":") if p]
    if allowed and not any(str(path.resolve()).startswith(str(Path(a).resolve())) for a in allowed):
        raise HTTPException(403, "caminho fora das raízes permitidas para varredura")
    return path.resolve()


@router.post("", response_model=ProjectOut, status_code=201)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db)) -> Project:
    project = Project(
        name=payload.name, description=payload.description,
        quality=resolve_quality(payload.quality),
        output_epsg=payload.output_epsg, target_gsd_cm=payload.target_gsd_cm,
        source_path=payload.source_path,
    )
    db.add(project)
    db.commit()
    settings.project_dir(project.id).mkdir(parents=True, exist_ok=True)
    return project


@router.get("", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db), limit: int = 50, offset: int = 0):
    return db.scalars(
        select(Project).order_by(Project.updated_at.desc()).limit(limit).offset(offset)
    ).all()


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: str, db: Session = Depends(get_db)) -> Project:
    return _get_project(db, project_id)


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(project_id: str, payload: ProjectUpdate, db: Session = Depends(get_db)):
    project = _get_project(db, project_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(project, field, value)
    db.commit()
    return project


@router.delete("/{project_id}", status_code=204, response_model=None)
def delete_project(project_id: str, db: Session = Depends(get_db)) -> None:
    project = _get_project(db, project_id)
    db.delete(project)
    db.commit()
    shutil.rmtree(settings.project_dir(project_id), ignore_errors=True)


@router.post("/{project_id}/scan", status_code=202)
def scan_folders(project_id: str, payload: ScanRequest, db: Session = Depends(get_db)) -> dict:
    """Varre as pastas escolhidas no servidor.

    Uma pasta ou dez: todas as imagens encontradas, em qualquer subpasta,
    entram no MESMO dataset e geram um único ortomosaico.
    """
    project = _get_project(db, project_id)
    selected = payload.all_paths()
    if not selected:
        raise HTTPException(400, "selecione ao menos uma pasta de imagens")
    roots = [str(_check_scan_root(Path(item))) for item in selected]

    job = Job(project_id=project.id, kind="scan", engine="-", options={"roots": roots})
    project.source_path = roots[0]
    project.source_paths = roots
    project.source_kind = "server"
    project.status = ProjectStatus.SCANNING
    db.add(job)
    db.commit()
    backend = enqueue(job.id, "scan")
    return {"job_id": job.id, "queue": backend, "roots": roots}


@router.post("/{project_id}/upload", status_code=202)
async def upload_images(
    project_id: str,
    files: list[UploadFile] = File(...),
    relative_paths: list[str] = Form(default=[]),
    finalize: bool = Form(default=False),
    db: Session = Depends(get_db),
) -> dict:
    """Recebe um lote de imagens do navegador preservando a árvore de pastas.

    O input do navegador entrega `webkitRelativePath`; guardamos a mesma
    hierarquia em disco para que a descoberta recursiva funcione igual ao modo
    servidor. Vários lotes alimentam o mesmo projeto, e `finalize=true` dispara
    a varredura.
    """
    project = _get_project(db, project_id)
    root = settings.project_dir(project_id) / "images"
    root.mkdir(parents=True, exist_ok=True)

    saved = 0
    for index, upload in enumerate(files):
        relative = (
            relative_paths[index] if index < len(relative_paths) else upload.filename or ""
        )
        relative = relative.replace("\\", "/").lstrip("/")
        # Impede que um caminho manipulado escreva fora do diretório do projeto.
        destination = (root / relative).resolve()
        if not str(destination).startswith(str(root.resolve())):
            raise HTTPException(400, f"caminho inválido no upload: {relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as fh:
            while chunk := await upload.read(1024 * 1024):
                fh.write(chunk)
        saved += 1

    project.source_kind = "upload"
    project.source_path = str(root)
    db.commit()

    if not finalize:
        return {"saved": saved, "finalized": False}

    job = Job(project_id=project.id, kind="scan", engine="-", options={"roots": [str(root)]})
    db.add(job)
    project.source_paths = [str(root)]
    project.status = ProjectStatus.SCANNING
    db.commit()
    return {"saved": saved, "finalized": True, "job_id": job.id, "queue": enqueue(job.id, "scan")}


@router.get("/{project_id}/summary")
def project_summary(project_id: str, db: Session = Depends(get_db)) -> dict:
    project = _get_project(db, project_id)
    if project.summary:
        return project.summary
    images = db.scalars(select(Image).where(Image.project_id == project_id)).all()
    return build_summary(images)


@router.get("/{project_id}/images", response_model=list[ImageOut])
def list_images(
    project_id: str,
    db: Session = Depends(get_db),
    limit: int = Query(200, le=2000),
    offset: int = 0,
    only_invalid: bool = False,
    folder: str | None = None,
):
    query = select(Image).where(Image.project_id == project_id)
    if only_invalid:
        query = query.where(Image.is_valid.is_(False))
    if folder:
        query = query.where(Image.folder == folder)
    return db.scalars(
        query.order_by(Image.captured_at, Image.relative_path).limit(limit).offset(offset)
    ).all()


@router.get("/{project_id}/images.geojson")
def images_geojson(
    project_id: str, db: Session = Depends(get_db), footprints: bool = True
) -> dict:
    """Posições das câmeras e, opcionalmente, o footprint de cada imagem."""
    images = db.scalars(
        select(Image).where(
            Image.project_id == project_id,
            Image.is_valid.is_(True),
            Image.latitude.is_not(None),
        )
    ).all()
    if not images:
        return {"type": "FeatureCollection", "features": []}

    epsg = utm_epsg(images[0].latitude, images[0].longitude)
    from ..geo.crs import from_utm

    features = []
    for img in images:
        properties = {
            "id": img.id, "file": img.relative_path, "folder": img.folder,
            "altitude": img.relative_altitude or img.altitude, "yaw": img.yaw,
            "captured_at": img.captured_at.isoformat() if img.captured_at else None,
            "camera": img.camera_model, "band": img.band,
        }
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [img.longitude, img.latitude]},
            "properties": {**properties, "kind": "camera"},
        })
        altitude = img.relative_altitude
        if (
            footprints and altitude and img.width and img.height
            and img.focal_length_mm and img.sensor_width_mm
        ):
            geometry = CameraGeometry(
                img.width, img.height, img.focal_length_mm, img.sensor_width_mm
            )
            corners = footprint_corners(
                geometry, img.longitude, img.latitude, altitude, img.yaw, epsg
            )
            ring = [list(from_utm(x, y, epsg)) for x, y in corners]
            features.append({
                "type": "Feature",
                "geometry": {"type": "Polygon", "coordinates": [ring + [ring[0]]]},
                "properties": {**properties, "kind": "footprint"},
            })
    return {"type": "FeatureCollection", "features": features}


@router.get("/{project_id}/folders")
def list_folders(project_id: str, db: Session = Depends(get_db)) -> list[dict]:
    """Subpastas encontradas — informação de diagnóstico, não de agrupamento."""
    rows = db.execute(
        select(Image.folder, func.count(Image.id))
        .where(Image.project_id == project_id)
        .group_by(Image.folder)
        .order_by(Image.folder)
    ).all()
    return [{"folder": folder, "images": count} for folder, count in rows]


@router.get("/{project_id}/products", response_model=list[ProductOut])
def list_products(project_id: str, db: Session = Depends(get_db)):
    return db.scalars(
        select(Product).where(Product.project_id == project_id).order_by(Product.created_at.desc())
    ).all()


@router.get("/{project_id}/preview.jpg")
def preview(project_id: str, db: Session = Depends(get_db)):
    product = db.scalars(
        select(Product)
        .where(Product.project_id == project_id, Product.kind == "orthomosaic")
        .order_by(Product.created_at.desc())
    ).first()
    if product is None:
        raise HTTPException(404, "nenhum ortomosaico gerado")
    path = Path(product.path).with_name("preview.jpg")
    if not path.exists():
        make_thumbnail(Path(product.path), path, size=512)
    if not path.exists():
        raise HTTPException(404, "pré-visualização indisponível")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/{project_id}/jobs")
def project_jobs(project_id: str, db: Session = Depends(get_db)) -> list[dict]:
    jobs = db.scalars(
        select(Job).where(Job.project_id == project_id).order_by(Job.created_at.desc())
    ).all()
    return [
        {
            "id": j.id, "kind": j.kind, "engine": j.engine, "status": j.status,
            "stage": j.stage, "stage_label": j.stage_label, "progress": j.progress,
            "images_done": j.images_done, "images_total": j.images_total,
            "warnings": j.warnings, "error": j.error,
            "created_at": j.created_at, "started_at": j.started_at,
            "finished_at": j.finished_at,
        }
        for j in jobs
    ]


@router.get("/{project_id}/stats")
def project_stats(project_id: str, db: Session = Depends(get_db)) -> dict:
    project = _get_project(db, project_id)
    running = db.scalar(
        select(func.count(Job.id)).where(
            Job.project_id == project_id, Job.status.in_([JobStatus.RUNNING, JobStatus.QUEUED])
        )
    )
    return {"status": project.status, "active_jobs": running, "summary": project.summary}
