"""Execução dos jobs: importação do voo e geração do ortomosaico."""
from __future__ import annotations

import json
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings
from ..db import session_scope
from ..ingest.importer import import_flight
from ..models import Image, Job, JobStatus, Product, Project, ProjectStatus
from ..utils.images import make_thumbnail
from .engines import EngineContext, EngineUnavailable, ImageRef, resolve_engine
from .reporter import JobReporter


def _job_paths(project_id: str, job_id: str) -> tuple[Path, Path, Path]:
    project_dir = settings.project_dir(project_id)
    work_dir = project_dir / "work" / job_id
    output_dir = project_dir / "products" / job_id
    log_path = project_dir / "logs" / f"{job_id}.log"
    return work_dir, output_dir, log_path


def _finish(job_id: str, status: JobStatus, error: str | None = None,
            warnings: list[str] | None = None) -> None:
    with session_scope() as db:
        job = db.get(Job, job_id)
        if job is None:
            return
        job.status = status
        job.error = error
        if warnings:
            job.warnings = warnings
        job.finished_at = datetime.now(timezone.utc)
        if status == JobStatus.SUCCEEDED:
            job.progress = 100.0
            job.stage = 8
            job.stage_label = "Concluído"
        project = db.get(Project, job.project_id)
        if project and job.kind == "orthomosaic":
            project.status = (
                ProjectStatus.COMPLETED if status == JobStatus.SUCCEEDED else ProjectStatus.FAILED
            )


def _start(job_id: str) -> tuple[str, str, dict]:
    with session_scope() as db:
        job = db.get(Job, job_id)
        if job is None:
            raise LookupError(f"job {job_id} não existe")
        job.status = JobStatus.RUNNING
        job.started_at = datetime.now(timezone.utc)
        project = db.get(Project, job.project_id)
        if project:
            project.status = (
                ProjectStatus.SCANNING if job.kind == "scan" else ProjectStatus.PROCESSING
            )
        return job.project_id, job.engine, dict(job.options or {})


def run_scan_job(job_id: str) -> None:
    project_id, _, options = _start(job_id)
    _, _, log_path = _job_paths(project_id, job_id)
    reporter = JobReporter(job_id, log_path)
    try:
        roots = options.get("roots") or ([options["root"]] if options.get("root") else [])
        if not roots:
            raise ValueError("nenhuma pasta de imagens informada")
        import_flight(project_id, roots, reporter)
        _finish(job_id, JobStatus.SUCCEEDED)
        with session_scope() as db:
            project = db.get(Project, project_id)
            if project:
                project.status = ProjectStatus.READY
    except InterruptedError:
        reporter.log("cancelado")
        _finish(job_id, JobStatus.CANCELED, "cancelado pelo usuário")
    except Exception as exc:
        reporter.log(traceback.format_exc())
        _finish(job_id, JobStatus.FAILED, f"{type(exc).__name__}: {exc}")
    finally:
        reporter.close()


