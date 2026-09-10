"""Motor `sfm`: fotogrametria real com COLMAP (pycolmap), sem Docker.

Pipeline executado aqui:

    features (SIFT) -> pares por GPS -> matching -> SfM incremental
    -> bundle adjustment -> alinhamento ao GPS/RTK (Sim3) -> MDS
    -> ortorretificação por projeção inversa -> ortomosaico

O que ele NÃO faz: reconstrução densa (MVS). O MDS sai da nuvem esparsa da
triangulação, então é mais grosseiro que o do ODM, que roda MVS completo. Para
o produto de máxima precisão o motor `odm` continua sendo a escolha; este existe
para gerar MDS e ortomosaico reais em máquina sem Docker, e é honesto quanto à
sua resolução: a densidade de pontos vai no relatório do job.

A ortorretificação é feita por projeção inversa: para cada célula do MDS, o
ponto (X, Y, Z) é projetado nas câmeras que o enxergam, usando as poses
estimadas e o modelo de câmera calibrado no bundle adjustment. É isso que
corrige o deslocamento causado pelo relevo — diferente de simplesmente
sobrepor imagens no plano.
"""
from __future__ import annotations

import math
import statistics
from pathlib import Path

import numpy as np

from ...geo.crs import from_utm, to_utm, utm_epsg
from .base import EngineContext, EngineResult, EngineUnavailable, ImageRef

MAX_OUTPUT_PIXELS = 60_000_000
NEIGHBORS_PER_IMAGE = 12          # pares candidatos por imagem, escolhidos por GPS
MAX_FEATURE_IMAGE_SIZE = 2400     # lado máximo usado na extração de features
DSM_SMOOTH_ITERATIONS = 2


