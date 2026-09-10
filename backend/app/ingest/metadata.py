"""Extração de metadados EXIF e XMP.

EXIF cobre GPS, câmera, tempo e geometria básica. Tudo que interessa de drone
(altitude relativa ao ponto de decolagem, ângulos do gimbal, flag de RTK, banda
espectral) só existe no bloco XMP da DJI, que não é EXIF e não é lido pelo
Pillow — por isso o parser dedicado abaixo.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, TiffImagePlugin
from PIL.ExifTags import GPSTAGS, TAGS

# Largura do sensor (mm) para câmeras comuns de drone. Usada quando o EXIF não
# traz FocalPlaneXResolution — necessário para calcular GSD e footprint.
SENSOR_WIDTH_MM: dict[str, float] = {
    "FC3411": 6.3,     # Mavic Air 2
    "FC3582": 9.6,     # Mini 3 Pro
    "FC7303": 6.3,     # Mini 2
    "FC6310": 13.2,    # Phantom 4 Pro
    "FC6360": 4.87,    # P4 Multispectral (banda)
    "FC6520": 17.3,    # Zenmuse X5S
    "FC220": 6.16,     # Mavic Pro
    "L1D-20c": 13.2,   # Mavic 2 Pro
    "M3M": 17.3,       # Mavic 3M RGB
    "M3E": 17.3,       # Mavic 3E
    "M3M-MS": 6.4,     # Mavic 3M multiespectral
    "ZH20T": 7.4,
}

# Sufixo de arquivo -> banda, no padrão de nomes da DJI multiespectral.
BAND_SUFFIXES = {
    "_ms_g": "Green", "_ms_r": "Red", "_ms_re": "RedEdge", "_ms_nir": "NIR",
    "_d": "RGB", "_g": "Green", "_r": "Red", "_re": "RedEdge", "_nir": "NIR",
}

_XMP_START = b"<x:xmpmeta"
_XMP_END = b"</x:xmpmeta>"
_XMP_ATTR = re.compile(r'(?:drone-dji|Camera|dji):([A-Za-z0-9_]+)\s*=\s*"([^"]*)"')
_XMP_TAG = re.compile(
    r"<(?:drone-dji|Camera|dji):([A-Za-z0-9_]+)>([^<]*)</(?:drone-dji|Camera|dji):[A-Za-z0-9_]+>"
)


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, TiffImagePlugin.IFDRational):
        return float(value) if value.denominator else None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (tuple, list)) and len(value) == 2 and value[1]:
        return float(value[0]) / float(value[1])
    if isinstance(value, str):
        try:
            return float(value.strip().lstrip("+"))
        except ValueError:
            return None
    return None


def _ref_str(value: Any) -> str | None:
    """Refs de GPS podem vir como str ou como bytes crus do EXIF."""
    if isinstance(value, bytes):
        return value.decode("ascii", "ignore").strip("\x00") or None
    return str(value) if value is not None else None


def _dms_to_degrees(dms: Any, ref: Any) -> float | None:
    try:
        d, m, s = (_to_float(v) or 0.0 for v in dms)
    except (TypeError, ValueError):
        return None
    deg = d + m / 60 + s / 3600
    ref = _ref_str(ref)
    if ref and ref.upper() in {"S", "W"}:
        deg = -deg
    return deg


def read_xmp(path: Path, max_bytes: int = 256 * 1024) -> dict[str, str]:
    """Lê o bloco XMP do início do arquivo, sem decodificar a imagem."""
    try:
        with path.open("rb") as fh:
            head = fh.read(max_bytes)
    except OSError:
        return {}
    start = head.find(_XMP_START)
    if start < 0:
        return {}
    end = head.find(_XMP_END, start)
    blob = head[start : end + len(_XMP_END)] if end > 0 else head[start:]
    text = blob.decode("utf-8", errors="ignore")
    data = {k: v.strip() for k, v in _XMP_ATTR.findall(text)}
    data.update({k: v.strip() for k, v in _XMP_TAG.findall(text)})
    return data


def _parse_exif_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(raw.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def detect_band(filename: str, xmp: dict[str, str]) -> str | None:
    if band := (xmp.get("BandName") or xmp.get("BandNames")):
        return band.strip().strip("[]'\" ")
    stem = Path(filename).stem.lower()
    for suffix, band in BAND_SUFFIXES.items():
        if stem.endswith(suffix):
            return band
    return None


def sensor_width(model: str | None, exif: dict[str, Any], width_px: int | None) -> float | None:
    """Largura física do sensor, por tabela ou derivada do EXIF."""
    if model and model.strip() in SENSOR_WIDTH_MM:
        return SENSOR_WIDTH_MM[model.strip()]
    x_res = _to_float(exif.get("FocalPlaneXResolution"))
    unit = exif.get("FocalPlaneResolutionUnit", 2)
    if x_res and width_px:
        mm_per_unit = {2: 25.4, 3: 10.0, 4: 1.0, 5: 0.001}.get(int(unit or 2), 25.4)
        return width_px / x_res * mm_per_unit
    # Última opção: derivar da relação entre focal real e equivalente 35 mm.
    focal = _to_float(exif.get("FocalLength"))
    focal35 = _to_float(exif.get("FocalLengthIn35mmFilm"))
    if focal and focal35:
        return 36.0 * focal / focal35
    return None


# RtkFlag da DJI: 0 sem correção, 16 float, 34/50 fixed (varia por firmware).
RTK_FIXED_FLAGS = {"16", "34", "50"}


def _positioning(xmp: dict[str, str], lat: float | None, lon: float | None) -> dict[str, Any]:
    """Classifica a origem da posição e o desvio informado pelo drone.

    O GPS de navegação é só a fonte mais fraca: quando o voo é RTK, o XMP traz
    a flag e os desvios padrão de cada eixo. PPK entra depois, por importação
    das posições corrigidas, e sobrescreve estes valores.
    """
    if lat is None or lon is None:
        return {"position_source": "none", "horizontal_accuracy_m": None,
                "vertical_accuracy_m": None}

    std_lat = _to_float(xmp.get("RtkStdLat"))
    std_lon = _to_float(xmp.get("RtkStdLon"))
    std_hgt = _to_float(xmp.get("RtkStdHgt"))
    flag = (xmp.get("RtkFlag") or "").strip()

    horizontal = None
    if std_lat is not None and std_lon is not None:
        horizontal = round((std_lat**2 + std_lon**2) ** 0.5, 4)
    elif std_lat is not None or std_lon is not None:
        horizontal = std_lat if std_lat is not None else std_lon

    is_rtk = flag in RTK_FIXED_FLAGS or (horizontal is not None and horizontal < 0.5)
    return {
        "position_source": "rtk" if is_rtk else "exif_gps",
        "horizontal_accuracy_m": horizontal,
        "vertical_accuracy_m": std_hgt,
    }


def read_metadata(path: Path | str) -> dict[str, Any]:
    """Devolve os metadados de uma imagem. Nunca levanta exceção."""
    path = Path(path)
    meta: dict[str, Any] = {"path": str(path), "error": None}
    try:
        with Image.open(path) as im:
            meta["width"], meta["height"] = im.size
            exif_raw = im.getexif()
            exif = {TAGS.get(tag, tag): value for tag, value in exif_raw.items()}
            gps_ifd = exif_raw.get_ifd(0x8825) or {}
            gps = {GPSTAGS.get(tag, tag): value for tag, value in gps_ifd.items()}
            exif.update(
                {TAGS.get(tag, tag): value for tag, value in exif_raw.get_ifd(0x8769).items()}
            )
    except Exception as exc:
        meta["error"] = f"{type(exc).__name__}: {exc}"
        return meta

    xmp = read_xmp(path)

    meta["camera_make"] = str(exif.get("Make", "") or "").strip() or None
    meta["camera_model"] = str(exif.get("Model", "") or "").strip() or None
    meta["focal_length_mm"] = _to_float(exif.get("FocalLength"))
    meta["focal_35mm"] = _to_float(exif.get("FocalLengthIn35mmFilm"))
    meta["sensor_width_mm"] = sensor_width(meta["camera_model"], exif, meta.get("width"))
    meta["captured_at"] = _parse_exif_datetime(
        exif.get("DateTimeOriginal") or exif.get("DateTime")
    )

    # GPS: XMP da DJI tem precisão maior e vem em graus decimais já assinados.
    lat = _to_float(xmp.get("GpsLatitude") or xmp.get("Latitude"))
    lon = _to_float(xmp.get("GpsLongitude") or xmp.get("GpsLongtitude") or xmp.get("Longitude"))
    if lat is None:
        lat = _dms_to_degrees(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef"))
    if lon is None:
        lon = _dms_to_degrees(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef"))
    meta["latitude"], meta["longitude"] = lat, lon

    altitude = _to_float(xmp.get("AbsoluteAltitude"))
    if altitude is None:
        altitude = _to_float(gps.get("GPSAltitude"))
        altitude_ref = gps.get("GPSAltitudeRef", 0)
        if isinstance(altitude_ref, bytes):
            altitude_ref = altitude_ref[0] if altitude_ref else 0
        if altitude is not None and int(altitude_ref or 0) == 1:
            altitude = -altitude
    meta["altitude"] = altitude
    meta["relative_altitude"] = _to_float(xmp.get("RelativeAltitude"))

    meta["yaw"] = _to_float(xmp.get("GimbalYawDegree") or xmp.get("FlightYawDegree"))
    meta["pitch"] = _to_float(xmp.get("GimbalPitchDegree") or xmp.get("FlightPitchDegree"))
    meta["roll"] = _to_float(xmp.get("GimbalRollDegree") or xmp.get("FlightRollDegree"))
    meta["rtk_flag"] = xmp.get("RtkFlag")
    meta.update(_positioning(xmp, lat, lon))
    meta["band"] = detect_band(path.name, xmp)

    meta["extra"] = {
        k: v for k, v in xmp.items()
        if k in {
            "RelativeAltitude", "AbsoluteAltitude", "FlightYawDegree", "GimbalYawDegree",
            "GimbalPitchDegree", "GimbalRollDegree", "RtkFlag", "RtkStdHgt", "BandName",
            "CentralWavelength", "WavelengthFWHM", "SensorGain", "ExposureTime",
            "IrradianceCalibrationMeasurement", "CalibratedOpticalCenterX",
            "CalibratedOpticalCenterY", "CalibratedFocalLength", "DewarpData",
        }
    }
    return meta
