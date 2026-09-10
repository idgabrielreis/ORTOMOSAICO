"""Importação de posições corrigidas (PPK, RTK pós-processado, base GNSS).

O pós-processamento GNSS costuma acontecer em software dedicado (RTKLIB, Emlid
Studio, o utilitário do fabricante) e sai como uma tabela: nome da foto e a
coordenada corrigida. Este módulo aplica essa tabela ao dataset do voo, guarda
a posição original de cada imagem e marca a fonte, para que o motor
fotogramétrico use a melhor posição disponível.

Quando o pós-processamento passar a rodar dentro do app, ele entra por aqui:
a interface com o resto do sistema continua sendo "posições corrigidas + fonte".
"""
from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field
from pathlib import Path

from ..models import Image, PositionSource

# Nomes aceitos para cada coluna, cobrindo as exportações mais comuns.
COLUMN_ALIASES = {
    "name": {"name", "file", "filename", "image", "photo", "arquivo", "imagem", "foto", "id"},
    "latitude": {"lat", "latitude", "y", "northing", "norte"},
    "longitude": {"lon", "long", "longitude", "x", "easting", "leste"},
    "altitude": {"alt", "altitude", "height", "ellh", "h", "z", "elevation"},
    "horizontal_accuracy": {"hacc", "h_acc", "sdxy", "std_xy", "horizontal_accuracy", "sde"},
    "vertical_accuracy": {"vacc", "v_acc", "sdz", "std_z", "vertical_accuracy", "sdu"},
}


@dataclass
class PositionImportResult:
    applied: int = 0
    not_matched: list[str] = field(default_factory=list)
    max_shift_m: float = 0.0
    mean_shift_m: float = 0.0
    rows: int = 0


def _normalize(header: str) -> str | None:
    key = header.strip().lower().lstrip("#").replace(" ", "_")
    for field_name, aliases in COLUMN_ALIASES.items():
        if key in aliases:
            return field_name
    return None


def parse_positions(content: bytes) -> list[dict]:
    """Lê CSV/TSV com cabeçalho, ou linhas `nome lat lon alt` sem cabeçalho."""
    text = content.decode("utf-8-sig", errors="ignore")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t ")
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(text), dialect)
    rows = [row for row in reader if row and not row[0].lstrip().startswith("%")]
    if not rows:
        return []

    mapping = {index: _normalize(cell) for index, cell in enumerate(rows[0])}
    has_header = sum(1 for value in mapping.values() if value) >= 3
    if not has_header:
        # Sem cabeçalho: assume a ordem mais comum das exportações PPK.
        mapping = {0: "name", 1: "latitude", 2: "longitude", 3: "altitude"}
        data_rows = rows
    else:
        data_rows = rows[1:]

    parsed: list[dict] = []
    for row in data_rows:
        record: dict = {}
        for index, cell in enumerate(row):
            field_name = mapping.get(index)
            if not field_name:
                continue
            cell = cell.strip()
            if field_name == "name":
                record["name"] = cell
                continue
            try:
                record[field_name] = float(cell.replace(",", "."))
            except ValueError:
                continue
        if record.get("name") and "latitude" in record and "longitude" in record:
            parsed.append(record)
    return parsed


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6_371_008.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def apply_positions(
    images: list[Image], records: list[dict], *, source: str = PositionSource.PPK
) -> PositionImportResult:
    """Aplica as posições corrigidas às imagens do voo, casando pelo nome."""
    result = PositionImportResult(rows=len(records))
    by_name: dict[str, Image] = {}
    for image in images:
        by_name.setdefault(image.filename.lower(), image)
        by_name.setdefault(Path(image.filename).stem.lower(), image)
        by_name.setdefault(image.relative_path.lower(), image)

    shifts: list[float] = []
    for record in records:
        raw_name = str(record["name"]).strip()
        image = (
            by_name.get(raw_name.lower())
            or by_name.get(Path(raw_name).name.lower())
            or by_name.get(Path(raw_name).stem.lower())
        )
        if image is None:
            result.not_matched.append(raw_name)
            continue

        if image.original_latitude is None and image.latitude is not None:
            image.original_latitude = image.latitude
            image.original_longitude = image.longitude
            image.original_altitude = image.altitude
        if image.latitude is not None:
            shifts.append(
                _distance_m(image.latitude, image.longitude,
                            record["latitude"], record["longitude"])
            )

        image.latitude = record["latitude"]
        image.longitude = record["longitude"]
        if record.get("altitude") is not None:
            image.altitude = record["altitude"]
        image.horizontal_accuracy_m = record.get("horizontal_accuracy",
                                                 image.horizontal_accuracy_m)
        image.vertical_accuracy_m = record.get("vertical_accuracy", image.vertical_accuracy_m)
        image.position_source = source
        result.applied += 1

    if shifts:
        result.max_shift_m = round(max(shifts), 3)
        result.mean_shift_m = round(sum(shifts) / len(shifts), 3)
    return result
