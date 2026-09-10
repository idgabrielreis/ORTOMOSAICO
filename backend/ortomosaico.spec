# Empacotamento com PyInstaller.
#
#   pyinstaller ortomosaico.spec
#
# Gera dist/Ortomosaico/Ortomosaico.exe (Windows) ou dist/Ortomosaico/Ortomosaico.
# O modo pasta é proposital: o modo arquivo único descompactaria centenas de MB
# a cada abertura, deixando a inicialização lenta.
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

hidden = [
    "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on",
    "rasterio.sample", "rasterio._shim", "rasterio.vrt", "rasterio.control",
    "pyproj", "pycolmap", "cv2", "PIL.Image", "sqlalchemy.dialects.sqlite",
    "app.processing.engines.sfm", "app.processing.engines.odm",
    "app.processing.engines.direct",
    # O gancho do pkg_resources puxa jaraco.context, que depende deste pacote.
    "backports", "backports.tarfile",
]

datas = [("../frontend/out", "web")]
for package in ("rasterio", "pyproj", "pycolmap", "rio_tiler", "morecantile"):
    try:
        datas += collect_data_files(package)
    except Exception:
        pass

binaries = []
for package in ("rasterio", "pyproj", "pycolmap", "cv2"):
    try:
        binaries += collect_dynamic_libs(package)
    except Exception:
        pass

a = Analysis(
    ["run_desktop.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "pytest", "IPython", "notebook"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="Ortomosaico",
    console=True, icon=None,
)
coll = COLLECT(
    exe, a.binaries, a.datas, strip=False, upx=False, name="Ortomosaico",
)
