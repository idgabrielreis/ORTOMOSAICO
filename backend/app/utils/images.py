"""Leitura leve de imagens (validação, thumbnails)."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = False
Image.MAX_IMAGE_PIXELS = None  # ortofotos e imagens de drone passam do limite padrão

MAGIC = {
    b"\xff\xd8\xff": "jpeg",
    b"II*\x00": "tiff",
    b"MM\x00*": "tiff",
    b"\x89PNG\r\n\x1a\n": "png",
}


def sniff_format(path: Path) -> str | None:
    with path.open("rb") as fh:
        head = fh.read(12)
    for magic, name in MAGIC.items():
        if head.startswith(magic):
            return name
    return None


def make_thumbnail(src: Path, dst: Path, size: int = 256) -> bool:
    """Thumbnail com `draft()`: o JPEG é decodificado já reduzido, gastando
    uma fração da memória de uma decodificação completa."""
    try:
        with Image.open(src) as im:
            im.draft("RGB", (size, size))
            im = im.convert("RGB")
            im.thumbnail((size, size))
            dst.parent.mkdir(parents=True, exist_ok=True)
            im.save(dst, "JPEG", quality=82)
        return True
    except Exception:
        return False
