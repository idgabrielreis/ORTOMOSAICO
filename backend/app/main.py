"""Aplicação FastAPI."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import exports, jobs, projects, system
from .config import settings
from .db import init_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="Ortomosaico",
    version="0.1.0",
    summary="Gerenciador de voos de drone e geração de ortomosaicos",
    description=(
        "Uma pasta raiz é um voo. Todas as subpastas abaixo dela entram no mesmo "
        "dataset e geram um único ortomosaico georreferenciado."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(projects.router)
app.include_router(jobs.router)
app.include_router(exports.router)
app.include_router(system.router)

# Importado por último: registra rotas que dependem do rio-tiler, opcional em
# ambientes sem GDAL.
try:
    from .api import tiles

    app.include_router(tiles.router)
except ImportError as exc:  # pragma: no cover
    logging.getLogger(__name__).warning("tiles indisponíveis: %s", exc)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}
