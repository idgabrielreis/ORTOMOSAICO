"""Ponto de entrada do aplicativo empacotado.

Sobe o servidor local, abre a interface no navegador padrão e fica rodando até
a janela do programa ser fechada. É o que transforma o conjunto backend +
interface em um programa de duplo clique, sem Python, Node ou Docker
instalados na máquina.
"""
from __future__ import annotations

import multiprocessing
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path


def _free_port(preferred: int = 8000) -> int:
    """Usa a porta padrão quando livre; senão pede uma qualquer ao sistema."""
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return probe.getsockname()[1]
            except OSError:
                continue
    return preferred


def _storage_root() -> Path:
    """Projetos e produtos ficam em Documentos, não dentro do programa."""
    if custom := os.environ.get("ORTO_STORAGE_ROOT"):
        return Path(custom)
    documents = Path.home() / "Documents"
    base = documents if documents.is_dir() else Path.home()
    return base / "Ortomosaico"


def main() -> None:
    # Necessário no Windows: sem isso cada processo do pool reabriria o programa.
    multiprocessing.freeze_support()

    storage = _storage_root()
    storage.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("ORTO_STORAGE_ROOT", str(storage))
    # Sem uma raiz definida, o navegador de pastas começa na casa do usuário.
    os.environ.setdefault("ORTO_SCAN_ALLOWED_ROOTS", "")

    port = int(os.environ.get("ORTO_PORT") or 0) or _free_port()
    url = f"http://127.0.0.1:{port}/"

    # flush explícito: no executável a saída fica em buffer e o usuário não veria
    # o endereço enquanto o servidor sobe.
    print("Ortomosaico", flush=True)
    print(f"  projetos e resultados em: {storage}", flush=True)
    print(f"  interface: {url}", flush=True)
    print("  feche esta janela para encerrar o programa", flush=True)

    def open_browser() -> None:
        time.sleep(1.5)
        webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()

    import uvicorn

    from .main import app

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    sys.exit(main())