def _flatten_dataset(images: list[Image], target_dir: Path, log) -> list[ImageRef]:
    """Materializa TODAS as subpastas em um único diretório plano.

    É aqui que "várias pastas = um voo" vira realidade para o motor: a
    hierarquia some, o nome da subpasta vira prefixo (evita colisão de
    DJI_0001.JPG repetido em cartões diferentes) e o motor recebe um dataset só.
    Usa hard link quando possível: custo zero de disco e de tempo.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    refs: list[ImageRef] = []
    used: set[str] = set()
    for img in images:
        prefix = img.folder.replace("/", "_").replace(".", "").strip("_")
        name = f"{prefix}__{img.filename}" if prefix else img.filename
        base, ext = os.path.splitext(name)
        counter = 1
        while name in used:
            name = f"{base}_{counter}{ext}"
            counter += 1
        used.add(name)

        destination = target_dir / name
        source = Path(img.path)
        if not destination.exists():
            try:
                os.link(source, destination)
            except OSError:
                try:
                    destination.symlink_to(source)
                except OSError:
                    import shutil

                    shutil.copy2(source, destination)
        refs.append(
            ImageRef(
                id=img.id, path=destination, name=name,
                latitude=img.latitude, longitude=img.longitude, altitude=img.altitude,
                relative_altitude=img.relative_altitude, yaw=img.yaw, pitch=img.pitch,
                width=img.width, height=img.height, focal_length_mm=img.focal_length_mm,
                sensor_width_mm=img.sensor_width_mm, band=img.band,
            )
        )
    log(f"dataset achatado: {len(refs)} imagens em {target_dir}")
    return refs


def run_orthomosaic_job(job_id: str) -> None:
    project_id, engine_name, options = _start(job_id)
    work_dir, output_dir, log_path = _job_paths(project_id, job_id)
    reporter = JobReporter(job_id, log_path)
    warnings: list[str] = []
    try:
        with session_scope() as db:
            project = db.get(Project, project_id)
            if project is None:
                raise LookupError("projeto não encontrado")
            all_images = db.query(Image).filter(Image.project_id == project_id).all()
            valid = [i for i in all_images if i.is_valid and not i.is_duplicate]
            output_epsg = project.output_epsg
            target_gsd = project.target_gsd_cm
            options.setdefault("quality", project.quality or "alta")

        total, usable = len(all_images), len(valid)
        if usable < settings.min_valid_images:
            raise ValueError(
                f"apenas {usable} imagens válidas; o mínimo é {settings.min_valid_images}"
            )
        ratio = usable / total if total else 0
        if ratio < settings.min_valid_ratio:
            raise ValueError(
                f"somente {ratio:.0%} das imagens são válidas (mínimo {settings.min_valid_ratio:.0%})"
            )
        invalid = sum(1 for i in all_images if not i.is_valid)
        duplicates = sum(1 for i in all_images if i.is_duplicate)
        ignored = total - usable
        # Erro em algumas imagens não interrompe o voo inteiro.
        if invalid:
            warnings.append(f"{invalid} imagens foram ignoradas devido a problemas nos arquivos")
        if duplicates:
            warnings.append(f"{duplicates} imagens duplicadas entraram no dataset uma única vez")
        for warning in warnings:
            reporter.log(warning)

        reporter.progress(1, 0.2, f"Preparando {usable} imagens do voo",
                          images_total=usable, force=True)
        refs = _flatten_dataset(valid, work_dir / "project" / "images", reporter.log)

        engine = resolve_engine(engine_name)
        with session_scope() as db:
            job = db.get(Job, job_id)
            if job:
                job.engine = engine.name
        reporter.log(f"motor: {engine.name} ({engine.description})")

        ctx = EngineContext(
            project_id=project_id, images=refs, work_dir=work_dir, output_dir=output_dir,
            options=options, progress=lambda s, f, m="": reporter.progress(s, f, m),
            log=reporter.log, is_canceled=reporter.is_canceled,
            output_epsg=output_epsg, target_gsd_cm=target_gsd,
        )
        result = engine.run(ctx)
        warnings.extend(result.warnings)

        reporter.progress(8, 0.7, "Registrando produtos", force=True)
        with session_scope() as db:
            for kind, path in (
                ("orthomosaic", result.orthomosaic), ("dsm", result.dsm), ("dtm", result.dtm),
                ("pointcloud", result.point_cloud), ("report", result.report),
            ):
                if path and Path(path).exists():
                    db.add(Product(
                        project_id=project_id, job_id=job_id, kind=kind, path=str(path),
                        epsg=result.epsg, gsd_cm=result.gsd_cm,
                        bounds_wgs84=result.bounds_wgs84,
                        size_bytes=Path(path).stat().st_size, engine=engine.name,
                    ))
            project = db.get(Project, project_id)
            if project:
                summary = dict(project.summary or {})
                summary["last_result"] = {
                    "engine": engine.name, "epsg": result.epsg, "gsd_cm": result.gsd_cm,
                    "bounds_wgs84": result.bounds_wgs84, "stats": result.stats,
                    "warnings": warnings,
                }
                project.summary = summary

        if result.orthomosaic:
            make_thumbnail(result.orthomosaic, output_dir / "preview.jpg", size=512)
        (output_dir / "report.json").write_text(
            json.dumps({
                "project_id": project_id, "job_id": job_id, "engine": engine.name,
                "images_total": total, "images_used": usable, "images_ignored": ignored,
                "epsg": result.epsg, "gsd_cm": result.gsd_cm,
                "bounds_wgs84": result.bounds_wgs84, "stats": result.stats,
                "warnings": warnings,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        _finish(job_id, JobStatus.SUCCEEDED, warnings=warnings)
        reporter.log("job concluído")
    except InterruptedError:
        reporter.log("cancelado")
        _finish(job_id, JobStatus.CANCELED, "cancelado pelo usuário", warnings)
    except EngineUnavailable as exc:
        reporter.log(f"motor indisponível: {exc}")
        _finish(job_id, JobStatus.FAILED, str(exc), warnings)
    except Exception as exc:
        reporter.log(traceback.format_exc())
        _finish(job_id, JobStatus.FAILED, f"{type(exc).__name__}: {exc}", warnings)
    finally:
        reporter.close()


def run_job(job_id: str, kind: str) -> None:
    if kind == "scan":
        run_scan_job(job_id)
    else:
        run_orthomosaic_job(job_id)
