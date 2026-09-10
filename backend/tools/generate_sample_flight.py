#!/usr/bin/env python3
"""Gera um voo sintético para demonstração e testes.

Cria uma cena de campo agrícola, simula um voo em grade sobre ela e recorta as
imagens que a câmera veria, com EXIF (GPS, câmera, tempo) e XMP da DJI
(altitude relativa, ângulos de gimbal). As imagens são distribuídas em várias
subpastas, como acontece quando o voo é dividido em partes ou cartões.

    python tools/generate_sample_flight.py --out /tmp/VOO_FAZENDA_X --parts 4

O resultado serve para exercitar o caminho completo: descoberta recursiva ->
dataset único -> ortomosaico georreferenciado.
"""
from __future__ import annotations

import argparse
import math
import random
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

XMP_TEMPLATE = """<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:drone-dji="http://www.dji.com/drone-dji/1.0/"
   drone-dji:AbsoluteAltitude="{absolute_altitude:+.2f}"
   drone-dji:RelativeAltitude="{relative_altitude:+.2f}"
   drone-dji:GpsLatitude="{latitude:.10f}"
   drone-dji:GpsLongitude="{longitude:.10f}"
   drone-dji:GimbalRollDegree="{roll:+.2f}"
   drone-dji:GimbalYawDegree="{yaw:+.2f}"
   drone-dji:GimbalPitchDegree="{pitch:+.2f}"
   drone-dji:FlightYawDegree="{yaw:+.2f}"
   drone-dji:RtkFlag="{rtk}"
   drone-dji:BandName="{band}"/>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""


def build_terrain(width: int, height: int, ground_gsd: float, seed: int = 11) -> np.ndarray:
    """Campo de altitudes do terreno, em metros acima do plano de referência.

    Sem relevo não existe paralaxe entre as fotos, e sem paralaxe nenhuma
    reconstrução fotogramétrica é possível: as imagens ficariam ligadas por uma
    homografia e a profundidade seria indeterminada. O terreno tem colinas
    suaves, um talude e árvores isoladas, que é o que dá volume ao MDS.
    """
    rng = np.random.default_rng(seed)
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
    xs *= ground_gsd
    ys *= ground_gsd
    terrain = np.zeros((height, width), dtype=np.float32)

    for _ in range(5):  # colinas
        cx = rng.uniform(0, width * ground_gsd)
        cy = rng.uniform(0, height * ground_gsd)
        amplitude = rng.uniform(4.0, 14.0)
        sigma = rng.uniform(40.0, 110.0)
        terrain += amplitude * np.exp(-(((xs - cx) ** 2 + (ys - cy) ** 2) / (2 * sigma**2)))

    # Talude atravessando a área: garante variação sistemática de cota.
    terrain += np.clip((xs - width * ground_gsd * 0.55) * 0.035, 0, 6.0)

    for _ in range(90):  # árvores e moitas
        cx = rng.uniform(0, width * ground_gsd)
        cy = rng.uniform(0, height * ground_gsd)
        amplitude = rng.uniform(3.0, 9.0)
        sigma = rng.uniform(2.0, 5.0)
        terrain += amplitude * np.exp(-(((xs - cx) ** 2 + (ys - cy) ** 2) / (2 * sigma**2)))

    return cv2.GaussianBlur(terrain, (0, 0), 1.5)


def build_scene(width: int, height: int, seed: int = 7) -> np.ndarray:
    """Cena sintética que lembra talhões agrícolas vistos de cima."""
    rng = np.random.default_rng(seed)
    scene = np.zeros((height, width, 3), dtype=np.uint8)
    scene[:, :] = (60, 110, 70)

    for _ in range(6):  # talhões
        x0, y0 = rng.integers(0, width // 2), rng.integers(0, height // 2)
        w, h = rng.integers(width // 6, width // 2), rng.integers(height // 6, height // 2)
        color = tuple(int(c) for c in rng.integers(40, 200, 3))
        cv2.rectangle(scene, (int(x0), int(y0)), (int(x0 + w), int(y0 + h)), color, -1)

    for x in range(0, width, 24):  # linhas de plantio
        cv2.line(scene, (x, 0), (x + 60, height), (30, 90, 40), 3)
    for _ in range(180):  # falhas, árvores, pivôs
        cx, cy = int(rng.integers(0, width)), int(rng.integers(0, height))
        cv2.circle(scene, (cx, cy), int(rng.integers(6, 26)),
                   tuple(int(c) for c in rng.integers(20, 240, 3)), -1)
    # Textura de alta frequência: sem ela o detector de características não tem
    # o que casar entre as fotos.
    noise = rng.integers(0, 40, (height, width, 1), dtype=np.int16)
    scene = np.clip(scene.astype(np.int16) + noise - 20, 0, 255).astype(np.uint8)
    return cv2.GaussianBlur(scene, (3, 3), 0)


def render_view(
    texture: np.ndarray,
    terrain: np.ndarray,
    ground_gsd: float,
    origin: tuple[float, float],
    camera_xyz: tuple[float, float, float],
    yaw_deg: float,
    focal_px: float,
    size: tuple[int, int],
) -> np.ndarray:
    """Renderiza o que a câmera veria, com projeção perspectiva sobre o relevo.

    Para cada pixel traça-se o raio da câmera e busca-se a interseção com o
    campo de altitudes, iterando a partir do plano médio. É essa interseção que
    produz o deslocamento devido ao relevo — o mesmo efeito que a
    ortorretificação depois corrige.
    """
    width_px, height_px = size
    cam_x, cam_y, cam_z = camera_xyz
    west, north = origin
    terrain_h, terrain_w = terrain.shape

    us, vs = np.meshgrid(
        np.arange(width_px, dtype=np.float32) + 0.5,
        np.arange(height_px, dtype=np.float32) + 0.5,
    )
    # Raio no referencial da câmera (nadir), depois girado pelo yaw.
    dx = (us - width_px / 2) / focal_px
    dy = (vs - height_px / 2) / focal_px
    theta = math.radians(yaw_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    east_component = dx * cos_t + dy * sin_t
    north_component = -(dx * -sin_t + dy * cos_t)

    def sample_terrain(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        col = np.clip((x - west) / ground_gsd, 0, terrain_w - 1).astype(np.float32)
        row = np.clip((north - y) / ground_gsd, 0, terrain_h - 1).astype(np.float32)
        return cv2.remap(terrain, col, row, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

    ground_z = float(np.median(terrain))
    depth = cam_z - ground_z
    for _ in range(6):  # converge a interseção raio/terreno
        x = cam_x + east_component * depth
        y = cam_y + north_component * depth
        z = sample_terrain(x, y)
        depth = cam_z - z

    col = np.clip((x - west) / ground_gsd, 0, terrain_w - 1).astype(np.float32)
    row = np.clip((north - y) / ground_gsd, 0, terrain_h - 1).astype(np.float32)
    return cv2.remap(texture, col, row, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def dd_to_dms(value: float) -> tuple[float, float, float]:
    value = abs(value)
    degrees = int(value)
    minutes_full = (value - degrees) * 60
    minutes = int(minutes_full)
    return float(degrees), float(minutes), round((minutes_full - minutes) * 60, 4)


def inject_xmp(path: Path, xmp: str) -> None:
    """Insere um segmento APP1 com o bloco XMP logo após o SOI do JPEG."""
    payload = b"http://ns.adobe.com/xap/1.0/\x00" + xmp.encode("utf-8")
    segment = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    data = path.read_bytes()
    path.write_bytes(data[:2] + segment + data[2:])


def write_image(
    path: Path, pixels: np.ndarray, *, latitude: float, longitude: float,
    absolute_altitude: float, relative_altitude: float, yaw: float,
    captured_at: datetime, focal_mm: float, focal_35mm: float, band: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.fromarray(cv2.cvtColor(pixels, cv2.COLOR_BGR2RGB))
    exif = Image.Exif()
    exif[0x010F] = "DJI"
    exif[0x0110] = "M3M"
    exif[0x0131] = "ortomosaico-sample-generator"
    exif[0x8769] = {
        0x920A: focal_mm,
        0xA405: int(focal_35mm),
        0x9003: captured_at.strftime("%Y:%m:%d %H:%M:%S"),
        0x829A: 0.001,
        0x8827: 100,
    }
    exif[0x8825] = {
        1: "S" if latitude < 0 else "N", 2: dd_to_dms(latitude),
        3: "W" if longitude < 0 else "E", 4: dd_to_dms(longitude),
        5: 0, 6: absolute_altitude,
    }
    image.save(path, "JPEG", quality=90, exif=exif.tobytes())
    inject_xmp(path, XMP_TEMPLATE.format(
        absolute_altitude=absolute_altitude, relative_altitude=relative_altitude,
        latitude=latitude, longitude=longitude, roll=0.0, yaw=yaw, pitch=-90.0,
        rtk=0, band=band,
    ))


def generate(
    out_dir: Path, *, parts: int = 4, rows: int = 6, cols: int = 8,
    altitude: float = 120.0, image_size: tuple[int, int] = (960, 720),
    center: tuple[float, float] = (-21.1750, -47.8367), seed: int = 7,
    corrupt: int = 2, duplicates: int = 2,
) -> dict:
    """Cria o voo em `out_dir`, dividido em `parts` subpastas."""
    from pyproj import Transformer

    random.seed(seed)
    lat0, lon0 = center
    zone = int((lon0 + 180) // 6) + 1
    epsg = (32600 if lat0 >= 0 else 32700) + zone
    to_utm = Transformer.from_crs(4326, epsg, always_xy=True)
    to_wgs = Transformer.from_crs(epsg, 4326, always_xy=True)
    cx, cy = to_utm.transform(lon0, lat0)

    width_px, height_px = image_size
    sensor_width_mm, focal_mm = 17.3, 12.29
    focal_35mm = round(36.0 * focal_mm / sensor_width_mm)
    gsd = altitude * sensor_width_mm / (focal_mm * width_px)   # metros por pixel
    ground_w, ground_h = gsd * width_px, gsd * height_px

    overlap_front, overlap_side = 0.75, 0.70
    step_x = ground_w * (1 - overlap_side)
    step_y = ground_h * (1 - overlap_front)

    total_w = step_x * (cols - 1) + ground_w
    total_h = step_y * (rows - 1) + ground_h
    # Margem: a foto enxerga além do nadir quando o terreno é baixo.
    margin = ground_w * 0.6
    scene_gsd = gsd / 2  # cena com o dobro da resolução das fotos
    scene_w = int((total_w + 2 * margin) / scene_gsd)
    scene_h = int((total_h + 2 * margin) / scene_gsd)
    scene = build_scene(scene_w, scene_h, seed=seed)
    terrain = build_terrain(scene_w, scene_h, scene_gsd, seed=seed + 4)

    west, north = cx - total_w / 2 - margin, cy + total_h / 2 + margin
    focal_px = focal_mm * width_px / sensor_width_mm
    ground_reference = float(np.median(terrain))
    start = datetime(2026, 9, 9, 9, 30, 0)
    manifest = {"images": [], "epsg": epsg, "gsd_cm": round(gsd * 100, 2),
                "area_ha": round(total_w * total_h / 10_000, 2),
                "terrain_min_m": None, "terrain_max_m": None}

    index = 0
    written: list[Path] = []
    for row in range(rows):
        for col in range(cols):
            index += 1
            # Serpentina, como um voo real; leve jitter de GPS e de yaw.
            actual_col = col if row % 2 == 0 else (cols - 1 - col)
            x = west + ground_w / 2 + actual_col * step_x + random.uniform(-1.5, 1.5)
            y = north - ground_h / 2 - row * step_y + random.uniform(-1.5, 1.5)

            yaw = random.uniform(-1.5, 1.5)
            camera_z = ground_reference + altitude + random.uniform(-1.0, 1.0)
            frame = render_view(
                scene, terrain, scene_gsd, (west, north), (x, y, camera_z),
                yaw, focal_px, (width_px, height_px),
            )
            # Variação de exposição entre faixas, como acontece em voo real.
            frame = np.clip(frame.astype(np.float32) * random.uniform(0.88, 1.12), 0, 255)
            frame = frame.astype(np.uint8)

            lon, lat = to_wgs.transform(x, y)
            part = f"PARTE_{index % parts + 1:02d}"
            name = f"DJI_{index:04d}.JPG"
            path = out_dir / part / name
            write_image(
                path, frame, latitude=lat, longitude=lon,
                absolute_altitude=520.0 + camera_z, relative_altitude=camera_z - ground_reference,
                yaw=yaw,
                captured_at=start + timedelta(seconds=index * 2),
                focal_mm=focal_mm, focal_35mm=focal_35mm, band="RGB",
            )
            written.append(path)
            manifest["images"].append(
                {"file": f"{part}/{name}", "lat": lat, "lon": lon, "alt": altitude}
            )

    # Arquivos problemáticos: o processamento deve seguir sem eles.
    for i in range(corrupt):
        bad = out_dir / "PARTE_01" / f"DJI_CORRUPT_{i:02d}.JPG"
        bad.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 128)
    # Mesma imagem copiada em outra pasta: deve entrar no dataset uma vez só.
    for i in range(min(duplicates, len(written))):
        target = out_dir / "BACKUP_CARTAO" / written[i].name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(written[i].read_bytes())

    manifest["terrain_min_m"] = round(float(terrain.min()), 2)
    manifest["terrain_max_m"] = round(float(terrain.max()), 2)
    manifest["folders"] = sorted({p.parent.name for p in written} | {"BACKUP_CARTAO"})
    manifest["count"] = len(written)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--parts", type=int, default=4)
    parser.add_argument("--rows", type=int, default=6)
    parser.add_argument("--cols", type=int, default=8)
    parser.add_argument("--altitude", type=float, default=120.0)
    args = parser.parse_args()

    manifest = generate(
        args.out, parts=args.parts, rows=args.rows, cols=args.cols, altitude=args.altitude
    )
    print(f"{manifest['count']} imagens em {len(manifest['folders'])} pastas -> {args.out}")
    print(f"área simulada: {manifest['area_ha']} ha, GSD {manifest['gsd_cm']} cm/px")


if __name__ == "__main__":
    main()
