"""Descoberta recursiva de imagens — o núcleo do produto.

Uma pasta raiz é um voo. Todas as imagens encontradas em qualquer profundidade
abaixo dela pertencem ao MESMO dataset. Subpastas (CAMERA_01, PARTE_03, cartões
de memória) são organização física e nada mais.

A varredura é iterativa (pilha explícita), ignora symlinks e detecta ciclos por
(device, inode), de modo que árvores profundas ou com links circulares não
travam nem estouram a pilha do Python.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..utils.hashing import quick_signature, sha256_file
from ..utils.images import sniff_format

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".tif", ".tiff", ".png"}

# Arquivos que acompanham um voo RTK/PPK da DJI e viajam junto das fotos.
# Não são imagens, mas descrevem o posicionamento delas.
AUXILIARY_EXTENSIONS = {
    ".mrk": "mrk",        # evento de cada foto: posição corrigida e desvios
    ".obs": "rinex_obs",  # observações do receptor do drone
    ".nav": "rinex_nav",  # efemérides
    ".bin": "ppk_raw",    # bruto do receptor, entrada do pós-processamento
}

# Diretórios que nunca contêm imagens do voo (ruído de sistema de arquivos,
# saídas do próprio app, caches de outros softwares de fotogrametria).
IGNORED_DIRS = {
    "__macosx", ".trash", ".trashes", ".spotlight-v100", ".fseventsd",
    ".thumbs", ".thumbnails", ".cache", "$recycle.bin", "system volume information",
    "opensfm", "odm_orthophoto", "odm_georeferencing", "odm_texturing",
    "_ortomosaico", "thumbnails", "previews",
}
IGNORED_PREFIXES = ("._",)  # resource forks do macOS


@dataclass
class FoundFile:
    path: Path
    relative_path: str
    folder: str
    filename: str
    size_bytes: int
    is_valid: bool = True
    invalid_reason: str | None = None
    digest: str | None = None
    is_duplicate: bool = False
    duplicate_of: str | None = None  # relative_path do original


@dataclass
class AuxiliaryFile:
    """Arquivo de apoio ao voo (MRK, RINEX, bruto do receptor)."""

    path: Path
    relative_path: str
    folder: str
    kind: str
    size_bytes: int


@dataclass
class DiscoveryResult:
    root: Path
    roots: list[Path] = field(default_factory=list)
    files: list[FoundFile] = field(default_factory=list)
    folders: set[str] = field(default_factory=set)
    auxiliary: list[AuxiliaryFile] = field(default_factory=list)
    scanned_entries: int = 0
    skipped_extensions: dict[str, int] = field(default_factory=dict)

    def auxiliary_of(self, kind: str) -> list[AuxiliaryFile]:
        return [item for item in self.auxiliary if item.kind == kind]

    @property
    def valid(self) -> list[FoundFile]:
        return [f for f in self.files if f.is_valid and not f.is_duplicate]

    @property
    def invalid(self) -> list[FoundFile]:
        return [f for f in self.files if not f.is_valid]

    @property
    def duplicates(self) -> list[FoundFile]:
        return [f for f in self.files if f.is_duplicate]


ProgressCB = Callable[[int, int], None]  # (arquivos encontrados, entradas varridas)


def walk_image_files(root: Path) -> Iterator[tuple[Path, os.stat_result, int]]:
    """Percorre a árvore e devolve (caminho, stat, entradas_varridas).

    Iterativo e resistente a ciclos; nunca segue symlinks de diretório.
    """
    root = root.resolve()
    stack: list[Path] = [root]
    seen_dirs: set[tuple[int, int]] = set()
    scanned = 0

    while stack:
        current = stack.pop()
        try:
            st = current.stat()
        except OSError:
            continue
        key = (st.st_dev, st.st_ino)
        if key in seen_dirs:
            continue
        seen_dirs.add(key)

        try:
            entries = list(os.scandir(current))
        except (PermissionError, OSError):
            continue

        for entry in entries:
            scanned += 1
            name = entry.name
            if name.startswith(IGNORED_PREFIXES):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if name.lower() in IGNORED_DIRS:
                        continue
                    stack.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                yield Path(entry.path), entry.stat(), scanned
            except OSError:
                continue


def discover_images(
    root: Path | str,
    *,
    progress: ProgressCB | None = None,
    validate: bool = True,
    deduplicate: bool = True,
    progress_every: int = 200,
) -> DiscoveryResult:
    """Varre `root` e devolve UM dataset com todas as imagens abaixo dela."""
    return discover_dataset(
        [root], progress=progress, validate=validate,
        deduplicate=deduplicate, progress_every=progress_every,
    )


def discover_dataset(
    roots: Sequence[Path | str],
    *,
    progress: ProgressCB | None = None,
    validate: bool = True,
    deduplicate: bool = True,
    progress_every: int = 200,
) -> DiscoveryResult:
    """Junta uma ou várias pastas em UM único dataset.

    Selecionar cinco pastas não cria cinco projetos: as imagens de todas elas
    entram na mesma lista, com a mesma deduplicação, e seguem para um único
    processamento. Quando há mais de uma raiz, o nome da pasta escolhida vira
    prefixo do caminho relativo, apenas para o usuário saber de onde veio cada
    foto.
    """
    resolved: list[Path] = []
    for candidate in roots:
        path = Path(candidate).expanduser().resolve()
        if not path.is_dir():
            raise NotADirectoryError(f"{path} não é um diretório")
        if path not in resolved:
            resolved.append(path)
    if not resolved:
        raise ValueError("nenhuma pasta selecionada")

    result = DiscoveryResult(root=resolved[0], roots=resolved)
    by_signature: dict[str, FoundFile] = {}
    multiple = len(resolved) > 1

    for root in resolved:
        _scan_root(
            root, result, by_signature,
            prefix=root.name if multiple else "",
            progress=progress, validate=validate,
            deduplicate=deduplicate, progress_every=progress_every,
        )

    if progress:
        progress(len(result.files), result.scanned_entries)
    return result


def _scan_root(
    root: Path,
    result: DiscoveryResult,
    by_signature: dict[str, FoundFile],
    *,
    prefix: str,
    progress: ProgressCB | None,
    validate: bool,
    deduplicate: bool,
    progress_every: int,
) -> None:
    base_scanned = result.scanned_entries

    for path, st, scanned in walk_image_files(root):
        result.scanned_entries = base_scanned + scanned
        ext = path.suffix.lower()
        if ext not in IMAGE_EXTENSIONS:
            rel_aux = _relative(path, root, prefix)
            kind = AUXILIARY_EXTENSIONS.get(ext)
            if kind:
                result.auxiliary.append(
                    AuxiliaryFile(
                        path=path,
                        relative_path=rel_aux,
                        folder=str(Path(rel_aux).parent) if "/" in rel_aux else ".",
                        kind=kind,
                        size_bytes=st.st_size,
                    )
                )
            elif ext:
                result.skipped_extensions[ext] = result.skipped_extensions.get(ext, 0) + 1
            continue

        rel = _relative(path, root, prefix)
        folder = str(Path(rel).parent) if "/" in rel else "."
        found = FoundFile(
            path=path,
            relative_path=rel,
            folder=folder,
            filename=path.name,
            size_bytes=st.st_size,
        )
        result.folders.add(folder)

        if validate:
            _validate(found)

        if deduplicate and found.is_valid:
            _mark_duplicate(found, by_signature)

        result.files.append(found)
        if progress and len(result.files) % progress_every == 0:
            progress(len(result.files), result.scanned_entries)


def _relative(path: Path, root: Path, prefix: str) -> str:
    relative = path.relative_to(root).as_posix()
    return f"{prefix}/{relative}" if prefix else relative


def _validate(found: FoundFile) -> None:
    """Validação barata: arquivo vazio, assinatura e integridade estrutural."""
    if found.size_bytes == 0:
        found.is_valid, found.invalid_reason = False, "arquivo vazio"
        return
    try:
        fmt = sniff_format(found.path)
    except OSError as exc:
        found.is_valid, found.invalid_reason = False, f"erro de leitura: {exc.strerror}"
        return
    if fmt is None:
        found.is_valid, found.invalid_reason = False, "assinatura de imagem não reconhecida"
        return
    try:
        from PIL import Image

        with Image.open(found.path) as im:
            im.verify()  # não decodifica os pixels, só valida a estrutura
    except Exception as exc:  # arquivo truncado, JPEG cortado no meio da cópia
        found.is_valid = False
        found.invalid_reason = f"imagem corrompida: {type(exc).__name__}"


def _mark_duplicate(found: FoundFile, by_signature: dict[str, FoundFile]) -> None:
    """Mesma imagem copiada em duas pastas entra no dataset uma única vez."""
    try:
        sig = quick_signature(found.path, found.size_bytes)
    except OSError:
        return
    candidate = by_signature.get(sig)
    if candidate is None:
        by_signature[sig] = found
        return
    # Assinatura colidiu: confirma com SHA-256 completo antes de descartar.
    if candidate.digest is None:
        candidate.digest = sha256_file(candidate.path)
    found.digest = sha256_file(found.path)
    if found.digest == candidate.digest:
        found.is_duplicate = True
        found.duplicate_of = candidate.relative_path
