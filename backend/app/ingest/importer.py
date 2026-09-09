"""Importação do voo: descoberta recursiva + metadados -> UM dataset."""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from datetime import timezone
from pathlib import Path

from sqlalchemy import delete

from ..config import settings
from ..db import session_scope
from ..models import Image, Project, ProjectStatus
from ..processing.reporter import JobReporter
from .discovery import discover_images
from .metadata import read_metadata
from .summary import build_summary

BATCH_SIZE = 500


def _workers() -> int:
    import os

    return settings.metadata_workers or min(8, os.cpu_count() or 2)


def import_flight(project_id: str, root: Path | str, reporter: JobReporter) -> dict:
    """Varre `root` inteira e grava as imagens como um único dataset do projeto."""
    root = Path(root)
    reporter.progress(1, 0.0, "Procurando imagens", force=True)
    reporter.log(f"varrendo {root}")

    def on_progress(found: int, scanned: int) -> None:
        reporter.progress(
            1, min(0.95, found / max(found + 500, 1)),
            f"{found} imagens encontradas ({scanned} arquivos varridos)",
            images_total=found,
        )

    result = discover_images(root, progress=on_progress)
    reporter.log(
        f"{len(result.files)} arquivos de imagem em {len(result.folders)} pastas; "
        f"{len(result.valid)} válidos, {len(result.invalid)} inválidos, "
        f"{len(result.duplicates)} duplicados"
    )
    reporter.progress(
        1, 1.0, f"{len(result.files)} imagens em {len(result.folders)} pastas",
        images_total=len(result.files), force=True,
    )

    # Metadados em pool de processos: EXIF/XMP é I/O + parsing, escala bem.
    reporter.progress(2, 0.0, "Lendo metadados EXIF/XMP", force=True)
    to_read = [f for f in result.files if f.is_valid and not f.is_duplicate]
    metadata: dict[str, dict] = {}
    done = 0
    if to_read:
        with ProcessPoolExecutor(max_workers=_workers()) as pool:
            for found, meta in zip(
                to_read, pool.map(read_metadata, [f.path for f in to_read], chunksize=8)
            ):
                metadata[found.relative_path] = meta
                done += 1
                if done % 25 == 0 or done == len(to_read):
                    reporter.progress(
                        2, done / len(to_read), f"Metadados {done}/{len(to_read)}",
                        images_done=done, images_total=len(result.files),
                    )
                if reporter.is_canceled():
                    raise InterruptedError("cancelado pelo usuário")

    with session_scope() as db:
        db.execute(delete(Image).where(Image.project_id == project_id))
        batch: list[Image] = []
        for found in result.files:
            meta = metadata.get(found.relative_path, {})
            captured = meta.get("captured_at")
            if captured is not None and captured.tzinfo is None:
                captured = captured.replace(tzinfo=timezone.utc)
            batch.append(
                Image(
                    project_id=project_id,
                    path=str(found.path),
                    relative_path=found.relative_path,
                    folder=found.folder,
                    filename=found.filename,
                    size_bytes=found.size_bytes,
                    digest=found.digest,
                    is_valid=found.is_valid and not meta.get("error"),
                    invalid_reason=found.invalid_reason or meta.get("error"),
                    is_duplicate=found.is_duplicate,
                    duplicate_of=found.duplicate_of,
                    width=meta.get("width"),
                    height=meta.get("height"),
                    latitude=meta.get("latitude"),
                    longitude=meta.get("longitude"),
                    altitude=meta.get("altitude"),
                    relative_altitude=meta.get("relative_altitude"),
                    captured_at=captured,
                    yaw=meta.get("yaw"),
                    pitch=meta.get("pitch"),
                    roll=meta.get("roll"),
                    camera_make=meta.get("camera_make"),
                    camera_model=meta.get("camera_model"),
                    focal_length_mm=meta.get("focal_length_mm"),
                    focal_35mm=meta.get("focal_35mm"),
                    sensor_width_mm=meta.get("sensor_width_mm"),
                    band=meta.get("band"),
                    rtk_flag=meta.get("rtk_flag"),
                    extra=meta.get("extra") or {},
                )
            )
            if len(batch) >= BATCH_SIZE:
                db.add_all(batch)
                db.flush()
                batch.clear()
        if batch:
            db.add_all(batch)
        db.flush()

        images = db.query(Image).filter(Image.project_id == project_id).all()
        summary = build_summary(images, folders=len(result.folders))
        summary["root"] = str(result.root)
        summary["scanned_entries"] = result.scanned_entries
        summary["skipped_extensions"] = result.skipped_extensions

        project = db.get(Project, project_id)
        if project:
            project.summary = summary
            project.source_path = str(result.root)
            project.status = ProjectStatus.READY

    reporter.progress(2, 1.0, "Dataset montado", force=True)
    return summary
