# ruff: noqa: F821 - PyInstaller injects Analysis, PYZ, EXE, COLLECT, SPECPATH and workpath
"""PyInstaller spec for the Windows no-install build (see docs/PACKAGING.md).

Built by scripts/build_windows.py:

    python -m PyInstaller packaging/ultimate-playlist.spec --noconfirm --clean --distpath dist --workpath build

Produces dist/UltimatePlaylist/ (onedir, console app). The build script then adds bin/ with
ffmpeg and a JavaScript runtime, the notices, README/LICENSE/START-HERE.txt, and zips it.
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

# PyInstaller injects SPECPATH (folder of this file), DISTPATH and workpath into the namespace.
ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"
PACKAGING = ROOT / "packaging"
WORK = Path(workpath)

# The project need not be installed in the venv: collect_data_files imports the package and
# the version is read from it, so src/ is put on the path like pathex does for the analysis.
sys.path.insert(0, str(SRC))
sys.path.insert(0, str(PACKAGING))
from make_icon import make_icon  # noqa: E402

from ultimate_playlist import __version__  # noqa: E402

APP_NAME = "UltimatePlaylist"

# The web UI: server/app.py resolves Path(__file__).parent / "static", so the files must land at
# _internal/ultimate_playlist/server/static (collect_data_files keeps the package-relative path).
datas = collect_data_files("ultimate_playlist", includes=["server/static/*"])

hiddenimports = [
    # providers/__init__.py imports the built-in providers with importlib, which PyInstaller's
    # static analysis cannot see; without these two lines the frozen app has no providers.
    "ultimate_playlist.providers.youtube",
    "ultimate_playlist.providers.spotify",
    # uvicorn picks its loop / protocol / lifespan classes by string at runtime. The contrib hook
    # (hook-uvicorn.py) collects every uvicorn submodule too; listing the usual set here keeps
    # the build working even if that hook is missing or changes.
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "anyio._backends._asyncio",
]

# yt-dlp ships its own PyInstaller hook (yt_dlp/__pyinstaller/hook-yt_dlp.py): it adds the
# websockets / requests / urllib3 / pycryptodomex submodules, certifi's CA bundle and the
# yt_dlp_ejs JavaScript solver files. pyinstaller-hooks-contrib covers uvicorn, anyio, pydantic.

excludes = [
    "tests",
    "pytest",
    "_pytest",
    "httpx",  # dev dependency (TestClient) only
    "tkinter",
    "IPython",
    # setuptools sneaks in through the venv's distutils-precedence.pth (_distutils_hack) and
    # drags 130 modules plus `packaging` along; nothing in the app imports any of them.
    "setuptools",
    "_distutils_hack",
    "pkg_resources",
]

a = Analysis(
    [str(PACKAGING / "entry.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# Tell the build script exactly which top-level modules ended up in the bundle, so that
# bin/THIRD_PARTY_NOTICES.txt lists the packages that are actually shipped (and no others).
_bundled = set()
for _name, _path, _kind in [*a.pure, *a.binaries, *a.datas]:
    _first = str(_name).replace("\\", "/").split("/")[0].split(".")[0]
    if _first:
        _bundled.add(_first)
(WORK / "bundled-modules.txt").write_text("\n".join(sorted(_bundled)) + "\n", encoding="utf-8")

pyz = PYZ(a.pure)


def _version_tuple(version: str) -> tuple[int, int, int, int]:
    """'0.1.0' / '0.2.0rc1' -> (0, 1, 0, 0): the numeric prefix, padded or cut to four parts."""
    parts: list[int] = []
    for piece in version.split("."):
        digits = ""
        for ch in piece:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
    parts = (parts + [0, 0, 0, 0])[:4]
    return parts[0], parts[1], parts[2], parts[3]


# The version resource: Properties > Details, Task Manager and SmartScreen's "More info" pane
# show a product name and version instead of nothing (an unsigned exe looks worse without it).
_numbers = _version_tuple(__version__)
version_resource = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_numbers, prodvers=_numbers, mask=0x3F, flags=0x0, OS=0x40004),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040904B0",  # US English, Unicode
                    [
                        StringStruct("CompanyName", "Ultimate Playlist"),
                        StringStruct(
                            "FileDescription", "Ultimate Playlist - YouTube to MP3 library"
                        ),
                        StringStruct("FileVersion", __version__),
                        StringStruct("InternalName", APP_NAME),
                        StringStruct("LegalCopyright", "MIT License - see LICENSE"),
                        StringStruct("OriginalFilename", f"{APP_NAME}.exe"),
                        StringStruct("ProductName", "Ultimate Playlist"),
                        StringStruct("ProductVersion", __version__),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [0x0409, 0x04B0])]),
    ],
)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # the user sees the URL and closes the window to stop the server
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=make_icon(WORK / "icon.ico"),
    version=version_resource,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
