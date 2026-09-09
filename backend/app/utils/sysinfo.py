"""Métricas de máquina exibidas na tela de processamento."""
from __future__ import annotations

import shutil
import subprocess

try:  # psutil é opcional em ambientes mínimos
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]


def gpu_info() -> list[dict]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    gpus = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 4:
            gpus.append({
                "name": parts[0],
                "utilization_percent": float(parts[1]),
                "memory_used_mb": float(parts[2]),
                "memory_total_mb": float(parts[3]),
            })
    return gpus


def resource_usage() -> dict:
    data: dict = {"cpu_percent": None, "memory_percent": None, "memory_used_gb": None,
                  "memory_total_gb": None, "gpus": gpu_info()}
    if psutil is not None:
        vm = psutil.virtual_memory()
        data.update({
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "cpu_count": psutil.cpu_count(logical=True),
            "memory_percent": vm.percent,
            "memory_used_gb": round(vm.used / 1e9, 2),
            "memory_total_gb": round(vm.total / 1e9, 2),
        })
    return data
