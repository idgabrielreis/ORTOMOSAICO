"""As 8 etapas exibidas na barra de progresso da interface."""
from __future__ import annotations

STAGES: list[tuple[int, str]] = [
    (1, "Importando imagens"),
    (2, "Lendo metadados"),
    (3, "Detectando características"),
    (4, "Encontrando correspondências"),
    (5, "Alinhando imagens"),
    (6, "Reconstruindo superfície"),
    (7, "Gerando ortomosaico"),
    (8, "Finalizando"),
]

STAGE_LABEL = dict(STAGES)


def overall_progress(stage: int, stage_fraction: float) -> float:
    """Converte (etapa, fração da etapa) em percentual global 0..100."""
    stage = min(max(stage, 1), len(STAGES))
    stage_fraction = min(max(stage_fraction, 0.0), 1.0)
    return round(((stage - 1) + stage_fraction) / len(STAGES) * 100, 1)
