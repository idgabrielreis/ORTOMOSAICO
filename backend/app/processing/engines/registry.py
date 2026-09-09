"""Registro dos motores disponíveis."""
from __future__ import annotations

from ...config import settings
from .direct import DirectGeoreferencingEngine
from .odm import ODMEngine

_ENGINES = {
    "odm": ODMEngine(),
    "direct": DirectGeoreferencingEngine(),
}


def get_engine(name: str):
    try:
        return _ENGINES[name]
    except KeyError:
        raise ValueError(f"motor desconhecido: {name}") from None


def available_engines() -> list[dict]:
    out = []
    for name, engine in _ENGINES.items():
        available, reason = engine.availability()
        out.append({
            "name": name,
            "description": engine.description,
            "precision": engine.precision,
            "available": available,
            "reason": reason,
        })
    return out


def resolve_engine(requested: str | None):
    """`auto` escolhe o motor de precisão quando ele estiver disponível."""
    requested = requested or settings.default_engine
    if requested != "auto":
        return get_engine(requested)
    odm = _ENGINES["odm"]
    if odm.availability()[0]:
        return odm
    return _ENGINES["direct"]
