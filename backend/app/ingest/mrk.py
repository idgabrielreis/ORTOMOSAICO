"""Leitura do arquivo .MRK dos drones DJI com RTK.

Em um voo RTK/PPK a DJI grava, ao lado das fotos, quatro arquivos por sessão:

    DJI_..._PPKRAW.bin      observações brutas do receptor do drone
    DJI_..._PPKOBS.obs      RINEX de observação
    DJI_..._PPKNAV.nav      RINEX de navegação
    DJI_..._Timestamp.MRK   evento de cada foto: posição, desvios e flag

O .MRK é o que interessa de imediato: cada linha corresponde a uma fotografia e
traz latitude, longitude, altitude elipsoidal, os desvios padrão em N/E/V e a
flag de qualidade da solução. É a posição do centro de projeção no instante do
disparo — bem melhor que o GPS de navegação do EXIF.

Formato de uma linha (separada por tabulações):

    1  230446.311477  [2226]  -8.40,N  -40.87,E  -45.81,V
       -21.17561264,Lat  -47.83656812,Lon  632.169,Ellh  0.017,0.015,0.036,Q  50

O campo final é a flag RTK (50 = fixed, 34 = float, 16 = single, 0 = sem
correção), e o bloco `Q` traz os desvios padrão em metros.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MRK_SUFFIXES = (".mrk",)
RINEX_SUFFIXES = (".obs", ".nav", ".bin")

# Flags da DJI: 50 fixed, 34 float, 16 single, 0 sem solução.
RTK_FLAG_LABEL = {"50": "fixed", "34": "float", "16": "single", "0": "none"}
_VALUE = re.compile(r"(-?\d+(?:\.\d+)?)\s*,\s*([A-Za-z]+)")


@dataclass
class MrkEvent:
    sequence: int
    latitude: float
    longitude: float
    ellipsoidal_height: float
    std_north: float | None = None
    std_east: float | None = None
    std_vertical: float | None = None
    flag: str = "0"

    @property
    def horizontal_accuracy_m(self) -> float | None:
        if self.std_north is None or self.std_east is None:
            return None
        return round((self.std_north**2 + self.std_east**2) ** 0.5, 4)

    @property
    def quality(self) -> str:
        return RTK_FLAG_LABEL.get(self.flag, self.flag)

    @property
    def is_fixed(self) -> bool:
        return self.flag == "50"


def parse_mrk(path: Path | str) -> list[MrkEvent]:
    """Lê o .MRK e devolve um evento por fotografia. Linhas inválidas são puladas."""
    path = Path(path)
    events: list[MrkEvent] = []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return events

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = re.split(r"[\t]+|\s{2,}", line)
        if len(fields) < 4:
            fields = line.split()
        try:
            sequence = int(fields[0])
        except (ValueError, IndexError):
            continue

        tagged = {tag.lower(): float(value) for value, tag in _VALUE.findall(line)}
        latitude = tagged.get("lat")
        longitude = tagged.get("lon")
        height = tagged.get("ellh")
        if latitude is None or longitude is None:
            continue

        # O bloco de desvios vem como "0.017,0.015,0.036,Q"; a flag é o último campo.
        std_north = std_east = std_vertical = None
        for chunk in fields:
            parts = [part.strip() for part in chunk.split(",")]
            if len(parts) == 4 and parts[3].upper() == "Q":
                try:
                    std_north, std_east, std_vertical = (float(p) for p in parts[:3])
                except ValueError:
                    pass
        flag = fields[-1].strip() if fields else "0"

        events.append(
            MrkEvent(
                sequence=sequence,
                latitude=latitude,
                longitude=longitude,
                ellipsoidal_height=height if height is not None else 0.0,
                std_north=std_north,
                std_east=std_east,
                std_vertical=std_vertical,
                flag=flag if flag.isdigit() else "0",
            )
        )
    return events


def sequence_from_filename(name: str) -> int | None:
    """Extrai o número de sequência do padrão DJI `DJI_20260820093250_0001_D.JPG`."""
    match = re.search(r"_(\d{4})(?:_[A-Z]+)?\.[A-Za-z0-9]+$", name)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d{4})\D*$", Path(name).stem)
    return int(match.group(1)) if match else None
