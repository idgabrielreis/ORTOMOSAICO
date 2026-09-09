"""Tiles XYZ servidos direto do GeoTIFF.

O mapa nunca baixa o ortomosaico inteiro: o rio-tiler lê apenas a janela do
tile pedido, aproveitando os overviews internos do arquivo.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Product

router = APIRouter(prefix="/api/projects", tags=["tiles"])


def _orthomosaic_path(db: Session, project_id: str) -> Path:
    product = db.scalars(
        select(Product)
        .where(Product.project_id == project_id, Product.kind == "orthomosaic")
        .order_by(Product.created_at.desc())
    ).first()
    if product is None or not Path(product.path).exists():
        raise HTTPException(404, "nenhum ortomosaico disponível para este projeto")
    return Path(product.path)


@router.get("/{project_id}/tiles/{z}/{x}/{y}.png")
def get_tile(project_id: str, z: int, x: int, y: int, db: Session = Depends(get_db)) -> Response:
    from rio_tiler.errors import TileOutsideBounds
    from rio_tiler.io import Reader

    path = _orthomosaic_path(db, project_id)
    try:
        with Reader(str(path)) as reader:
            image = reader.tile(x, y, z, tilesize=256)
            content = image.render(img_format="PNG", add_mask=True)
    except TileOutsideBounds:
        return Response(status_code=204)
    return Response(content, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/{project_id}/tilejson.json")
def tilejson(project_id: str, db: Session = Depends(get_db)) -> dict:
    from rio_tiler.io import Reader

    path = _orthomosaic_path(db, project_id)
    with Reader(str(path)) as reader:
        bounds = list(reader.get_geographic_bounds("EPSG:4326"))
        min_zoom, max_zoom = reader.minzoom, reader.maxzoom
    return {
        "tilejson": "2.2.0",
        "name": f"ortomosaico-{project_id}",
        "scheme": "xyz",
        "tiles": [f"/api/projects/{project_id}/tiles/{{z}}/{{x}}/{{y}}.png"],
        "bounds": bounds,
        "minzoom": min_zoom,
        "maxzoom": max_zoom,
        "center": [(bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2, min_zoom + 2],
    }
