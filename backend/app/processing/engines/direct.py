"""Motor `direct`: ortomosaico por georreferenciamento direto.

Projeta cada imagem no plano do terreno usando GPS, altitude relativa, geometria
da câmera e o yaw do gimbal, e compõe o mosaico com feathering e correção de
exposição. Não faz SfM nem bundle adjustment e assume terreno plano, portanto a
precisão é inferior à do motor ODM — mas o produto é um GeoTIFF realmente
georreferenciado, não uma simulação.

Uso: pré-visualização rápida do voo, validação do dataset, ambiente sem Docker
e testes automatizados.
"""
from __future__ import annotations

import math
import statistics
from pathlib import Path

import numpy as np

from ...geo.crs import from_utm, to_utm, utm_epsg
from ...geo.footprint import CameraGeometry, footprint_corners
from .base import EngineContext, EngineResult, EngineUnavailable, ImageRef

MAX_OUTPUT_PIXELS = 60_000_000  # teto do canvas; acima disso o GSD é relaxado
FEATHER_FRACTION = 0.12         # largura da borda suavizada, em fração da imagem


def _geometry(img: ImageRef) -> CameraGeometry | None:
    if not (img.width and img.height and img.focal_length_mm and img.sensor_width_mm):
        return None
    return CameraGeometry(img.width, img.height, img.focal_length_mm, img.sensor_width_mm)


def _feather_weights(height: int, width: int) -> np.ndarray:
    """Peso 0 na borda e 1 no miolo, para o blending entre imagens vizinhas."""
    fy = max(1, int(height * FEATHER_FRACTION))
    fx = max(1, int(width * FEATHER_FRACTION))
    wy = np.ones(height, dtype=np.float32)
    wx = np.ones(width, dtype=np.float32)
    wy[:fy] = np.linspace(0.02, 1.0, fy, dtype=np.float32)
    wy[-fy:] = np.linspace(1.0, 0.02, fy, dtype=np.float32)
    wx[:fx] = np.linspace(0.02, 1.0, fx, dtype=np.float32)
    wx[-fx:] = np.linspace(1.0, 0.02, fx, dtype=np.float32)
    return np.outer(wy, wx)


