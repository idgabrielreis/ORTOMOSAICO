"""EXIF e XMP: sem eles não há georreferenciamento."""
from __future__ import annotations

from pathlib import Path

from app.ingest.discovery import discover_images
from app.ingest.metadata import detect_band, read_metadata, read_xmp


def _primeira_imagem(flight_dir: Path) -> Path:
    return sorted(f.path for f in discover_images(flight_dir).valid)[0]


def test_le_gps_camera_e_geometria(flight_dir: Path):
    meta = read_metadata(_primeira_imagem(flight_dir))

    assert meta["error"] is None
    assert meta["latitude"] < 0 and meta["longitude"] < 0  # hemisfério sul, oeste
    assert meta["camera_model"] == "M3M"
    assert meta["focal_length_mm"] == 12.29
    assert meta["sensor_width_mm"] == 17.3
    assert meta["captured_at"] is not None


def test_le_xmp_da_dji(flight_dir: Path):
    path = _primeira_imagem(flight_dir)

    xmp = read_xmp(path)
    meta = read_metadata(path)

    assert "RelativeAltitude" in xmp
    # O voo simulado tem variação de altura, como um voo real.
    assert 105.0 < meta["relative_altitude"] < 115.0
    assert meta["pitch"] == -90.0  # gimbal em nadir


def test_banda_multiespectral_pelo_nome_do_arquivo():
    assert detect_band("DJI_20260909_0001_MS_NIR.TIF", {}) == "NIR"
    assert detect_band("DJI_20260909_0001_MS_RE.TIF", {}) == "RedEdge"
    assert detect_band("DJI_0001.JPG", {"BandName": "Green"}) == "Green"


def test_arquivo_corrompido_nao_levanta_excecao(tmp_path: Path):
    bad = tmp_path / "ruim.jpg"
    bad.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)

    meta = read_metadata(bad)

    assert meta["error"] is not None
