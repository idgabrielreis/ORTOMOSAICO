"""A regra central: uma pasta raiz vira UM dataset, não um por subpasta."""
from __future__ import annotations

from pathlib import Path

from app.ingest.discovery import discover_images


def test_varredura_recursiva_junta_todas_as_subpastas(flight_dir: Path):
    result = discover_images(flight_dir)

    assert len(result.folders) >= 3, "as subpastas do voo deveriam ter sido percorridas"
    assert len(result.valid) == 6, "todas as imagens válidas entram no mesmo dataset"
    # Nenhuma imagem foi atribuída a um "projeto por pasta": a raiz é o dataset.
    assert {f.relative_path.split("/")[0] for f in result.valid} >= {"PARTE_01", "PARTE_02"}


def test_arquivos_corrompidos_sao_marcados_e_nao_derrubam_a_varredura(flight_dir: Path):
    result = discover_images(flight_dir)

    assert len(result.invalid) == 2
    assert all(f.invalid_reason for f in result.invalid)


def test_duplicatas_entram_uma_vez_so(flight_dir: Path):
    result = discover_images(flight_dir)

    assert len(result.duplicates) == 2
    for duplicate in result.duplicates:
        assert duplicate.duplicate_of and duplicate.duplicate_of != duplicate.relative_path


def test_diretorios_de_ruido_sao_ignorados(tmp_path: Path):
    (tmp_path / "__MACOSX").mkdir()
    (tmp_path / "__MACOSX" / "DJI_0001.JPG").write_bytes(b"\xff\xd8\xff\xe0lixo")
    (tmp_path / "PARTE_01").mkdir()

    result = discover_images(tmp_path)

    assert result.files == []


def test_symlink_circular_nao_trava(tmp_path: Path):
    (tmp_path / "PARTE_01").mkdir()
    try:
        (tmp_path / "PARTE_01" / "loop").symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        return  # sistema sem suporte a symlink

    result = discover_images(tmp_path)

    assert result.files == []
