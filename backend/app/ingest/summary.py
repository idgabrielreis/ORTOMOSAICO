"""Resumo do dataset apresentado antes de processar o voo."""
from __future__ import annotations

import statistics
from collections import Counter
from typing import Iterable

from ..geo.crs import from_utm, utm_epsg, utm_zone_label
from ..geo.footprint import (
    CameraGeometry,
    convex_hull,
    footprint_corners,
    polygon_area_m2,
)
from ..models import Image


def _camera_geometry(img: Image) -> CameraGeometry | None:
    if not (img.width and img.height and img.focal_length_mm and img.sensor_width_mm):
        return None
    return CameraGeometry(img.width, img.height, img.focal_length_mm, img.sensor_width_mm)


def build_summary(images: Iterable[Image], *, folders: int | None = None) -> dict:
    images = list(images)
    valid = [i for i in images if i.is_valid and not i.is_duplicate]
    with_gps = [i for i in valid if i.has_gps]

    folder_names = {i.folder for i in images if i.folder}
    altitudes = [i.relative_altitude for i in valid if i.relative_altitude is not None]
    if not altitudes:
        abs_alt = [i.altitude for i in valid if i.altitude is not None]
        # Sem altitude relativa: aproxima pela diferença para o menor valor absoluto,
        # que costuma ser o ponto de decolagem.
        altitudes = [a - min(abs_alt) for a in abs_alt] if len(abs_alt) > 1 else []

    mean_altitude = round(statistics.fmean(altitudes), 1) if altitudes else None
    cameras = Counter(i.camera_model for i in valid if i.camera_model)
    bands = Counter(i.band for i in valid if i.band)
    resolutions = Counter(f"{i.width}x{i.height}" for i in valid if i.width and i.height)
    timestamps = sorted(i.captured_at for i in valid if i.captured_at)

    summary: dict = {
        "total_files": len(images),
        "folders": folders if folders is not None else len(folder_names),
        "folder_names": sorted(folder_names)[:50],
        "valid_images": len(valid),
        "invalid_images": sum(1 for i in images if not i.is_valid),
        "duplicate_images": sum(1 for i in images if i.is_duplicate),
        "images_with_gps": len(with_gps),
        "images_without_gps": len(valid) - len(with_gps),
        "mean_relative_altitude_m": mean_altitude,
        "cameras": [{"model": m, "count": c} for m, c in cameras.most_common()],
        "bands": [{"band": b, "count": c} for b, c in bands.most_common()],
        "resolutions": [{"size": r, "count": c} for r, c in resolutions.most_common(5)],
        "captured_from": timestamps[0].isoformat() if timestamps else None,
        "captured_to": timestamps[-1].isoformat() if timestamps else None,
        "invalid_samples": [
            {"file": i.relative_path, "reason": i.invalid_reason}
            for i in images if not i.is_valid
        ][:20],
        "duplicate_samples": [
            {"file": i.relative_path, "duplicate_of": i.duplicate_of}
            for i in images if i.is_duplicate
        ][:20],
    }

    if not with_gps:
        summary.update({"area_ha": None, "gsd_cm": None, "bounds_wgs84": None, "epsg": None})
        return summary

    lat0 = statistics.fmean(i.latitude for i in with_gps)
    lon0 = statistics.fmean(i.longitude for i in with_gps)
    epsg = utm_epsg(lat0, lon0)

    points: list[tuple[float, float]] = []
    gsds: list[float] = []
    for img in with_gps:
        geometry = _camera_geometry(img)
        altitude = img.relative_altitude or mean_altitude
        if geometry and altitude:
            points.extend(
                footprint_corners(geometry, img.longitude, img.latitude, altitude, img.yaw, epsg)
            )
            gsds.append(geometry.gsd_m(altitude))
        else:
            from ..geo.crs import to_utm

            points.append(to_utm(img.longitude, img.latitude, epsg))

    hull = convex_hull(points)
    area_m2 = polygon_area_m2(hull)
    lons_lats = [from_utm(x, y, epsg) for x, y in hull]
    lons = [p[0] for p in lons_lats]
    lats = [p[1] for p in lons_lats]

    summary.update({
        "area_ha": round(area_m2 / 10_000, 1) if area_m2 else None,
        "gsd_cm": round(statistics.fmean(gsds) * 100, 2) if gsds else None,
        "epsg": epsg,
        "utm_zone": utm_zone_label(epsg),
        "center": [round(lon0, 6), round(lat0, 6)],
        "bounds_wgs84": [min(lons), min(lats), max(lons), max(lats)],
        "hull_wgs84": [[round(x, 6), round(y, 6)] for x, y in lons_lats],
    })
    return summary
