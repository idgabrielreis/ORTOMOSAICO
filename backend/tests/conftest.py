"""Fixtures dos testes: um voo sintético em várias subpastas."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# O storage precisa ser definido ANTES de qualquer import de `app`: a
# configuração é lida uma única vez, no import do módulo.
_STORAGE = Path(tempfile.mkdtemp(prefix="ortomosaico-tests-"))
os.environ["ORTO_STORAGE_ROOT"] = str(_STORAGE)
os.environ.setdefault("ORTO_QUEUE_BACKEND", "local")


@pytest.fixture(scope="session")
def storage_root() -> Path:
    return _STORAGE


@pytest.fixture(scope="session")
def flight_dir(tmp_path_factory) -> Path:
    """Voo pequeno, com subpastas, imagens corrompidas e duplicadas."""
    from tools.generate_sample_flight import generate

    out = tmp_path_factory.mktemp("VOO_TESTE")
    generate(out, parts=3, rows=2, cols=3, altitude=110.0, image_size=(480, 360))
    return out


@pytest.fixture()
def db_session():
    from app.db import init_db, session_scope

    init_db()
    with session_scope() as session:
        yield session
