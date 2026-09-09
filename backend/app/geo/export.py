"""Exportações derivadas do ortomosaico.

O GeoTIFF é a fonte da verdade: CRS e transform vêm dele, e todo o resto
(PNG, World File, KMZ) é derivado sem perder georreferenciamento.
"""
from __future__ import annotations

from pathlib import Path


def read_info(path: Path) -> dict:
    import rasterio
    from rasterio.warp import transform_bounds

    with rasterio.open(path) as src:
        epsg = src.crs.to_epsg() if src.crs else None
        return {
            "path": str(path),
            "width": src.width,
            "height": src.height,
            "bands": src.count,
            "epsg": epsg,
            "crs": str(src.crs),
            "gsd_m": abs(src.transform.a),
            "bounds": list(src.bounds),
            "bounds_wgs84": list(
                transform_bounds(src.crs, "EPSG:4326", *src.bounds, densify_pts=21)
            ) if src.crs else None,
            "nodata": src.nodata,
        }


def export_png(source: Path, destination: Path, max_size: int = 4096) -> Path:
    """Versão de visualização, reamostrada. Não substitui o GeoTIFF."""
    import numpy as np
    import rasterio
    from PIL import Image
    from rasterio.enums import Resampling

    with rasterio.open(source) as src:
        scale = min(1.0, max_size / max(src.width, src.height))
        out_w, out_h = max(1, int(src.width * scale)), max(1, int(src.height * scale))
        count = min(src.count, 4)
        data = src.read(
            indexes=list(range(1, count + 1)),
            out_shape=(count, out_h, out_w),
            resampling=Resampling.average,
        )
    array = np.moveaxis(data, 0, -1)
    if array.dtype != np.uint8:
        finite = array[np.isfinite(array)]
        hi = float(finite.max()) if finite.size else 1.0
        array = (np.clip(array / (hi or 1.0), 0, 1) * 255).astype(np.uint8)
    mode = {1: "L", 3: "RGB", 4: "RGBA"}.get(array.shape[-1], "RGB")
    if mode == "L":
        array = array[..., 0]
    destination.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(array, mode=mode).save(destination)
    return destination


def export_worldfile(source: Path, destination: Path) -> Path:
    """World File (.pgw/.tfw): mesma transformação afim, em texto."""
    import rasterio

    with rasterio.open(source) as src:
        t = src.transform
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        "\n".join(
            f"{value:.10f}" for value in (
                t.a, t.d, t.b, t.e, t.c + t.a / 2, t.f + t.e / 2
            )
        ) + "\n",
        encoding="utf-8",
    )
    return destination


def export_kmz(source: Path, destination: Path, name: str = "Ortomosaico") -> Path:
    """KMZ com GroundOverlay reprojetado para WGS84 (exigência do KML)."""
    import numpy as np
    import rasterio
    import simplekml
    from PIL import Image
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    with rasterio.open(source) as src:
        transform, width, height = calculate_default_transform(
            src.crs, "EPSG:4326", src.width, src.height, *src.bounds
        )
        scale = min(1.0, 4096 / max(width, height))
        width, height = max(1, int(width * scale)), max(1, int(height * scale))
        transform = transform * transform.scale(
            (src.width / width) if width else 1, (src.height / height) if height else 1
        )
        count = min(src.count, 4)
        destination_array = np.zeros((count, height, width), dtype=np.uint8)
        for band in range(count):
            reproject(
                source=rasterio.band(src, band + 1),
                destination=destination_array[band],
                src_transform=src.transform, src_crs=src.crs,
                dst_transform=transform, dst_crs="EPSG:4326",
                resampling=Resampling.average,
            )
    west = transform.c
    north = transform.f
    east = west + transform.a * width
    south = north + transform.e * height

    array = np.moveaxis(destination_array, 0, -1)
    if array.shape[-1] == 3:
        array = np.dstack([array, np.full(array.shape[:2], 255, dtype=np.uint8)])
    image = Image.fromarray(array[..., :4], mode="RGBA")
    destination.parent.mkdir(parents=True, exist_ok=True)
    png_path = destination.with_suffix(".overlay.png")
    image.save(png_path)

    kml = simplekml.Kml(name=name)
    overlay = kml.newgroundoverlay(name=name)
    # addfile embute o PNG dentro do KMZ e devolve o caminho interno do arquivo.
    overlay.icon.href = kml.addfile(str(png_path))
    overlay.latlonbox.north = north
    overlay.latlonbox.south = south
    overlay.latlonbox.east = east
    overlay.latlonbox.west = west
    kml.savekmz(str(destination))
    png_path.unlink(missing_ok=True)
    return destination
