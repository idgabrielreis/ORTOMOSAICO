"""Presets de qualidade do processamento.

A escolha é uma só, feita pelo usuário na criação do projeto, e vale para todos
os motores. O que ela controla:

* quanto detalhe é usado para detectar características (afeta o alinhamento);
* quantos pares de imagens são comparados (afeta tempo e robustez);
* o GSD do ortomosaico em relação ao GSD nativo do voo (afeta o produto final).

Na qualidade alta o ortomosaico sai no GSD nativo das fotos: nada de resolução
é descartado, e o preço é tempo de processamento.
"""
from __future__ import annotations

QUALITY_PRESETS: dict[str, dict] = {
    "alta": {
        "label": "Alta",
        "summary": "Mantém a resolução nativa das fotos. Mais lento.",
        "feature_max_size": 3200,
        "max_features": 16384,
        "neighbors": 16,
        "gsd_factor": 1.0,
        "odm_quality": "high",
    },
    "media": {
        "label": "Média",
        "summary": "Equilíbrio entre detalhe e tempo. Ortomosaico com metade da resolução.",
        "feature_max_size": 2000,
        "max_features": 8192,
        "neighbors": 12,
        "gsd_factor": 2.0,
        "odm_quality": "medium",
    },
    "baixa": {
        "label": "Baixa",
        "summary": "Prévia rápida do voo. Um quarto da resolução.",
        "feature_max_size": 1400,
        "max_features": 4096,
        "neighbors": 8,
        "gsd_factor": 4.0,
        "odm_quality": "low",
    },
}

DEFAULT_QUALITY = "alta"

# Nomes antigos e equivalentes em inglês continuam aceitos.
ALIASES = {
    "high": "alta", "ultra": "alta", "máxima": "alta", "maxima": "alta",
    "medium": "media", "média": "media",
    "low": "baixa", "lowest": "baixa", "rápida": "baixa", "rapida": "baixa",
}


def resolve_quality(value: str | None) -> str:
    if not value:
        return DEFAULT_QUALITY
    key = value.strip().lower()
    key = ALIASES.get(key, key)
    return key if key in QUALITY_PRESETS else DEFAULT_QUALITY


def preset(value: str | None) -> dict:
    return QUALITY_PRESETS[resolve_quality(value)]


def options() -> list[dict]:
    return [{"value": key, **data} for key, data in QUALITY_PRESETS.items()]
