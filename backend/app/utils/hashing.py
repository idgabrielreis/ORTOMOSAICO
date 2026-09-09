"""Hash de arquivos com custo controlado.

A deduplicação usa duas fases: uma assinatura barata (tamanho + trechos do
início e do fim) e, apenas quando duas assinaturas colidem, o SHA-256 completo.
Isso evita ler gigabytes de imagens só para descobrir que são diferentes.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK = 64 * 1024


def quick_signature(path: Path, size: int) -> str:
    """Assinatura barata: tamanho + 64 KiB do início + 64 KiB do fim."""
    h = hashlib.blake2b(digest_size=16)
    h.update(str(size).encode())
    with path.open("rb") as fh:
        h.update(fh.read(CHUNK))
        if size > 2 * CHUNK:
            fh.seek(-CHUNK, 2)
            h.update(fh.read(CHUNK))
    return h.hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()
