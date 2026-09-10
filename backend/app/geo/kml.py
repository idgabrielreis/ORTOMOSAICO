"""Importação de KML/KMZ do planejamento do voo.

O KML descreve a área que o operador pretendia levantar. Ele é referência e
material de comparação — o ortomosaico continua sendo calculado a partir das
fotografias, nunca preenchido a partir do polígono.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .crs import to_utm, utm_epsg
from .footprint import polygon_area_m2

KML_NAMESPACE = {"kml": "http://www.opengis.net/kml/2.2"}


class KmlParseError(ValueError):
    pass


def _read_kml_bytes(path: Path) -> bytes:
    """Aceita .kml direto ou .kmz (um zip com o doc.kml dentro)."""
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.lower().endswith(".kml")]
            if not names:
                raise KmlParseError("o KMZ não contém nenhum arquivo .kml")
            preferred = next((n for n in names if n.lower().endswith("doc.kml")), names[0])
            return archive.read(preferred)
    return path.read_bytes()


def _parse_coordinates(text: str) -> list[tuple[float, float]]:
    """`lon,lat[,alt]` separados por espaço ou quebra de linha."""
    points: list[tuple[float, float]] = []
    for chunk in re.split(r"\s+", text.strip()):
        if not chunk:
            continue
        parts = chunk.split(",")
        if len(parts) < 2:
            continue
        try:
            points.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return points


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_kml(path: Path | str) -> dict:
    """Devolve um FeatureCollection GeoJSON com o que o KML contém.

    Suporta Polygon (com furos), LineString e Point, em qualquer profundidade
    de pastas, que é como os softwares de planejamento costumam exportar.
    """
    path = Path(path)
    try:
        root = ElementTree.fromstring(_read_kml_bytes(path))
    except ElementTree.ParseError as exc:
        raise KmlParseError(f"KML inválido: {exc}") from exc

    features: list[dict] = []
    for placemark in root.iter():
        if _local_name(placemark.tag) != "Placemark":
            continue
        name_element = next(
            (child for child in placemark if _local_name(child.tag) == "name"), None
        )
        name = (name_element.text or "").strip() if name_element is not None else ""

        for geometry in placemark.iter():
            kind = _local_name(geometry.tag)
            if kind == "Polygon":
                rings: list[list[list[float]]] = []
                for boundary in geometry.iter():
                    if _local_name(boundary.tag) != "coordinates":
                        continue
                    ring = _parse_coordinates(boundary.text or "")
                    if len(ring) >= 3:
                        if ring[0] != ring[-1]:
                            ring.append(ring[0])
                        rings.append([[x, y] for x, y in ring])
                if rings:
                    features.append({
                        "type": "Feature",
                        "properties": {"name": name, "kind": "planning"},
                        "geometry": {"type": "Polygon", "coordinates": rings},
                    })
            elif kind in ("LineString", "Point"):
                coordinates_element = next(
                    (c for c in geometry if _local_name(c.tag) == "coordinates"), None
                )
                if coordinates_element is None:
                    continue
                points = _parse_coordinates(coordinates_element.text or "")
                if not points:
                    continue
                geometry_json = (
                    {"type": "Point", "coordinates": [points[0][0], points[0][1]]}
                    if kind == "Point"
                    else {"type": "LineString", "coordinates": [[x, y] for x, y in points]}
                )
                features.append({
                    "type": "Feature",
                    "properties": {"name": name, "kind": "planning"},
                    "geometry": geometry_json,
                })

    if not features:
        raise KmlParseError("nenhuma geometria encontrada no arquivo")
    return {"type": "FeatureCollection", "features": features}


def geojson_area_ha(collection: dict) -> float:
    """Área dos polígonos do KML, calculada em UTM local (em hectares)."""
    total_m2 = 0.0
    for feature in collection.get("features", []):
        geometry = feature.get("geometry") or {}
        if geometry.get("type") != "Polygon":
            continue
        rings = geometry.get("coordinates") or []
        if not rings:
            continue
        outer = rings[0]
        epsg = utm_epsg(outer[0][1], outer[0][0])
        projected = [to_utm(x, y, epsg) for x, y in outer]
        area = polygon_area_m2(projected)
        for hole in rings[1:]:
            area -= polygon_area_m2([to_utm(x, y, epsg) for x, y in hole])
        total_m2 += max(area, 0.0)
    return round(total_m2 / 10_000, 2)
