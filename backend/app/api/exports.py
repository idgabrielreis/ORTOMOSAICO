"""Exportação dos produtos."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..geo.export import export_kmz, export_png, export_worldfile, read_info
from .tiles import _orthomosaic_path

router = APIRouter(prefix="/api/projects", tags=["exports"])

FORMATS = {"geotiff", "png", "kmz", "worldfile", "report"}


@router.get("/{project_id}/exports/info")
def raster_info(project_id: str, db: Session = Depends(get_db)) -> dict:
    return read_info(_orthomosaic_path(db, project_id))


@router.get("/{project_id}/exports/{fmt}")
def export(
    project_id: str,
    fmt: str,
    db: Session = Depends(get_db),
    max_size: int = Query(4096, ge=256, le=16384),
) -> FileResponse:
    if fmt not in FORMATS:
        raise HTTPException(400, f"formato inválido; use um de {sorted(FORMATS)}")
    ortho = _orthomosaic_path(db, project_id)
    exports_dir = ortho.parent / "exports"
    exports_dir.mkdir(parents=True, exist_ok=True)

    if fmt == "geotiff":
        return FileResponse(ortho, media_type="image/tiff", filename=f"{project_id}_ortho.tif")
    if fmt == "report":
        report = ortho.parent / "report.json"
        if not report.exists():
            raise HTTPException(404, "relatório indisponível")
        return FileResponse(report, media_type="application/json",
                            filename=f"{project_id}_relatorio.json")
    if fmt == "png":
        destination = exports_dir / f"ortho_{max_size}.png"
        if not destination.exists():
            export_png(ortho, destination, max_size=max_size)
        return FileResponse(destination, media_type="image/png",
                            filename=f"{project_id}_ortho.png")
    if fmt == "worldfile":
        destination = exports_dir / "ortho.tfw"
        export_worldfile(ortho, destination)
        return FileResponse(destination, media_type="text/plain",
                            filename=f"{project_id}_ortho.tfw")

    destination = exports_dir / "ortho.kmz"
    if not destination.exists():
        export_kmz(ortho, destination, name=f"Ortomosaico {project_id[:8]}")
    return FileResponse(destination, media_type="application/vnd.google-earth.kmz",
                        filename=f"{project_id}_ortho.kmz")


@router.get("/{project_id}/products/{product_id}/download")
def download_product(project_id: str, product_id: str, db: Session = Depends(get_db)):
    from ..models import Product

    product = db.get(Product, product_id)
    if product is None or product.project_id != project_id:
        raise HTTPException(404, "produto não encontrado")
    path = Path(product.path)
    if not path.exists():
        raise HTTPException(404, "arquivo do produto não está mais em disco")
    return FileResponse(path, filename=path.name)
