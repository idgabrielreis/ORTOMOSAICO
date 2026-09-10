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
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ...geo.crs import from_utm, to_utm, utm_epsg
from .base import EngineContext, EngineResult, EngineUnavailable, ImageRef

# O ortomosaico sai no GSD nativo do voo. Não existe teto de resolução: um voo
# de 100 ha a 2,5 cm/px passa de 1,6 gigapixel, e reduzir isso jogaria fora
# justamente o detalhe que o drone capturou. O que torna esse tamanho viável é
# processar a saída em blocos, nunca com o mosaico inteiro em memória.
ORTHO_TILE = 2048                 # lado do bloco de saída, em pixels
IMAGE_CACHE_SIZE = 12             # fotos decodificadas mantidas em memória
SAFETY_MAX_PIXELS = 8_000_000_000 # guarda contra parâmetro absurdo (não é limite de qualidade)

# O MDS não precisa da resolução do ortomosaico: a nuvem esparsa não tem
# densidade para isso, e uma grade mais grossa é mais estável e muito mais leve.
DSM_GSD_FACTOR = 8

NEIGHBORS_PER_IMAGE = 12          # pares candidatos por imagem, escolhidos por GPS
MAX_FEATURE_IMAGE_SIZE = 2400     # só para detectar features; não afeta a saída
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


class _ImageCache:
    """Cache das fotos decodificadas.

    Blocos vizinhos do ortomosaico são cobertos pelas mesmas fotos; decodificar
    de novo a cada bloco dominaria o tempo de processamento. O cache é pequeno
    e limitado por número de imagens, para o pico de memória não depender do
    tamanho do voo.
    """

    def __init__(self, max_items: int):
        from collections import OrderedDict

        self.max_items = max_items
        self._items: OrderedDict[str, object] = OrderedDict()

    def get(self, key: str, path):
        import cv2

        if key in self._items:
            self._items.move_to_end(key)
            return self._items[key]
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            return None
        self._items[key] = image
        if len(self._items) > self.max_items:
            self._items.popitem(last=False)
        return image


def _sample_surface(
    surface: np.ndarray, minx: float, maxy: float, gsd: float,
    grid_x: np.ndarray, grid_y: np.ndarray,
) -> np.ndarray:
    """Amostra o MDS (grade grossa) nas coordenadas do bloco do ortomosaico."""
    import cv2

    columns = np.clip((grid_x - minx) / gsd - 0.5, 0, surface.shape[1] - 1).astype(np.float32)
    rows = np.clip((maxy - grid_y) / gsd - 0.5, 0, surface.shape[0] - 1).astype(np.float32)
    return cv2.remap(
        surface, columns, rows, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
    ).astype(np.float64)


