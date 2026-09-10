"""Sessão e engine do SQLAlchemy."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

_connect_args = {"check_same_thread": False} if settings.sqlalchemy_url.startswith("sqlite") else {}

engine = create_engine(settings.sqlalchemy_url, future=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    """Dependência do FastAPI."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Sessão para uso fora do request (workers)."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  (registra as tabelas)

    models.Base.metadata.create_all(engine)
    _add_missing_columns(models.Base)


def _add_missing_columns(base) -> None:
    """Migração mínima: cria colunas novas em tabelas que já existem.

    `create_all` só cria tabelas ausentes, então um banco de uma versão
    anterior ficaria sem as colunas adicionadas depois. Alembic entra quando
    houver mudança que exija transformar dados; para colunas novas e anuláveis
    um ALTER TABLE direto resolve, e mantém o protótipo rodando sem passo extra.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table in base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                column_type = column.type.compile(engine.dialect)
                connection.execute(
                    text(f'ALTER TABLE {table.name} ADD COLUMN "{column.name}" {column_type}')
                )
