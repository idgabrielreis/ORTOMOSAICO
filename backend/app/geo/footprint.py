"""Geometria de tomada: GSD, footprint no solo e área do voo.

Modelo pinhole com terreno plano na altitude média do voo. É aproximação
suficiente para resumo do dataset, visualização de footprints e para o motor
`direct`; o produto de precisão sai do motor fotogramétrico, que usa DSM real.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .crs import to_utm, utm_epsg


@dataclass
class CameraGeometry:
    width_px: int
    height_px: int
    focal_length_mm: float
    sensor_width_mm: float

    @property
    def sensor_height_mm(self) -> float:
        return self.sensor_width_mm * self.height_px / self.width_px

    def gsd_m(self, altitude_m: float) -> float:
        """Tamanho do pixel no solo, em metros."""
        return altitude_m * self.sensor_width_mm / (self.focal_length_mm * self.width_px)

    def ground_size_m(self, altitude_m: float) -> tuple[float, float]:
        gsd = self.gsd_m(altitude_m)
        return gsd * self.width_px, gsd * self.height_px


def footprint_corners(
    geometry: CameraGeometry,
    longitude: float,
    latitude: float,
    altitude_m: float,
    yaw_deg: float | None = None,
    epsg: int | None = None,
) -> list[tuple[float, float]]:
    """Cantos do footprint em UTM, no sentido horário a partir do superior esquerdo.

    Assume nadir (gimbal a -90°). `yaw` gira a imagem em torno do centro.
    """
    epsg = epsg or utm_epsg(latitude, longitude)
    cx, cy = to_utm(longitude, latitude, epsg)
    gw, gh = geometry.ground_size_m(altitude_m)
    hw, hh = gw / 2, gh / 2
    corners = [(-hw, hh), (hw, hh), (hw, -hh), (-hw, -hh)]
    theta = math.radians(yaw_deg or 0.0)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    out = []
    for dx, dy in corners:
        # Yaw da DJI é azimute (horário a partir do norte); em UTM o eixo Y é o norte.
        rx = dx * cos_t + dy * sin_t
        ry = -dx * sin_t + dy * cos_t
        out.append((cx + rx, cy + ry))
    return out


def convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Envoltória convexa (monotone chain). Evita depender do shapely aqui."""
    pts = sorted(set(points))
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    upper: list[tuple[float, float]] = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def polygon_area_m2(points: list[tuple[float, float]]) -> float:
    """Área por fórmula do shoelace. Entrada em metros (UTM)."""
    if len(points) < 3:
        return 0.0
    total = 0.0
    for i, (x1, y1) in enumerate(points):
        x2, y2 = points[(i + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2