@dataclass
class _View:
    """Uma fotografia alinhada, com a pose e o modelo de câmera do COLMAP."""

    name: str
    path: Path
    camera: object
    cam_from_world: np.ndarray
    center: np.ndarray
    west: float = 0.0
    south: float = 0.0
    east: float = 0.0
    north: float = 0.0

    def compute_extent(self, z_reference: float, z_min: float, z_max: float) -> None:
        """Área do solo que esta foto pode enxergar.

        Os quatro cantos da imagem são traçados como raios e intersectados com
        os planos de menor e maior cota do modelo; a envoltória disso é o
        retângulo usado para decidir quais fotos entram em cada bloco.
        """
        rotation = self.cam_from_world[:, :3]
        corners = np.array(
            [[0, 0], [self.camera.width, 0],
             [self.camera.width, self.camera.height], [0, self.camera.height]],
            dtype=np.float64,
        )
        rays = np.asarray(self.camera.cam_ray_from_img(corners), dtype=np.float64)
        directions = rays @ rotation  # equivale a R.T @ ray, por linha

        points = []
        for plane_z in (z_min - 5.0, z_reference, z_max + 5.0):
            for direction in directions:
                if abs(direction[2]) < 1e-9:
                    continue
                scale = (plane_z - self.center[2]) / direction[2]
                if scale <= 0:
                    continue
                points.append(self.center + direction * scale)
        if not points:
            self.west = self.south = self.east = self.north = 0.0
            return
        array = np.array(points)
        self.west, self.east = float(array[:, 0].min()), float(array[:, 0].max())
        self.south, self.north = float(array[:, 1].min()), float(array[:, 1].max())

    def intersects(self, west: float, south: float, east: float, north: float) -> bool:
        return not (
            self.east < west or self.west > east or self.north < south or self.south > north
        )

    def sample(
        self, world: np.ndarray, shape: tuple[int, int], cache: _ImageCache
    ) -> tuple[np.ndarray | None, np.ndarray]:
        """Projeta os pontos do bloco nesta foto e devolve cor e peso.

        Esta é a ortorretificação propriamente dita: cada célula do terreno é
        levada ao pixel correspondente da fotografia pela pose estimada, o que
        corrige o deslocamento causado pelo relevo.
        """
        import cv2

        source = cache.get(self.name, self.path)
        if source is None:
            return None, np.zeros(shape, dtype=np.float32)

        scale_x = source.shape[1] / self.camera.width
        scale_y = source.shape[0] / self.camera.height
        rotation = self.cam_from_world[:, :3]
        translation = self.cam_from_world[:, 3]

        in_camera = world @ rotation.T + translation
        visible = in_camera[:, 2] > 1e-6
        if not visible.any():
            return None, np.zeros(shape, dtype=np.float32)

        projected = np.full((world.shape[0], 2), -1.0, dtype=np.float64)
        projected[visible] = self.camera.img_from_cam(in_camera[visible])
        px = projected[:, 0] * scale_x
        py = projected[:, 1] * scale_y
        inside = (
            visible
            & (px >= 0) & (px < source.shape[1] - 1)
            & (py >= 0) & (py < source.shape[0] - 1)
        )
        if not inside.any():
            return None, np.zeros(shape, dtype=np.float32)

        map_x = np.where(inside, px, -1).astype(np.float32).reshape(shape)
        map_y = np.where(inside, py, -1).astype(np.float32).reshape(shape)
        sampled = cv2.remap(
            source, map_x, map_y, interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0),
        ).astype(np.float32)

        # Peso maior no centro da foto: menos distorção, visada mais próxima da
        # vertical e menos deslocamento por relevo. É o que define a seamline.
        center_x = self.camera.principal_point_x * scale_x
        center_y = self.camera.principal_point_y * scale_y
        normalized = np.sqrt(
            ((px - center_x) / (source.shape[1] / 2)) ** 2
            + ((py - center_y) / (source.shape[0] / 2)) ** 2
        )
        weight = np.clip(1.15 - normalized, 0.0, 1.0) ** 2
        weight = np.where(inside, weight, 0.0).astype(np.float32).reshape(shape)
        return sampled, weight


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
        import cv2  # noqa: F401  (usado pelos auxiliares deste módulo)
        import pycolmap
        import rasterio
        import rasterio.windows  # noqa: F401
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
        if width * height > SAFETY_MAX_PIXELS:
            factor = math.sqrt(width * height / SAFETY_MAX_PIXELS)
            gsd *= factor
            width = int(math.ceil((maxx - minx) / gsd))
            height = int(math.ceil((maxy - miny) / gsd))
            warnings.append(
                f"GSD ajustado para {gsd * 100:.2f} cm/px: o pedido excedia o limite de "
                "segurança de 8 gigapixels"
            )
        ctx.log(
            f"ortomosaico {width}x{height} px ({width * height / 1e6:.0f} MP), "
            f"GSD {gsd * 100:.2f} cm/px, EPSG:{epsg}"
        )

        # ------------------------------------------------------------------ MDS
        dsm_gsd = float(ctx.options.get("dsm_gsd_m") or gsd * DSM_GSD_FACTOR)
        dsm_width = max(2, int(math.ceil((maxx - minx) / dsm_gsd)))
        dsm_height = max(2, int(math.ceil((maxy - miny) / dsm_gsd)))
        surface, dsm_valid = _build_dsm(points, minx, maxy, dsm_gsd, dsm_width, dsm_height)
        ctx.log(f"MDS {dsm_width}x{dsm_height} px, GSD {dsm_gsd * 100:.1f} cm/px")
        ctx.progress(6, 0.9, "MDS gerado")

        # ---------------------------------------------- geometria das câmeras
        ctx.progress(7, 0.02, "Ortorretificando as fotografias")
        by_name = {image.name: image for image in ctx.images}
        views: list[_View] = []
        elevation_reference = float(np.median(points[:, 2]))
        for colmap_image in reconstruction.images.values():
            reference = by_name.get(colmap_image.name)
            if reference is None:
                continue
            camera = reconstruction.cameras[colmap_image.camera_id]
            view = _View(
                name=colmap_image.name,
                path=reference.path,
                camera=camera,
                cam_from_world=colmap_image.cam_from_world().matrix(),
                center=np.asarray(colmap_image.projection_center(), dtype=np.float64),
            )
            view.compute_extent(elevation_reference, float(points[:, 2].min()),
                                float(points[:, 2].max()))
            views.append(view)
        if not views:
            raise EngineUnavailable("nenhuma fotografia alinhada pôde ser ortorretificada")

        cache = _ImageCache(IMAGE_CACHE_SIZE)
        coverage_pixels = 0
        total_pixels = width * height

        # ------------------------------------------- gravação em blocos
        ctx.output_dir.mkdir(parents=True, exist_ok=True)
        transform_affine = from_origin(minx, maxy, gsd, gsd)
        ortho_path = ctx.output_dir / "orthomosaic.tif"
        tiles_x = math.ceil(width / ORTHO_TILE)
        tiles_y = math.ceil(height / ORTHO_TILE)
        tiles_total = tiles_x * tiles_y

        with rasterio.open(
            ortho_path, "w", driver="GTiff", height=height, width=width, count=4,
            dtype="uint8", crs=f"EPSG:{epsg}", transform=transform_affine,
            compress="deflate", predictor=2, tiled=True, blockxsize=512, blockysize=512,
            BIGTIFF="YES", num_threads="ALL_CPUS",
        ) as dst:
            dst.colorinterp = [
                rasterio.enums.ColorInterp.red, rasterio.enums.ColorInterp.green,
                rasterio.enums.ColorInterp.blue, rasterio.enums.ColorInterp.alpha,
            ]
            dst.update_tags(
                ORTOMOSAICO_ENGINE="sfm", ORTOMOSAICO_GSD_CM=f"{gsd * 100:.2f}",
                ORTOMOSAICO_IMAGES=str(len(views)),
            )

            done = 0
            for tile_row in range(tiles_y):
                for tile_col in range(tiles_x):
                    if ctx.is_canceled():
                        raise InterruptedError("cancelado pelo usuário")
                    row0 = tile_row * ORTHO_TILE
                    col0 = tile_col * ORTHO_TILE
                    tile_h = min(ORTHO_TILE, height - row0)
                    tile_w = min(ORTHO_TILE, width - col0)

                    west_tile = minx + col0 * gsd
                    east_tile = west_tile + tile_w * gsd
                    north_tile = maxy - row0 * gsd
                    south_tile = north_tile - tile_h * gsd

                    visible = [
                        view for view in views
                        if view.intersects(west_tile, south_tile, east_tile, north_tile)
                    ]
                    done += 1
                    if not visible:
                        continue

                    grid_x, grid_y = np.meshgrid(
                        west_tile + (np.arange(tile_w, dtype=np.float64) + 0.5) * gsd,
                        north_tile - (np.arange(tile_h, dtype=np.float64) + 0.5) * gsd,
                    )
                    grid_z = _sample_surface(surface, minx, maxy, dsm_gsd, grid_x, grid_y)
                    world = np.stack([grid_x, grid_y, grid_z], axis=-1).reshape(-1, 3)

                    accumulator = np.zeros((tile_h, tile_w, 3), dtype=np.float32)
                    weights = np.zeros((tile_h, tile_w), dtype=np.float32)
                    for view in visible:
                        sampled, weight = view.sample(world, (tile_h, tile_w), cache)
                        if sampled is None:
                            continue
                        accumulator += sampled * weight[..., None]
                        weights += weight

                    covered = weights > 1e-3
                    if not covered.any():
                        continue
                    coverage_pixels += int(covered.sum())

                    rgb = np.zeros_like(accumulator)
                    np.divide(accumulator, weights[..., None], out=rgb, where=covered[..., None])
                    rgb = np.clip(rgb, 0, 255).astype(np.uint8)
                    window = rasterio.windows.Window(col0, row0, tile_w, tile_h)
                    # OpenCV entrega BGR; o GeoTIFF sai em RGB + alfa.
                    dst.write(rgb[:, :, 2], 1, window=window)
                    dst.write(rgb[:, :, 1], 2, window=window)
                    dst.write(rgb[:, :, 0], 3, window=window)
                    dst.write((covered * 255).astype(np.uint8), 4, window=window)

                    ctx.progress(
                        7, done / tiles_total,
                        f"Ortorretificando bloco {done}/{tiles_total} "
                        f"({len(visible)} fotos)",
                    )

        if coverage_pixels == 0:
            raise EngineUnavailable("nenhuma fotografia pôde ser ortorretificada")

        ctx.progress(8, 0.3, "Gerando pirâmides e escrevendo o MDS")
        with rasterio.open(ortho_path, "r+") as dst:
            dst.build_overviews([2, 4, 8, 16, 32], rasterio.enums.Resampling.average)

        dsm_path = ctx.output_dir / "dsm.tif"
        # Só publica cota onde a nuvem tinha pontos: o preenchimento de buracos
        # serve para ortorretificar, não para inventar terreno.
        dsm_output = np.where(dsm_valid, surface, np.nan).astype(np.float32)
        with rasterio.open(
            dsm_path, "w", driver="GTiff", height=dsm_height, width=dsm_width, count=1,
            dtype="float32", crs=f"EPSG:{epsg}",
            transform=from_origin(minx, maxy, dsm_gsd, dsm_gsd),
            nodata=float("nan"), compress="deflate", predictor=3, tiled=True,
            blockxsize=512, blockysize=512, BIGTIFF="IF_SAFER",
        ) as dst:
            dst.write(dsm_output, 1)
            if min(dsm_width, dsm_height) > 256:
                dst.build_overviews([2, 4, 8], rasterio.enums.Resampling.average)
            dst.update_tags(
                ORTOMOSAICO_ENGINE="sfm",
                ORTOMOSAICO_SOURCE="nuvem esparsa do SfM",
                ORTOMOSAICO_POINTS=str(len(points)),
                ORTOMOSAICO_GSD_CM=f"{dsm_gsd * 100:.1f}",
            )

        cloud_path = ctx.output_dir / "point_cloud.ply"
        reconstruction.export_PLY(str(cloud_path))

        west, south = from_utm(minx, miny, epsg)
        east, north = from_utm(maxx, maxy, epsg)
        elevations = dsm_output[np.isfinite(dsm_output)]
        coverage_percent = round(coverage_pixels / total_pixels * 100, 1)
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
                "megapixels": round(width * height / 1e6, 1),
                "dsm_gsd_cm": round(dsm_gsd * 100, 1),
                "coverage_percent": coverage_percent,
                "elevation_min_m": round(float(np.percentile(elevations, 1)), 2)
                if elevations.size else None,
                "elevation_max_m": round(float(np.percentile(elevations, 99)), 2)
                if elevations.size else None,
                "dsm_measured_cells_percent": round(float(dsm_valid.mean()) * 100, 1),
                "mean_flight_height_m": round(flight_height, 1),
                "pairs_matched": len(pairs),
            },
        )
