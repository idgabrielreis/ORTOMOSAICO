"""Interface do motor fotogramétrico.

Todo motor recebe o MESMO dataset único do voo e devolve produtos
georreferenciados. Trocar de motor não muda a API nem o frontend.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class ImageRef:
    """Imagem do dataset, já achatada: a subpasta de origem virou prefixo do nome."""

    id: str
    path: Path
    name: str
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    relative_altitude: float | None = None
    yaw: float | None = None
    pitch: float | None = None
    width: int | None = None
    height: int | None = None
    focal_length_mm: float | None = None
    sensor_width_mm: float | None = None
    band: str | None = None


@dataclass
class EngineContext:
    project_id: str
    images: list[ImageRef]
    work_dir: Path
    output_dir: Path
    options: dict = field(default_factory=dict)
    progress: Callable[[int, float, str], None] = lambda stage, frac, msg: None
    log: Callable[[str], None] = lambda line: None
    is_canceled: Callable[[], bool] = lambda: False
    output_epsg: int | None = None
    target_gsd_cm: float | None = None


@dataclass
class EngineResult:
    orthomosaic: Path | None = None
    dsm: Path | None = None
    dtm: Path | None = None
    point_cloud: Path | None = None
    report: Path | None = None
    epsg: int | None = None
    gsd_cm: float | None = None
    bounds_wgs84: list[float] | None = None
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


class EngineUnavailable(RuntimeError):
    pass


class PhotogrammetryEngine(Protocol):
    name: str
    description: str
    precision: str

    def availability(self) -> tuple[bool, str]: ...

    def run(self, ctx: EngineContext) -> EngineResult: ...
