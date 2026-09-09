"""Contratos de entrada e saída da API."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    source_path: str | None = None
    output_epsg: int | None = None
    target_gsd_cm: float | None = Field(default=None, gt=0, le=100)


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    output_epsg: int | None = None
    target_gsd_cm: float | None = None


class ProjectOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    status: str
    source_path: str | None
    source_kind: str
    output_epsg: int | None
    target_gsd_cm: float | None
    summary: dict
    created_at: datetime
    updated_at: datetime


class ScanRequest(BaseModel):
    path: str = Field(description="Pasta raiz do voo no servidor; subpastas entram no mesmo dataset")


class JobCreate(BaseModel):
    engine: str = "auto"
    quality: str = "medium"
    fast_orthophoto: bool = True
    multispectral: bool = False
    options: dict = Field(default_factory=dict)


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    kind: str
    engine: str
    status: str
    stage: int
    stage_label: str
    progress: float
    images_done: int
    images_total: int
    warnings: list
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ImageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    relative_path: str
    folder: str
    filename: str
    size_bytes: int
    is_valid: bool
    invalid_reason: str | None
    is_duplicate: bool
    width: int | None
    height: int | None
    latitude: float | None
    longitude: float | None
    altitude: float | None
    relative_altitude: float | None
    captured_at: datetime | None
    yaw: float | None
    camera_model: str | None
    band: str | None


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    epsg: int | None
    gsd_cm: float | None
    bounds_wgs84: list | None
    size_bytes: int
    engine: str
    created_at: datetime