class DirectGeoreferencingEngine:
    name = "direct"
    description = "Ortomosaico por georreferenciamento direto (GPS + EXIF/XMP, terreno plano)"
    precision = "aproximada"

    def availability(self) -> tuple[bool, str]:
        try:
            import cv2  # noqa: F401
            import rasterio  # noqa: F401
        except ImportError as exc:
            return False, f"dependência ausente: {exc.name}"
        return True, "pronto"

    def run(self, ctx: EngineContext) -> EngineResult:
        import cv2
        import rasterio
        from rasterio.transform import from_origin

        usable = [
            img for img in ctx.images
            if img.latitude is not None and img.longitude is not None and _geometry(img)
        ]
        if len(usable) < 2:
            raise EngineUnavailable(
                "o motor direct precisa de ao menos 2 imagens com GPS e dados de câmera"
            )

        warnings: list[str] = []
        skipped = len(ctx.images) - len(usable)
        if skipped:
            warnings.append(f"{skipped} imagens sem GPS ou sem dados de câmera foram ignoradas")

        ctx.progress(3, 0.2, "Calculando geometria de tomada")
        lat0 = statistics.fmean(i.latitude for i in usable)
        lon0 = statistics.fmean(i.longitude for i in usable)
        epsg = ctx.output_epsg or utm_epsg(lat0, lon0)

        altitudes = [i.relative_altitude for i in usable if i.relative_altitude]
        if not altitudes:
            absolutes = [i.altitude for i in usable if i.altitude is not None]
            base = min(absolutes) if absolutes else 0.0
            altitudes = [(i.altitude - base) for i in usable if i.altitude is not None]
        mean_alt = statistics.fmean(altitudes) if altitudes else 0.0
        if mean_alt <= 1:
            raise EngineUnavailable(
                "altitude de voo indisponível nos metadados; use o motor ODM ou informe a altitude"
            )

        # Footprints e extensão total do mosaico.
        ctx.progress(4, 0.4, "Projetando footprints no solo")
        quads: list[tuple[ImageRef, list[tuple[float, float]], float]] = []
        for img in usable:
            geom = _geometry(img)
            alt = img.relative_altitude or mean_alt
            corners = footprint_corners(geom, img.longitude, img.latitude, alt, img.yaw, epsg)
            quads.append((img, corners, geom.gsd_m(alt)))

        xs = [x for _, corners, _ in quads for x, _ in corners]
        ys = [y for _, corners, _ in quads for _, y in corners]
        minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)

        from ..quality import preset as quality_preset

        preset = quality_preset(ctx.options.get("quality"))
        native_gsd = statistics.fmean(g for _, _, g in quads)
        gsd = (ctx.target_gsd_cm / 100) if ctx.target_gsd_cm else native_gsd * preset["gsd_factor"]
        width = int(math.ceil((maxx - minx) / gsd))
        height = int(math.ceil((maxy - miny) / gsd))
        if width * height > MAX_OUTPUT_PIXELS:
            factor = math.sqrt(width * height / MAX_OUTPUT_PIXELS)
            gsd *= factor
            width = int(math.ceil((maxx - minx) / gsd))
            height = int(math.ceil((maxy - miny) / gsd))
            warnings.append(
                f"GSD ajustado para {gsd * 100:.1f} cm/px para manter o mosaico abaixo de "
                f"{MAX_OUTPUT_PIXELS // 1_000_000} MP"
            )
        ctx.log(f"canvas {width}x{height} px, GSD {gsd * 100:.2f} cm/px, EPSG:{epsg}")

        accum = np.zeros((height, width, 3), dtype=np.float32)
        weights = np.zeros((height, width), dtype=np.float32)

        # Alvo de exposição: mediana das médias, para não deixar uma imagem
        # muito clara ou muito escura dominar a emenda.
        ctx.progress(5, 0.1, "Alinhando imagens pelo georreferenciamento direto")
        total = len(quads)
        means: list[float] = []

        for index, (img, corners, _) in enumerate(quads, start=1):
            if ctx.is_canceled():
                raise InterruptedError("cancelado pelo usuário")

            source = cv2.imread(str(img.path), cv2.IMREAD_COLOR)
            if source is None:
                warnings.append(f"não foi possível decodificar {img.name}")
                continue

            # Reduz a imagem ao tamanho que ela realmente ocupa no mosaico:
            # ampliar depois só gastaria memória sem ganhar detalhe.
            dest = np.array(
                [[(x - minx) / gsd, (maxy - y) / gsd] for x, y in corners], dtype=np.float32
            )
            span_x = float(np.linalg.norm(dest[1] - dest[0]))
            span_y = float(np.linalg.norm(dest[3] - dest[0]))
            scale = min(1.0, max(span_x / source.shape[1], span_y / source.shape[0]))
            if scale < 0.95:
                source = cv2.resize(
                    source, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA
                )
            sh, sw = source.shape[:2]

            patch = source.astype(np.float32)
            mean_luma = float(patch.mean())
            means.append(mean_luma)
            target = statistics.median(means)
            gain = float(np.clip(target / max(mean_luma, 1e-3), 0.7, 1.4))
            patch *= gain

            x0 = int(max(0, math.floor(dest[:, 0].min())))
            y0 = int(max(0, math.floor(dest[:, 1].min())))
            x1 = int(min(width, math.ceil(dest[:, 0].max())))
            y1 = int(min(height, math.ceil(dest[:, 1].max())))
            if x1 <= x0 or y1 <= y0:
                continue

            src_pts = np.array(
                [[0, 0], [sw - 1, 0], [sw - 1, sh - 1], [0, sh - 1]], dtype=np.float32
            )
            local = dest - np.array([x0, y0], dtype=np.float32)
            matrix = cv2.getPerspectiveTransform(src_pts, local)
            roi_w, roi_h = x1 - x0, y1 - y0

            warped = cv2.warpPerspective(
                patch, matrix, (roi_w, roi_h), flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0),
            )
            mask = cv2.warpPerspective(
                _feather_weights(sh, sw), matrix, (roi_w, roi_h),
                flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0,
            )

            accum[y0:y1, x0:x1] += warped * mask[..., None]
            weights[y0:y1, x0:x1] += mask

            if index % 5 == 0 or index == total:
                ctx.progress(7, index / total, f"Compondo mosaico ({index}/{total})")

        covered = weights > 1e-3
        if not covered.any():
            raise EngineUnavailable("nenhuma imagem pôde ser projetada no mosaico")

        ctx.progress(8, 0.3, "Escrevendo GeoTIFF")
        rgb = np.zeros_like(accum)
        np.divide(accum, weights[..., None], out=rgb, where=covered[..., None])
        rgb = np.clip(rgb, 0, 255).astype(np.uint8)
        alpha = (covered * 255).astype(np.uint8)

        ctx.output_dir.mkdir(parents=True, exist_ok=True)
        ortho_path = ctx.output_dir / "orthomosaic.tif"
        transform = from_origin(minx, maxy, gsd, gsd)
        profile = {
            "driver": "GTiff", "height": height, "width": width, "count": 4,
            "dtype": "uint8", "crs": f"EPSG:{epsg}", "transform": transform,
            "compress": "deflate", "predictor": 2, "tiled": True,
            "blockxsize": 512, "blockysize": 512, "BIGTIFF": "IF_SAFER",
        }
        with rasterio.open(ortho_path, "w", **profile) as dst:
            # OpenCV entrega BGR; o GeoTIFF sai em RGB + alfa.
            dst.write(rgb[:, :, 2], 1)
            dst.write(rgb[:, :, 1], 2)
            dst.write(rgb[:, :, 0], 3)
            dst.write(alpha, 4)
            dst.colorinterp = [
                rasterio.enums.ColorInterp.red, rasterio.enums.ColorInterp.green,
                rasterio.enums.ColorInterp.blue, rasterio.enums.ColorInterp.alpha,
            ]
            dst.build_overviews([2, 4, 8, 16], rasterio.enums.Resampling.average)
            dst.update_tags(
                ORTOMOSAICO_ENGINE="direct", ORTOMOSAICO_IMAGES=str(len(quads)),
                ORTOMOSAICO_GSD_CM=f"{gsd * 100:.2f}",
            )

        west, south = from_utm(minx, miny, epsg)
        east, north = from_utm(maxx, maxy, epsg)
        ctx.progress(8, 1.0, "Concluído")
        return EngineResult(
            orthomosaic=ortho_path,
            epsg=epsg,
            gsd_cm=round(gsd * 100, 2),
            bounds_wgs84=[west, south, east, north],
            warnings=warnings,
            stats={
                "images_projected": len(quads),
                "canvas_px": [width, height],
                "mean_altitude_m": round(mean_alt, 1),
                "coverage_percent": round(float(covered.mean()) * 100, 1),
            },
        )


__all__ = ["DirectGeoreferencingEngine", "to_utm"]
