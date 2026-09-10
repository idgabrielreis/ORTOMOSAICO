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
from ..quality import preset as quality_preset
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

NEIGHBORS_PER_IMAGE = 12          # padrão de pares por imagem; o preset de qualidade ajusta
DSM_SMOOTH_ITERATIONS = 2

# Acima disso o modelo não bate com as posições do drone e o produto não presta.
MAX_GEOREFERENCE_RMS_M = 60.0


def _factory_calibration(images: list[ImageRef]) -> dict | None:
    """Calibração gravada pelo fabricante, quando a maioria das fotos a traz."""
    found = [image.calibration for image in images if image.calibration]
    if len(found) < max(1, len(images) // 2):
        return None
    return found[0]


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


def _fit_plane(points: np.ndarray) -> tuple[float, float, float]:
    """Plano médio do terreno, z = a·x + b·y + c, por mínimos quadrados."""
    origin = points.mean(axis=0)
    design = np.column_stack(
        [points[:, 0] - origin[0], points[:, 1] - origin[1], np.ones(len(points))]
    )
    coefficients, *_ = np.linalg.lstsq(design, points[:, 2], rcond=None)
    a, b, c = coefficients
    return float(a), float(b), float(c - a * origin[0] - b * origin[1])


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

    # Fora da área medida, o terreno vira o plano médio ajustado à nuvem. Propagar
    # cotas para longe curvaria a superfície e entortaria a ortorretificação.
    a, b, c = _fit_plane(points)
    grid_x = minx + (np.arange(width, dtype=np.float64) + 0.5) * gsd
    grid_y = maxy - (np.arange(height, dtype=np.float64) + 0.5) * gsd
    plane = (a * grid_x[None, :] + b * grid_y[:, None] + c).astype(np.float32)
    dsm = np.where(valid, dsm, np.nan)

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

    # Mistura suave entre o medido (perto dos pontos) e o plano (longe deles).
    distance = cv2.distanceTransform(
        (~valid).astype(np.uint8), cv2.DIST_L2, 3
    ).astype(np.float32)
    blend = np.clip(distance / max(8.0, 0.02 * max(width, height)), 0.0, 1.0)
    surface = np.where(valid, dsm, filled * (1 - blend) + plane * blend).astype(np.float32)
    for _ in range(DSM_SMOOTH_ITERATIONS):
        surface = cv2.GaussianBlur(surface, (5, 5), 0)
    surface = np.where(valid, dsm, surface).astype(np.float32)
    return surface, valid


def _umeyama(source: np.ndarray, target: np.ndarray) -> tuple[float, np.ndarray, np.ndarray]:
    """Similaridade que leva `source` em `target` (Umeyama/Kabsch com escala)."""
    source_mean = source.mean(axis=0)
    target_mean = target.mean(axis=0)
    source_centered = source - source_mean
    target_centered = target - target_mean

    covariance = target_centered.T @ source_centered / len(source)
    u, singular, vt = np.linalg.svd(covariance)
    correction = np.eye(3)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        correction[2, 2] = -1.0  # evita solução com reflexão
    rotation = u @ correction @ vt
    variance = (source_centered**2).sum() / len(source)
    scale = float((singular * np.diag(correction)).sum() / variance) if variance > 0 else 1.0
    translation = target_mean - scale * rotation @ source_mean
    return scale, rotation, translation


def _viewing_direction(yaw_deg: float | None, pitch_deg: float | None) -> np.ndarray | None:
    """Direção para onde a câmera aponta, em ENU, a partir do gimbal.

    Yaw é o azimute (horário a partir do norte) e pitch é negativo abaixo do
    horizonte, como a DJI grava no XMP.
    """
    if yaw_deg is None or pitch_deg is None:
        return None
    yaw = math.radians(yaw_deg)
    pitch = math.radians(pitch_deg)
    return np.array(
        [math.cos(pitch) * math.sin(yaw), math.cos(pitch) * math.cos(yaw), math.sin(pitch)],
        dtype=np.float64,
    )


def _align_using_orientation(
    reconstruction, names: list[str], targets: np.ndarray,
    images: dict[str, ImageRef], distance: float,
):
    """Georreferencia usando posição E orientação das fotografias.

    Quando as câmeras estão quase em linha reta — uma única faixa de voo, ou um
    trecho curto — a posição sozinha não define a rotação do modelo: sobra a
    liberdade de girar em torno do eixo do voo. A orientação do gimbal remove
    essa ambiguidade. Cada foto contribui com um segundo ponto, deslocado na
    direção para onde a câmera aponta, no modelo e no mundo real.
    """
    import pycolmap

    by_name = {image.name: image for image in reconstruction.images.values()}
    source_points: list[np.ndarray] = []
    target_points: list[np.ndarray] = []
    used_orientation = 0

    for index, name in enumerate(names):
        colmap_image = by_name.get(name)
        reference = images.get(name)
        if colmap_image is None:
            continue
        center = np.asarray(colmap_image.projection_center(), dtype=np.float64)
        source_points.append(center)
        target_points.append(targets[index])

        direction = _viewing_direction(
            reference.yaw if reference else None, reference.pitch if reference else None
        )
        if direction is None:
            continue
        # Eixo óptico da câmera no modelo: terceira linha da rotação mundo->câmera.
        model_direction = colmap_image.cam_from_world().matrix()[2, :3]
        norm = np.linalg.norm(model_direction)
        if norm < 1e-9:
            continue
        source_points.append(center + model_direction / norm * distance)
        target_points.append(targets[index] + direction * distance)
        used_orientation += 1

    if len(source_points) < 3:
        return None, 0
    scale, rotation, translation = _umeyama(
        np.array(source_points), np.array(target_points)
    )
    if not np.isfinite(scale) or scale <= 0:
        return None, used_orientation
    # Sim3d aplica x' = escala · R · x + t, então a translação entra como está.
    return pycolmap.Sim3d(scale, pycolmap.Rotation3d(rotation), translation), used_orientation


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

    def compute_extent(
        self, z_reference: float, z_min: float, z_max: float,
        max_incidence_deg: float = 60.0,
    ) -> None:
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

        # Além do limite de incidência a foto não contribui, então esse trecho
        # de terreno não pertence ao footprint útil desta imagem.
        reach = max(self.center[2] - z_reference, 1.0) * math.tan(
            math.radians(max_incidence_deg)
        )
        self.west = max(self.west, self.center[0] - reach)
        self.east = min(self.east, self.center[0] + reach)
        self.south = max(self.south, self.center[1] - reach)
        self.north = min(self.north, self.center[1] + reach)

    def intersects(self, west: float, south: float, east: float, north: float) -> bool:
        return not (
            self.east < west or self.west > east or self.north < south or self.south > north
        )

    def off_nadir_degrees(self) -> float:
        """Quanto o eixo óptico se afasta da vertical, em graus."""
        axis = self.cam_from_world[2, :3]
        norm = np.linalg.norm(axis)
        if norm < 1e-9:
            return 0.0
        return float(math.degrees(math.acos(min(1.0, abs(axis[2]) / norm))))

    def sample(
        self, world: np.ndarray, shape: tuple[int, int], cache: _ImageCache,
        max_incidence_cos: float = 0.42,
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

        # Ângulo com que a visada chega ao solo. Perto da vertical o pixel do
        # solo é bem amostrado; muito inclinado, um pixel da foto cobre metros
        # de terreno e a projeção fica esticada. É o que produz o rastro
        # borrado nas bordas de fotos oblíquas.
        ray = world - self.center
        distance = np.linalg.norm(ray, axis=1)
        incidence_cos = np.abs(ray[:, 2]) / np.maximum(distance, 1e-9)
        visible &= incidence_cos >= max_incidence_cos
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
        # Visada mais próxima da vertical vale mais na composição.
        weight = weight * incidence_cos**2
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
        preset = quality_preset(ctx.options.get("quality"))
        ctx.log(f"qualidade: {preset['label']} — {preset['summary']}")
        images = _usable(ctx.images)
        if len(images) < 3:
            raise EngineUnavailable("o motor sfm precisa de ao menos 3 fotografias com posição")
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
        extraction.max_image_size = preset["feature_max_size"]
        extraction.sift.max_num_features = preset["max_features"]

        # Intrínseca vinda do EXIF. Em voo nadir sobre terreno pouco acidentado,
        # focal e profundidade da cena são quase indistinguíveis: deixar a
        # autocalibração livre produz uma reconstrução coerente porém com escala
        # errada (e, por consequência, MDS e GSD errados). A focal da câmera do
        # drone é conhecida e confiável, então ela entra como parâmetro fixo.
        reader = pycolmap.ImageReaderOptions()
        camera_mode = pycolmap.CameraMode.AUTO
        calibration = _factory_calibration(images)
        focal_px = calibration["fx"] if calibration else _focal_in_pixels(images)
        if calibration:
            # Calibração de fábrica: focal, centro óptico e distorção medidos
            # pelo fabricante. Melhor ponto de partida que qualquer estimativa.
            reader.camera_model = "OPENCV"
            reader.camera_params = ",".join(
                f"{calibration[key]}" for key in ("fx", "fy", "cx", "cy", "k1", "k2", "p1", "p2")
            )
            camera_mode = pycolmap.CameraMode.SINGLE
            ctx.log(
                f"calibração de fábrica da lente: f={calibration['fx']:.1f} px, "
                f"centro ({calibration['cx']:.1f}, {calibration['cy']:.1f}), "
                f"k1={calibration['k1']:.4f}"
            )
        elif focal_px:
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
        pairs = _gps_pairs(images, epsg, neighbors=preset["neighbors"])
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
        # O modelo mínimo nunca pode passar do número de fotos disponíveis,
        # senão o COLMAP descarta a reconstrução que acabou de montar.
        mapper_options.min_model_size = max(3, min(len(names), len(names) // 10))
        # O padrão do COLMAP (16°) foi pensado para fotos ao redor de um objeto.
        # Em voo de mapeamento a base entre fotos consecutivas é pequena diante
        # da altura de voo: 8 m de deslocamento a 120 m de altura dão cerca de
        # 4°. Com o limite padrão o par inicial nunca é aceito e a reconstrução
        # nem começa.
        mapper_options.mapper.init_min_tri_angle = 2.0
        if focal_px:
            # Com a intrínseca conhecida, deixar o bundle adjustment mexer nela
            # reintroduz a ambiguidade entre focal e profundidade da cena.
            mapper_options.ba_refine_focal_length = False
            mapper_options.ba_refine_principal_point = False
            mapper_options.ba_refine_extra_params = bool(calibration is None)
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
            # RANSAC precisa de posições bem distribuídas. Em uma faixa única ou
            # trecho curto elas ficam quase colineares e a rotação em torno do
            # eixo do voo fica indefinida; a orientação do gimbal resolve isso.
            by_name_ref = {image.name: image for image in images}
            altitudes = [
                image.relative_altitude for image in images if image.relative_altitude
            ]
            distance = statistics.fmean(altitudes) if altitudes else 100.0
            transform, used = _align_using_orientation(
                reconstruction, registered_names, target, by_name_ref, distance
            )
            if transform is not None:
                warnings.append(
                    "posições das câmeras quase colineares: o georreferenciamento usou também "
                    f"a orientação do gimbal de {used} fotografias. Com uma única faixa de voo "
                    "a orientação do modelo é menos confiável do que com faixas cruzadas"
                )
        if transform is None:
            raise EngineUnavailable(
                "não foi possível alinhar a reconstrução às posições das fotografias"
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
        if rms is not None and rms > MAX_GEOREFERENCE_RMS_M:
            # Melhor falhar do que gravar um GeoTIFF no lugar errado: um produto
            # georreferenciado com erro grosseiro é pior que produto nenhum.
            raise EngineUnavailable(
                f"georreferenciamento inconsistente: as posições calculadas ficaram a "
                f"{rms:.0f} m das posições das fotografias. Verifique se as fotos são do "
                "mesmo voo e se têm sobreposição suficiente"
            )
        if rms is not None and rms > 5 * (
            statistics.fmean(
                [image.horizontal_accuracy_m for image in images if image.horizontal_accuracy_m]
                or [2.0]
            )
        ):
            warnings.append(
                f"resíduo de {rms:.1f} m entre as posições calculadas e as gravadas pelo drone"
            )

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
        # GSD nativo do voo: altura de voo dividida pela focal em pixels.
        native_gsd = flight_height / camera.mean_focal_length()
        gsd = (ctx.target_gsd_cm / 100) if ctx.target_gsd_cm else native_gsd * preset["gsd_factor"]
        ctx.log(
            f"GSD nativo {native_gsd * 100:.2f} cm/px; saída {gsd * 100:.2f} cm/px "
            f"(qualidade {preset['label']})"
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
            views.append(view)
        if not views:
            raise EngineUnavailable("nenhuma fotografia alinhada pôde ser ortorretificada")

        off_nadir = [view.off_nadir_degrees() for view in views]
        median_off_nadir = statistics.median(off_nadir) if off_nadir else 0.0
        # Em voo nadir corta-se a partir de 55°; em voo oblíquo o limite
        # acompanha a inclinação real, senão não sobraria pixel algum.
        max_incidence_deg = min(70.0, max(55.0, median_off_nadir + 15.0))
        max_incidence_cos = math.cos(math.radians(max_incidence_deg))
        ctx.log(
            f"inclinação das fotos: {median_off_nadir:.1f}° do nadir; "
            f"visadas aceitas até {max_incidence_deg:.0f}°"
        )
        if median_off_nadir > 20:
            warnings.append(
                f"as fotografias estão a {median_off_nadir:.0f}° do nadir (captura oblíqua). "
                "O ortomosaico sai com resolução desigual e cobertura em leque; para "
                "mapeamento, o gimbal deve estar a -90°"
            )

        for view in views:
            view.compute_extent(
                elevation_reference, float(points[:, 2].min()), float(points[:, 2].max()),
                max_incidence_deg,
            )

        # A tela do ortomosaico é a união dos footprints úteis, não a extensão
        # inteira da nuvem: sem isso sobra área vazia em volta do produto.
        minx = max(minx, min(view.west for view in views))
        maxx = min(maxx, max(view.east for view in views))
        miny = max(miny, min(view.south for view in views))
        maxy = min(maxy, max(view.north for view in views))
        width = max(1, int(math.ceil((maxx - minx) / gsd)))
        height = max(1, int(math.ceil((maxy - miny) / gsd)))
        ctx.log(f"tela recortada para {width}x{height} px pela cobertura útil das fotos")

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

        # Três bandas RGB, como entregam os softwares fotogramétricos: a área sem
        # cobertura vai na máscara interna do GeoTIFF, não em uma quarta banda.
        # Assim o QGIS abre o arquivo como Banda 1/2/3 (Red, Green, Blue) e ainda
        # respeita a transparência.
        with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True), rasterio.open(
            ortho_path, "w", driver="GTiff", height=height, width=width, count=3,
            dtype="uint8", crs=f"EPSG:{epsg}", transform=transform_affine,
            compress="deflate", predictor=2, tiled=True, blockxsize=512, blockysize=512,
            BIGTIFF="YES", num_threads="ALL_CPUS",
        ) as dst:
            dst.colorinterp = [
                rasterio.enums.ColorInterp.red, rasterio.enums.ColorInterp.green,
                rasterio.enums.ColorInterp.blue,
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
                        sampled, weight = view.sample(
                            world, (tile_h, tile_w), cache, max_incidence_cos
                        )
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
                    dst.write_mask((covered * 255).astype(np.uint8), window=window)

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
                "native_gsd_cm": round(native_gsd * 100, 2),
                "quality": preset["label"],
                "off_nadir_deg": round(median_off_nadir, 1),
                "max_incidence_deg": round(max_incidence_deg, 1),
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
