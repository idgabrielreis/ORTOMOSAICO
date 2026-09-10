"""Modelo de dados.

Hierarquia do domínio:

    Fazenda -> Voo (Project) -> dataset de fotografias -> processamento -> ortomosaico

Regra central: um voo é UM dataset e gera UM ortomosaico.
`Image.relative_path` guarda a subpasta de origem apenas como informação de
diagnóstico e relatório; ela nunca agrupa nem separa o processamento. Nenhuma
entidade abaixo do voo divide o processamento — talhões, quando existirem,
serão camada GIS desenhada sobre o ortomosaico pronto.

O KML do planejamento é referência geográfica (`PlanningArea`), nunca insumo do
ortomosaico: o produto sai das fotografias.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ProjectStatus(StrEnum):
    CREATED = "created"
    SCANNING = "scanning"
    READY = "ready"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class PositionSource(StrEnum):
    """Origem da posição de cada fotografia, em ordem crescente de precisão."""

    NONE = "none"
    EXIF_GPS = "exif_gps"      # GPS de navegação gravado no EXIF
    RTK = "rtk"                # correção em tempo real, marcada no XMP da DJI
    PPK = "ppk"                # pós-processada contra base GNSS
    GCP_ADJUSTED = "gcp"       # ajustada no bundle adjustment com pontos de controle


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


class Farm(Base):
    """Fazenda: agrupa voos. Não participa do processamento."""

    __tablename__ = "farms"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    flights: Mapped[list["Project"]] = relationship(
        back_populates="farm", cascade="all, delete-orphan", order_by="Project.created_at.desc()"
    )


class Project(Base):
    """Um voo. É a unidade de processamento: um dataset, um ortomosaico."""

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    farm_id: Mapped[str | None] = mapped_column(
        ForeignKey("farms.id", ondelete="SET NULL"), default=None, index=True
    )
    flight_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default=ProjectStatus.CREATED)

    source_path: Mapped[str | None] = mapped_column(Text, default=None)
    source_kind: Mapped[str] = mapped_column(String(20), default="server")  # server | upload

    # CRS de saída escolhido pelo usuário (None = derivado do centroide do voo)
    output_epsg: Mapped[int | None] = mapped_column(Integer, default=None)
    target_gsd_cm: Mapped[float | None] = mapped_column(Float, default=None)

    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    farm: Mapped["Farm | None"] = relationship(back_populates="flights")
    images: Mapped[list["Image"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="selectin"
    )
    planning_areas: Mapped[list["PlanningArea"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    jobs: Mapped[list["Job"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="Job.created_at.desc()"
    )
    products: Mapped[list["Product"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Image(Base):
    """Uma imagem do voo. Pertence ao projeto, nunca à subpasta."""

    __tablename__ = "images"
    __table_args__ = (
        Index("ix_images_project_valid", "project_id", "is_valid"),
        Index("ix_images_project_digest", "project_id", "digest"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )

    path: Mapped[str] = mapped_column(Text)               # caminho absoluto
    relative_path: Mapped[str] = mapped_column(Text)      # ex.: CAMERA_01/DJI_0001.JPG
    folder: Mapped[str] = mapped_column(Text, default="") # subpasta de origem (informativo)
    filename: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    digest: Mapped[str | None] = mapped_column(String(64), default=None)

    is_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    invalid_reason: Mapped[str | None] = mapped_column(Text, default=None)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    duplicate_of: Mapped[str | None] = mapped_column(String(32), default=None)

    width: Mapped[int | None] = mapped_column(Integer, default=None)
    height: Mapped[int | None] = mapped_column(Integer, default=None)
    latitude: Mapped[float | None] = mapped_column(Float, default=None)
    longitude: Mapped[float | None] = mapped_column(Float, default=None)
    altitude: Mapped[float | None] = mapped_column(Float, default=None)          # absoluta (m)
    relative_altitude: Mapped[float | None] = mapped_column(Float, default=None) # acima do takeoff
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    yaw: Mapped[float | None] = mapped_column(Float, default=None)
    pitch: Mapped[float | None] = mapped_column(Float, default=None)
    roll: Mapped[float | None] = mapped_column(Float, default=None)

    camera_make: Mapped[str | None] = mapped_column(String(120), default=None)
    camera_model: Mapped[str | None] = mapped_column(String(120), default=None)
    focal_length_mm: Mapped[float | None] = mapped_column(Float, default=None)
    focal_35mm: Mapped[float | None] = mapped_column(Float, default=None)
    sensor_width_mm: Mapped[float | None] = mapped_column(Float, default=None)
    band: Mapped[str | None] = mapped_column(String(32), default=None)  # RGB, NIR, RedEdge...
    rtk_flag: Mapped[str | None] = mapped_column(String(32), default=None)

    # Posicionamento: o GPS de navegação é apenas a fonte mais fraca. RTK vem
    # marcado no XMP; PPK chega depois, por importação das posições corrigidas.
    position_source: Mapped[str] = mapped_column(String(16), default=PositionSource.NONE)
    horizontal_accuracy_m: Mapped[float | None] = mapped_column(Float, default=None)
    vertical_accuracy_m: Mapped[float | None] = mapped_column(Float, default=None)
    original_latitude: Mapped[float | None] = mapped_column(Float, default=None)
    original_longitude: Mapped[float | None] = mapped_column(Float, default=None)
    original_altitude: Mapped[float | None] = mapped_column(Float, default=None)

    extra: Mapped[dict] = mapped_column(JSON, default=dict)  # XMP bruto relevante

    project: Mapped[Project] = relationship(back_populates="images")

    @property
    def has_gps(self) -> bool:
        return self.latitude is not None and self.longitude is not None


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(20), default="orthomosaic")
    engine: Mapped[str] = mapped_column(String(20), default="direct")
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.QUEUED)

    stage: Mapped[int] = mapped_column(Integer, default=0)      # 0..8
    stage_label: Mapped[str] = mapped_column(String(120), default="")
    progress: Mapped[float] = mapped_column(Float, default=0.0)  # 0..100
    images_done: Mapped[int] = mapped_column(Integer, default=0)
    images_total: Mapped[int] = mapped_column(Integer, default=0)

    options: Mapped[dict] = mapped_column(JSON, default=dict)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    project: Mapped[Project] = relationship(back_populates="jobs")


class Product(Base):
    """Saída de um job: ortomosaico, DSM, nuvem de pontos, relatório."""

    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[str | None] = mapped_column(String(32), default=None)
    kind: Mapped[str] = mapped_column(String(30))  # orthomosaic | dsm | dtm | pointcloud | report
    path: Mapped[str] = mapped_column(Text)
    epsg: Mapped[int | None] = mapped_column(Integer, default=None)
    gsd_cm: Mapped[float | None] = mapped_column(Float, default=None)
    bounds_wgs84: Mapped[list | None] = mapped_column(JSON, default=None)  # [w, s, e, n]
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    engine: Mapped[str] = mapped_column(String(20), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    project: Mapped[Project] = relationship(back_populates="products")


class PlanningArea(Base):
    """Área planejada do voo, importada de KML/KMZ.

    Referência de planejamento e comparação com o que foi realmente fotografado.
    Nunca entra no cálculo do ortomosaico.
    """

    __tablename__ = "planning_areas"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), default="Planejamento")
    source_file: Mapped[str] = mapped_column(Text, default="")
    geojson: Mapped[dict] = mapped_column(JSON, default=dict)
    area_ha: Mapped[float | None] = mapped_column(Float, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    project: Mapped[Project] = relationship(back_populates="planning_areas")


class GnssDataset(Base):
    """Arquivos de base GNSS e resultados de PPK associados ao voo.

    O MVP importa posições já corrigidas (CSV/TXT). Guardar a origem aqui é o
    que permite, depois, rodar o pós-processamento dentro do próprio app.
    """

    __tablename__ = "gnss_datasets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(30), default="corrected_positions")
    source_file: Mapped[str] = mapped_column(Text, default="")
    epsg: Mapped[int] = mapped_column(Integer, default=4326)
    applied_to: Mapped[int] = mapped_column(Integer, default=0)
    not_matched: Mapped[list] = mapped_column(JSON, default=list)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GroundControlPoint(Base):
    """Preparado para GCP; não usado pelo MVP, mas já persistível."""

    __tablename__ = "gcps"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(80))
    epsg: Mapped[int] = mapped_column(Integer, default=4326)
    x: Mapped[float] = mapped_column(Float)
    y: Mapped[float] = mapped_column(Float)
    z: Mapped[float | None] = mapped_column(Float, default=None)
    observations: Mapped[list] = mapped_column(JSON, default=list)  # [{image_id, px, py}]