def _focal_in_pixels(images: list[ImageRef]) -> float | None:
    """Focal em pixels a partir do EXIF: f_px = f_mm * largura_px / sensor_mm."""
    values = [
        image.focal_length_mm * image.width / image.sensor_width_mm
        for image in images
        if image.focal_length_mm and image.width and image.sensor_width_mm
    ]
    if len(values) < max(1, len(images) // 2):
        return None
    return float(statistics.median(values))


def _usable(images: list[ImageRef]) -> list[ImageRef]:
    return [i for i in images if i.latitude is not None and i.longitude is not None]


def _camera_locations(images: list[ImageRef], epsg: int) -> dict[str, tuple[float, float, float]]:
    """Posição de cada câmera em UTM (metros), que é o alvo do alinhamento."""
    altitudes = [i.altitude for i in images if i.altitude is not None]
    base = min(altitudes) if altitudes else 0.0
    locations: dict[str, tuple[float, float, float]] = {}
    for image in images:
        x, y = to_utm(image.longitude, image.latitude, epsg)
        if image.altitude is not None:
            z = image.altitude
        elif image.relative_altitude is not None:
            z = base + image.relative_altitude
        else:
            z = base
        locations[image.name] = (x, y, z)
    return locations


def _gps_pairs(
    images: list[ImageRef], epsg: int, neighbors: int = NEIGHBORS_PER_IMAGE
) -> list[tuple[str, str]]:
    """Pares candidatos pelos vizinhos mais próximos no solo.

    Comparar todas as imagens entre si é O(n²) e desnecessário: em um voo, só
    fotos próximas se sobrepõem. Com 3.000 imagens isso troca 4,5 milhões de
    pares por cerca de 36 mil.
    """
    positions = [(image.name, *to_utm(image.longitude, image.latitude, epsg)) for image in images]
    coordinates = np.array([[x, y] for _, x, y in positions], dtype=np.float64)
    names = [name for name, _, _ in positions]

    pairs: set[tuple[str, str]] = set()
    for index in range(len(names)):
        distances = np.linalg.norm(coordinates - coordinates[index], axis=1)
        order = np.argsort(distances)[1 : neighbors + 1]
        for other in order:
            a, b = sorted((names[index], names[int(other)]))
            pairs.add((a, b))
    # A ordem de captura também ajuda: fotos consecutivas quase sempre se
    # sobrepõem, mesmo quando o GPS de uma delas está ruim.
    for first, second in zip(names, names[1:]):
        pairs.add(tuple(sorted((first, second))))  # type: ignore[arg-type]
    return sorted(pairs)


def _build_dsm(
    points: np.ndarray, minx: float, maxy: float, gsd: float, width: int, height: int
) -> tuple[np.ndarray, np.ndarray]:
    """Rasteriza a nuvem esparsa em um Modelo Digital de Superfície.

    Cada célula recebe a maior cota entre os pontos que caem nela (superfície,
    não terreno). Buracos são preenchidos por dilatação iterativa da vizinhança
    válida, e o resultado passa por suavização leve para não deixar degraus que
    apareceriam como distorção no ortomosaico.
    """
    import cv2

    columns = np.clip(((points[:, 0] - minx) / gsd).astype(np.int32), 0, width - 1)
    rows = np.clip(((maxy - points[:, 1]) / gsd).astype(np.int32), 0, height - 1)

    dsm = np.full((height, width), np.nan, dtype=np.float32)
    flat_index = rows.astype(np.int64) * width + columns.astype(np.int64)
    order = np.argsort(points[:, 2])          # menor cota primeiro
    np.put(dsm, flat_index[order], points[order, 2])  # a maior cota sobrescreve

    valid = np.isfinite(dsm)
    if not valid.any():
        raise EngineUnavailable("a reconstrução não gerou pontos suficientes para o MDS")

    # Preenchimento por vizinhança: propaga as cotas conhecidas para os vazios.
    filled = np.where(valid, dsm, 0).astype(np.float32)
    weight = valid.astype(np.float32)
    kernel = np.ones((5, 5), np.float32)
    for _ in range(60):
        if weight.min() > 0:
            break
        filled = cv2.filter2D(filled, -1, kernel, borderType=cv2.BORDER_REPLICATE)
        weight = cv2.filter2D(weight, -1, kernel, borderType=cv2.BORDER_REPLICATE)
        known = weight > 0
        filled = np.where(known, filled / np.maximum(weight, 1e-6), 0)
        weight = known.astype(np.float32)
        filled = np.where(valid, dsm, filled)

    surface = np.where(valid, dsm, filled).astype(np.float32)
    for _ in range(DSM_SMOOTH_ITERATIONS):
        surface = cv2.GaussianBlur(surface, (5, 5), 0)
    surface = np.where(valid, dsm, surface).astype(np.float32)
    return surface, valid


class SfmEngine:
    name = "sfm"
    description = "COLMAP (pycolmap): SfM, bundle adjustment, MDS esparso e ortorretificação"
    precision = "fotogramétrica (nuvem esparsa)"

    def availability(self) -> tuple[bool, str]:
        missing = []
        for module in ("pycolmap", "cv2", "rasterio"):
            try:
                __import__(module)
            except ImportError:
                missing.append(module)
        if missing:
            return False, f"dependência ausente: {', '.join(missing)}"
        return True, "pronto (CPU)"

    def run(self, ctx: EngineContext) -> EngineResult:
        import cv2
        import pycolmap
        import rasterio
        from rasterio.transform import from_origin

        warnings: list[str] = []
        images = _usable(ctx.images)
        if len(images) < 5:
            raise EngineUnavailable("o motor sfm precisa de ao menos 5 imagens com GPS")
        skipped = len(ctx.images) - len(images)
        if skipped:
            warnings.append(f"{skipped} imagens sem GPS ficaram de fora do alinhamento")

        image_dir = ctx.images[0].path.parent
        work = ctx.work_dir / "colmap"
        work.mkdir(parents=True, exist_ok=True)
        database = work / "database.db"
        if database.exists():
            database.unlink()

        epsg = ctx.output_epsg or utm_epsg(images[0].latitude, images[0].longitude)
        locations = _camera_locations(images, epsg)
        names = [image.name for image in images]

        # ------------------------------------------------------------ features
        ctx.progress(3, 0.05, "Detectando características (SIFT)")
        extraction = pycolmap.FeatureExtractionOptions()
        extraction.max_image_size = MAX_FEATURE_IMAGE_SIZE
        extraction.sift.max_num_features = 8192

        # Intrínseca vinda do EXIF. Em voo nadir sobre terreno pouco acidentado,
        # focal e profundidade da cena são quase indistinguíveis: deixar a
        # autocalibração livre produz uma reconstrução coerente porém com escala
        # errada (e, por consequência, MDS e GSD errados). A focal da câmera do
        # drone é conhecida e confiável, então ela entra como parâmetro fixo.
        reader = pycolmap.ImageReaderOptions()
        focal_px = _focal_in_pixels(images)
        camera_mode = pycolmap.CameraMode.AUTO
        if focal_px:
            reference = next(
                image for image in images if image.width and image.focal_length_mm
            )
            reader.camera_model = "SIMPLE_RADIAL"
            reader.camera_params = (
                f"{focal_px},{reference.width / 2},{reference.height / 2},0"
            )
            camera_mode = pycolmap.CameraMode.SINGLE
            ctx.log(f"focal do EXIF: {focal_px:.1f} px (câmera fixa no bundle adjustment)")
        else:
            warnings.append(
                "focal ou tamanho de sensor ausentes no EXIF: a escala do modelo será estimada "
                "apenas pelas posições GPS, com precisão menor"
            )
        batch = max(1, len(names) // 20)
        for start in range(0, len(names), batch):
            if ctx.is_canceled():
                raise InterruptedError("cancelado pelo usuário")
            chunk = names[start : start + batch]
            pycolmap.extract_features(
                database_path=database,
                image_path=image_dir,
                image_names=chunk,
                camera_mode=camera_mode,
                reader_options=reader,
                extraction_options=extraction,
            )
            ctx.progress(
                3, min(0.99, (start + len(chunk)) / len(names)),
                f"Características {start + len(chunk)}/{len(names)}",
            )

        # ------------------------------------------------------------- matching
        ctx.progress(4, 0.1, "Selecionando pares pelo GPS")
        pairs = _gps_pairs(images, epsg)
        pairs_file = work / "pairs.txt"
        pairs_file.write_text("\n".join(f"{a} {b}" for a, b in pairs), encoding="utf-8")
        ctx.log(
            f"{len(pairs)} pares candidatos para {len(names)} imagens "
            f"(exaustivo seriam {len(names) * (len(names) - 1) // 2})"
        )

        pairing = pycolmap.ImportedPairingOptions()
        pairing.match_list_path = str(pairs_file)
        ctx.progress(4, 0.35, f"Correspondências em {len(pairs)} pares")
        pycolmap.match_image_pairs(database_path=database, pairing_options=pairing)
        ctx.progress(4, 1.0, "Correspondências concluídas")

        # ------------------------------------------------------------------ SfM
        ctx.progress(5, 0.05, "Alinhando imagens (SfM incremental)")
        sparse_dir = work / "sparse"
        sparse_dir.mkdir(exist_ok=True)
        registered = {"count": 0}

        def on_next_image() -> None:
            registered["count"] += 1
            ctx.progress(
                5, min(0.95, registered["count"] / len(names)),
                f"Alinhando imagens ({registered['count']}/{len(names)})",
            )

        mapper_options = pycolmap.IncrementalPipelineOptions()
        mapper_options.min_model_size = max(5, len(names) // 10)
        if focal_px:
            mapper_options.ba_refine_focal_length = False
            mapper_options.ba_refine_principal_point = False
        reconstructions = pycolmap.incremental_mapping(
            database_path=database,
            image_path=image_dir,
            output_path=sparse_dir,
            options=mapper_options,
            next_image_callback=on_next_image,
        )
        if not reconstructions:
            raise EngineUnavailable(
                "o SfM não conseguiu alinhar as imagens; verifique a sobreposição do voo"
            )
        reconstruction = max(reconstructions.values(), key=lambda r: r.num_reg_images())
        ctx.log(
            f"reconstrução: {reconstruction.num_reg_images()} imagens registradas, "
            f"{reconstruction.num_points3D()} pontos"
        )
        if reconstruction.num_reg_images() < len(names):
            warnings.append(
                f"{len(names) - reconstruction.num_reg_images()} imagens não foram alinhadas "
                "e ficaram fora do ortomosaico"
            )

        # -------------------------------------------- georreferenciamento (Sim3)
        ctx.progress(5, 0.97, "Georreferenciando pelo GPS/RTK das fotos")
        registered_names = [
            image.name for image in reconstruction.images.values()
            if image.name in locations
        ]
        target = np.array([locations[name] for name in registered_names], dtype=np.float64)
        transform = pycolmap.align_reconstruction_to_locations(
            reconstruction, registered_names, target, 3, pycolmap.RANSACOptions()
        )
        if transform is None:
            raise EngineUnavailable(
                "não foi possível alinhar a reconstrução às coordenadas GPS das fotos"
            )
        reconstruction.transform(transform)

        residuals = []
        for image in reconstruction.images.values():
            if image.name not in locations:
                continue
            center = np.asarray(image.projection_center(), dtype=np.float64)
            residuals.append(float(np.linalg.norm(center - np.array(locations[image.name]))))
        rms = round(math.sqrt(sum(r**2 for r in residuals) / len(residuals)), 3) if residuals else None
        ctx.log(f"resíduo das posições das câmeras: RMS {rms} m")

        # ------------------------------------------------------------------ MDS
        ctx.progress(6, 0.2, "Reconstruindo a superfície (MDS)")
        points = np.array(
            [point.xyz for point in reconstruction.points3D.values()], dtype=np.float64
        )
        if len(points) < 100:
            raise EngineUnavailable("nuvem de pontos pequena demais para gerar o MDS")

        # Remove pontos absurdos (ruído de triangulação) pelos percentis.
        z_low, z_high = np.percentile(points[:, 2], [1, 99])
        margin = max((z_high - z_low) * 0.5, 5.0)
        points = points[(points[:, 2] > z_low - margin) & (points[:, 2] < z_high + margin)]

        camera_heights = [locations[name][2] for name in registered_names]
        ground_z = float(np.median(points[:, 2]))
        flight_height = max(float(np.median(camera_heights)) - ground_z, 1.0)

        first = next(iter(reconstruction.images.values()))
        camera = reconstruction.cameras[first.camera_id]
        gsd = (
            ctx.target_gsd_cm / 100
            if ctx.target_gsd_cm
            else flight_height / camera.mean_focal_length()
        )

        minx, maxx = float(points[:, 0].min()), float(points[:, 0].max())
        miny, maxy = float(points[:, 1].min()), float(points[:, 1].max())
        width = int(math.ceil((maxx - minx) / gsd))
        height = int(math.ceil((maxy - miny) / gsd))
        if width * height > MAX_OUTPUT_PIXELS:
            factor = math.sqrt(width * height / MAX_OUTPUT_PIXELS)
            gsd *= factor
            width = int(math.ceil((maxx - minx) / gsd))
            height = int(math.ceil((maxy - miny) / gsd))
            warnings.append(f"GSD ajustado para {gsd * 100:.1f} cm/px pelo tamanho do produto")
        ctx.log(f"grade {width}x{height}, GSD {gsd * 100:.2f} cm/px, EPSG:{epsg}")

        surface, dsm_valid = _build_dsm(points, minx, maxy, gsd, width, height)
        ctx.progress(6, 0.9, "MDS gerado")

        # -------------------------------------------------- ortorretificação
        ctx.progress(7, 0.02, "Ortorretificando as fotografias")
        xs = minx + (np.arange(width, dtype=np.float64) + 0.5) * gsd
        ys = maxy - (np.arange(height, dtype=np.float64) + 0.5) * gsd

        accumulator = np.zeros((height, width, 3), dtype=np.float32)
        weights = np.zeros((height, width), dtype=np.float32)
        by_name = {image.name: image for image in ctx.images}
        total = reconstruction.num_reg_images()

        for index, colmap_image in enumerate(reconstruction.images.values(), start=1):
            if ctx.is_canceled():
                raise InterruptedError("cancelado pelo usuário")
            reference = by_name.get(colmap_image.name)
            if reference is None:
                continue
            source = cv2.imread(str(reference.path), cv2.IMREAD_COLOR)
            if source is None:
                warnings.append(f"não foi possível decodificar {colmap_image.name}")
                continue

            camera = reconstruction.cameras[colmap_image.camera_id]
            scale_x = source.shape[1] / camera.width
            scale_y = source.shape[0] / camera.height
            cam_from_world = colmap_image.cam_from_world().matrix()
            rotation = cam_from_world[:, :3]
            translation = cam_from_world[:, 3]
            center = np.asarray(colmap_image.projection_center(), dtype=np.float64)

            # Recorte da grade que a foto pode enxergar, para não projetar o
            # mosaico inteiro a cada imagem.
            radius = flight_height * max(camera.width, camera.height) / (
                2 * camera.mean_focal_length()
            ) * 1.4
            col0 = max(0, int((center[0] - radius - minx) / gsd))
            col1 = min(width, int((center[0] + radius - minx) / gsd) + 1)
            row0 = max(0, int((maxy - center[1] - radius) / gsd))
            row1 = min(height, int((maxy - center[1] + radius) / gsd) + 1)
            if col1 <= col0 or row1 <= row0:
                continue

            grid_x, grid_y = np.meshgrid(xs[col0:col1], ys[row0:row1])
            grid_z = surface[row0:row1, col0:col1]
            world = np.stack([grid_x, grid_y, grid_z], axis=-1).reshape(-1, 3)

            in_camera = world @ rotation.T + translation
            depth = in_camera[:, 2]
            visible = depth > 1e-6
            if not visible.any():
                continue

            projected = np.full((world.shape[0], 2), -1.0, dtype=np.float64)
            projected[visible] = camera.img_from_cam(in_camera[visible])
            px = projected[:, 0] * scale_x
            py = projected[:, 1] * scale_y
            inside = (
                visible
                & (px >= 0) & (px < source.shape[1] - 1)
                & (py >= 0) & (py < source.shape[0] - 1)
            )
            if not inside.any():
                continue

            patch_shape = (row1 - row0, col1 - col0)
            map_x = np.where(inside, px, -1).astype(np.float32).reshape(patch_shape)
            map_y = np.where(inside, py, -1).astype(np.float32).reshape(patch_shape)
            sampled = cv2.remap(
                source, map_x, map_y, interpolation=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0),
            ).astype(np.float32)

            # Peso: prioriza o centro da foto (menos distorção, visada mais
            # próxima da vertical) e descarta as bordas, onde o relevo desloca mais.
            center_x = camera.principal_point_x * scale_x
            center_y = camera.principal_point_y * scale_y
            normalized = np.sqrt(
                ((px - center_x) / (source.shape[1] / 2)) ** 2
                + ((py - center_y) / (source.shape[0] / 2)) ** 2
            )
            weight = np.clip(1.15 - normalized, 0.0, 1.0) ** 2
            weight = np.where(inside, weight, 0.0).astype(np.float32).reshape(patch_shape)

            accumulator[row0:row1, col0:col1] += sampled * weight[..., None]
            weights[row0:row1, col0:col1] += weight

            if index % 5 == 0 or index == total:
                ctx.progress(7, index / max(total, 1), f"Ortorretificando ({index}/{total})")

        covered = weights > 1e-3
        if not covered.any():
            raise EngineUnavailable("nenhuma fotografia pôde ser ortorretificada")

        rgb = np.zeros_like(accumulator)
        np.divide(accumulator, weights[..., None], out=rgb, where=covered[..., None])
        rgb = np.clip(rgb, 0, 255).astype(np.uint8)
        alpha = (covered * 255).astype(np.uint8)

        # ----------------------------------------------------------- gravação
        ctx.progress(8, 0.3, "Escrevendo GeoTIFF do ortomosaico e do MDS")
        ctx.output_dir.mkdir(parents=True, exist_ok=True)
        transform_affine = from_origin(minx, maxy, gsd, gsd)
        ortho_path = ctx.output_dir / "orthomosaic.tif"
        with rasterio.open(
            ortho_path, "w", driver="GTiff", height=height, width=width, count=4,
            dtype="uint8", crs=f"EPSG:{epsg}", transform=transform_affine,
            compress="deflate", predictor=2, tiled=True, blockxsize=512, blockysize=512,
            BIGTIFF="IF_SAFER",
        ) as dst:
            dst.write(rgb[:, :, 2], 1)
            dst.write(rgb[:, :, 1], 2)
            dst.write(rgb[:, :, 0], 3)
            dst.write(alpha, 4)
            dst.colorinterp = [
                rasterio.enums.ColorInterp.red, rasterio.enums.ColorInterp.green,
                rasterio.enums.ColorInterp.blue, rasterio.enums.ColorInterp.alpha,
            ]
            dst.build_overviews([2, 4, 8, 16], rasterio.enums.Resampling.average)
            dst.update_tags(ORTOMOSAICO_ENGINE="sfm", ORTOMOSAICO_GSD_CM=f"{gsd * 100:.2f}")

        dsm_path = ctx.output_dir / "dsm.tif"
        # Só publica cota onde há cobertura fotográfica: o preenchimento de
        # buracos serve para ortorretificar, não para inventar terreno.
        dsm_output = np.where(covered, surface, np.nan).astype(np.float32)
        with rasterio.open(
            dsm_path, "w", driver="GTiff", height=height, width=width, count=1,
            dtype="float32", crs=f"EPSG:{epsg}", transform=transform_affine,
            nodata=float("nan"), compress="deflate", predictor=3, tiled=True,
            blockxsize=512, blockysize=512, BIGTIFF="IF_SAFER",
        ) as dst:
            dst.write(dsm_output, 1)
            dst.build_overviews([2, 4, 8, 16], rasterio.enums.Resampling.average)
            dst.update_tags(
                ORTOMOSAICO_ENGINE="sfm",
                ORTOMOSAICO_SOURCE="nuvem esparsa do SfM",
                ORTOMOSAICO_POINTS=str(len(points)),
            )

        cloud_path = ctx.output_dir / "point_cloud.ply"
        reconstruction.export_PLY(str(cloud_path))

        west, south = from_utm(minx, miny, epsg)
        east, north = from_utm(maxx, maxy, epsg)
        elevations = dsm_output[np.isfinite(dsm_output)]
        ctx.progress(8, 1.0, "Concluído")

        return EngineResult(
            orthomosaic=ortho_path,
            dsm=dsm_path,
            point_cloud=cloud_path if cloud_path.exists() else None,
            epsg=epsg,
            gsd_cm=round(gsd * 100, 2),
            bounds_wgs84=[west, south, east, north],
            warnings=warnings,
            stats={
                "registered_images": reconstruction.num_reg_images(),
                "total_images": len(names),
                "sparse_points": int(len(points)),
                "gps_rms_m": rms,
                "canvas_px": [width, height],
                "coverage_percent": round(float(covered.mean()) * 100, 1),
                "elevation_min_m": round(float(np.percentile(elevations, 1)), 2)
                if elevations.size else None,
                "elevation_max_m": round(float(np.percentile(elevations, 99)), 2)
                if elevations.size else None,
                "dsm_measured_cells_percent": round(float(dsm_valid.mean()) * 100, 1),
                "mean_flight_height_m": round(flight_height, 1),
                "pairs_matched": len(pairs),
            },
        )
