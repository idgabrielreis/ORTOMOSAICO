"""Sistemas de coordenadas.

O voo é medido em metros, então o processamento e o produto final usam UTM
local (derivado do centroide do voo) por padrão. WGS84 fica para exibição e
troca de dados.
"""
from __future__ import annotations

from functools import lru_cache

from pyproj import CRS, Transformer

WGS84 = 4326
WEB_MERCATOR = 3857


def utm_epsg(latitude: float, longitude: float) -> int:
    """EPSG da zona UTM que contém o ponto (326xx norte, 327xx sul)."""
    zone = int((longitude + 180) // 6) + 1
    zone = min(max(zone, 1), 60)
    return (32600 if latitude >= 0 else 32700) + zone


def utm_zone_label(epsg: int) -> str:
    zone = epsg % 100
    hemisphere = "N" if 32600 <= epsg < 32700 else "S"
    return f"UTM {zone}{hemisphere}"


@lru_cache(maxsize=64)
def transformer(from_epsg: int, to_epsg: int) -> Transformer:
    return Transformer.from_crs(CRS.from_epsg(from_epsg), CRS.from_epsg(to_epsg), always_xy=True)


def to_utm(longitude: float, latitude: float, epsg: int) -> tuple[float, float]:
    return transformer(WGS84, epsg).transform(longitude, latitude)


def from_utm(x: float, y: float, epsg: int) -> tuple[float, float]:
    return transformer(epsg, WGS84).transform(x, y)


def describe(epsg: int) -> dict:
    crs = CRS.from_epsg(epsg)
    return {
        "epsg": epsg,
        "name": crs.name,
        "units": crs.axis_info[0].unit_name if crs.axis_info else None,
        "is_projected": crs.is_projected,
    }
