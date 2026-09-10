"""Configuração da aplicação, carregada de variáveis de ambiente."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ORTO_", env_file=".env", extra="ignore")

    # Armazenamento
    storage_root: Path = REPO_ROOT / "storage"

    # Banco: SQLite por padrão; aponte para postgresql+psycopg://... para usar PostGIS
    database_url: str = ""

    # Fila: "local" (thread no processo da API) ou "celery"
    queue_backend: str = "local"
    celery_broker_url: str = "redis://localhost:6379/0"

    # Motor fotogramétrico
    default_engine: str = "auto"          # auto | odm | direct
    nodeodm_url: str = ""                 # ex.: http://localhost:3000
    odm_docker_image: str = "opendronemap/odm:latest"
    odm_use_gpu: bool = False

    # Descoberta / ingestão
    scan_allowed_roots: str = ""          # lista separada por ":" (vazio = qualquer caminho legível)
    max_upload_batch_mb: int = 512
    metadata_workers: int = 0             # 0 = os.cpu_count()

    # Tolerância a erro
    min_valid_ratio: float = 0.6
    min_valid_images: int = 5

    # API
    cors_origins: str = "*"
    # Pasta com a interface compilada; vazio deixa o app procurar sozinho.
    web_dir: str = ""

    @property
    def sqlalchemy_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.storage_root.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{self.storage_root / 'ortomosaico.db'}"

    def project_dir(self, project_id: str) -> Path:
        return self.storage_root / "projects" / project_id


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    return settings


settings = get_settings()
