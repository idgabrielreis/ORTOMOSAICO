"""Caminho completo: pasta do voo -> dataset único -> GeoTIFF georreferenciado."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.db import session_scope
from app.models import Image, Job, JobStatus, Product, Project
from app.processing.pipeline import run_orthomosaic_job, run_scan_job


@pytest.fixture()
def projeto_importado(db_session, flight_dir: Path) -> str:
    with session_scope() as db:
        project = Project(name="Voo de teste")
        db.add(project)
        db.flush()
        job = Job(project_id=project.id, kind="scan", options={"root": str(flight_dir)})
        db.add(job)
        db.flush()
        project_id, job_id = project.id, job.id

    run_scan_job(job_id)
    return project_id


def test_importacao_cria_um_unico_dataset(projeto_importado: str):
    with session_scope() as db:
        project = db.get(Project, projeto_importado)
        images = db.query(Image).filter(Image.project_id == projeto_importado).all()
        summary = project.summary

    pastas = {i.folder for i in images}
    assert len(pastas) >= 3, "as imagens vieram de várias subpastas"
    # ... e mesmo assim pertencem todas ao mesmo projeto.
    assert {i.project_id for i in images} == {projeto_importado}
    assert summary["valid_images"] == 6
    assert summary["images_with_gps"] == 6
    assert summary["folders"] >= 3
    assert summary["area_ha"] > 0
    assert summary["epsg"] and 32700 <= summary["epsg"] < 32800  # UTM sul
    assert summary["mean_relative_altitude_m"] == 110.0


def test_ortomosaico_gerado_e_georreferenciado(projeto_importado: str):
    rasterio = pytest.importorskip("rasterio")

    with session_scope() as db:
        job = Job(project_id=projeto_importado, kind="orthomosaic", engine="direct")
        db.add(job)
        db.flush()
        job_id = job.id

    run_orthomosaic_job(job_id)

    with session_scope() as db:
        job = db.get(Job, job_id)
        product = (
            db.query(Product)
            .filter(Product.project_id == projeto_importado, Product.kind == "orthomosaic")
            .first()
        )
        assert job.status == JobStatus.SUCCEEDED, job.error
        assert job.progress == 100.0
        assert product is not None

        with rasterio.open(product.path) as src:
            assert src.crs is not None and src.crs.to_epsg() == product.epsg
            assert src.count == 4                      # RGB + alfa
            assert abs(src.transform.a) > 0            # transformação afim válida
            assert src.width > 200 and src.height > 100
            # A área coberta precisa bater com a extensão do voo, não com uma
            # imagem isolada.
            largura_m = src.width * abs(src.transform.a)
            assert largura_m > 100


def test_avisos_listam_arquivos_ignorados(projeto_importado: str):
    """Imagens ruins não interrompem o voo: elas viram aviso no job."""
    with session_scope() as db:
        job = Job(project_id=projeto_importado, kind="orthomosaic", engine="direct")
        db.add(job)
        db.flush()
        job_id = job.id

    run_orthomosaic_job(job_id)

    with session_scope() as db:
        job = db.get(Job, job_id)

    assert job.status == JobStatus.SUCCEEDED, job.error
    assert any("problemas nos arquivos" in w for w in job.warnings)
    assert any("duplicadas" in w for w in job.warnings)
