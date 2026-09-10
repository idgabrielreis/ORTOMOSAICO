"""Motor ODM: adaptador para o OpenDroneMap.

Duas formas de execução, escolhidas por configuração:

* **NodeODM** (`ORTO_NODEODM_URL`): envia o dataset para um nó REST, acompanha o
  progresso e baixa os produtos. É o caminho para escalar em várias máquinas.
* **Docker local**: `docker run opendronemap/odm` sobre um diretório de projeto.

O ODM é quem faz SfM, bundle adjustment, nuvem de pontos, DSM/DTM,
ortorretificação e blending. Este módulo só prepara o dataset, traduz o log em
progresso das 8 etapas da interface e recolhe as saídas.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import zipfile
from functools import lru_cache
from pathlib import Path

from ...config import settings
from ..quality import preset as quality_preset
from .base import EngineContext, EngineResult, EngineUnavailable

# Trechos do log do ODM -> (etapa da UI, fração aproximada da etapa)
_DAEMON_CHECKED_AT = 0.0

ODM_STAGE_PATTERNS: list[tuple[re.Pattern[str], int, float]] = [
    (re.compile(r"running dataset stage", re.I), 1, 0.5),
    (re.compile(r"loading dataset|found \d+ usable images", re.I), 2, 0.5),
    (re.compile(r"running opensfm stage|detecting features|extract_metadata", re.I), 3, 0.4),
    (re.compile(r"matching|match_features", re.I), 4, 0.5),
    (re.compile(r"reconstruct|bundle|align|create_tracks", re.I), 5, 0.5),
    (re.compile(r"running openmvs stage|densify|point cloud|running odm_filterpoints", re.I), 6, 0.3),
    (re.compile(r"running odm_dem|dsm|dtm", re.I), 6, 0.8),
    (re.compile(r"running odm_orthophoto|orthophoto", re.I), 7, 0.5),
    (re.compile(r"running odm_report|compressing|post processing", re.I), 8, 0.5),
]


@lru_cache(maxsize=1)
def _docker_daemon_probe() -> bool:
    try:
        return subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, timeout=8,
        ).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def _docker_daemon_running(ttl: float = 30.0) -> bool:
    """Resultado em cache: `docker info` custa caro para chamar a cada request."""
    global _DAEMON_CHECKED_AT
    now = time.monotonic()
    if now - _DAEMON_CHECKED_AT > ttl:
        _docker_daemon_probe.cache_clear()
        _DAEMON_CHECKED_AT = now
    return _docker_daemon_probe()


def _map_progress(line: str) -> tuple[int, float] | None:
    for pattern, stage, fraction in ODM_STAGE_PATTERNS:
        if pattern.search(line):
            return stage, fraction
    return None


class ODMEngine:
    name = "odm"
    description = "OpenDroneMap: SfM, bundle adjustment, DSM e ortorretificação"
    precision = "fotogramétrica"

    def availability(self) -> tuple[bool, str]:
        if settings.nodeodm_url:
            return True, f"NodeODM em {settings.nodeodm_url}"
        if not shutil.which("docker"):
            return False, "requer Docker instalado ou ORTO_NODEODM_URL configurado"
        # O binário do docker existir não basta: sem daemon acessível o job
        # falharia só depois de enfileirado.
        if not _docker_daemon_running():
            return False, "o daemon do Docker não está acessível"
        return True, f"Docker local ({settings.odm_docker_image})"

    def run(self, ctx: EngineContext) -> EngineResult:
        available, reason = self.availability()
        if not available:
            raise EngineUnavailable(reason)
        if settings.nodeodm_url:
            return self._run_nodeodm(ctx)
        return self._run_docker(ctx)

    # ------------------------------------------------------------------ docker

    def _odm_args(self, ctx: EngineContext) -> list[str]:
        args = [
            "--project-path", "/datasets", "project",
            "--orthophoto-compression", "DEFLATE",
            "--cog",
        ]
        if ctx.target_gsd_cm:
            args += ["--orthophoto-resolution", f"{ctx.target_gsd_cm:.2f}"]
        if ctx.output_epsg:
            args += ["--force-gps"] if ctx.options.get("force_gps") else []
        quality = quality_preset(ctx.options.get("quality"))["odm_quality"]
        args += ["--feature-quality", quality, "--pc-quality", quality]
        if ctx.options.get("fast_orthophoto", True):
            # DSM a partir de malha 2.5D: bem mais rápido, precisão suficiente
            # para agricultura em terreno pouco acidentado.
            args += ["--fast-orthophoto"]
        else:
            args += ["--dsm", "--dtm"]
        if ctx.options.get("multispectral"):
            args += ["--radiometric-calibration", "camera+sun"]
        if settings.odm_use_gpu:
            args += ["--feature-type", "sift"]
        args += ["--rerun-all"] if ctx.options.get("rerun") else []
        return args

    def _run_docker(self, ctx: EngineContext) -> EngineResult:
        project_dir = ctx.work_dir / "project"
        (project_dir / "images").mkdir(parents=True, exist_ok=True)
        image = settings.odm_docker_image
        cmd = ["docker", "run", "--rm", "-v", f"{ctx.work_dir}:/datasets"]
        if settings.odm_use_gpu:
            cmd += ["--gpus", "all"]
            image = image.replace(":latest", ":gpu") if ":gpu" not in image else image
        cmd += [image, *self._odm_args(ctx)]
        ctx.log("$ " + " ".join(cmd))

        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
        )
        try:
            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip()
                ctx.log(line)
                if mapped := _map_progress(line):
                    ctx.progress(mapped[0], mapped[1], line[:120])
                if ctx.is_canceled():
                    process.terminate()
                    raise InterruptedError("cancelado pelo usuário")
        finally:
            code = process.wait()
        if code != 0:
            raise RuntimeError(f"ODM terminou com código {code}; veja o log do job")
        return self._collect(ctx, project_dir)

    # ----------------------------------------------------------------- nodeodm

    def _run_nodeodm(self, ctx: EngineContext) -> EngineResult:
        import httpx

        base = settings.nodeodm_url.rstrip("/")
        preset = quality_preset(ctx.options.get("quality"))
        options = [
            {"name": "orthophoto-resolution", "value": ctx.target_gsd_cm or 2},
            {"name": "feature-quality", "value": preset["odm_quality"]},
            {"name": "pc-quality", "value": preset["odm_quality"]},
        ]
        if ctx.options.get("fast_orthophoto", True):
            options.append({"name": "fast-orthophoto", "value": True})

        with httpx.Client(timeout=None) as client:
            ctx.progress(1, 0.1, "Enviando dataset para o NodeODM")
            files = [("images", (img.name, img.path.open("rb"))) for img in ctx.images]
            try:
                response = client.post(
                    f"{base}/task/new",
                    data={"name": ctx.project_id, "options": json.dumps(options)},
                    files=files,
                )
            finally:
                for _, (_, handle) in files:
                    handle.close()
            response.raise_for_status()
            uuid = response.json()["uuid"]
            ctx.log(f"NodeODM task {uuid}")

            while True:
                if ctx.is_canceled():
                    client.post(f"{base}/task/cancel", json={"uuid": uuid})
                    raise InterruptedError("cancelado pelo usuário")
                info = client.get(f"{base}/task/{uuid}/info").json()
                status = info.get("status", {}).get("code")
                for line in info.get("output", [])[-5:]:
                    if mapped := _map_progress(line):
                        ctx.progress(mapped[0], mapped[1], line[:120])
                if status == 40:  # COMPLETED
                    break
                if status in (30, 50):  # FAILED / CANCELED
                    raise RuntimeError(info.get("status", {}).get("errorMessage", "falha no ODM"))
                time.sleep(5)

            ctx.progress(8, 0.4, "Baixando produtos")
            archive = ctx.work_dir / "all.zip"
            with client.stream("GET", f"{base}/task/{uuid}/download/all.zip") as stream:
                stream.raise_for_status()
                with archive.open("wb") as fh:
                    for chunk in stream.iter_bytes(1024 * 1024):
                        fh.write(chunk)
        project_dir = ctx.work_dir / "project"
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(project_dir)
        archive.unlink(missing_ok=True)
        return self._collect(ctx, project_dir)

    # ----------------------------------------------------------------- saídas

    def _collect(self, ctx: EngineContext, project_dir: Path) -> EngineResult:
        import rasterio
        from rasterio.warp import transform_bounds

        ctx.output_dir.mkdir(parents=True, exist_ok=True)
        result = EngineResult()

        mapping = {
            "orthomosaic": ("odm_orthophoto/odm_orthophoto.tif", "orthomosaic.tif"),
            "dsm": ("odm_dem/dsm.tif", "dsm.tif"),
            "dtm": ("odm_dem/dtm.tif", "dtm.tif"),
            "point_cloud": ("odm_georeferencing/odm_georeferenced_model.laz", "point_cloud.laz"),
            "report": ("odm_report/report.pdf", "odm_report.pdf"),
        }
        for attr, (relative, target) in mapping.items():
            source = project_dir / relative
            if source.exists():
                destination = ctx.output_dir / target
                shutil.move(str(source), destination)
                setattr(result, attr, destination)

        if result.orthomosaic is None:
            raise RuntimeError("ODM terminou sem gerar odm_orthophoto.tif")

        with rasterio.open(result.orthomosaic) as src:
            result.epsg = src.crs.to_epsg() if src.crs else None
            result.gsd_cm = round(abs(src.transform.a) * 100, 2)
            result.bounds_wgs84 = list(
                transform_bounds(src.crs, "EPSG:4326", *src.bounds, densify_pts=21)
            )
            result.stats = {"size_px": [src.width, src.height], "bands": src.count}
        return result
