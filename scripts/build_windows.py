"""Build the Windows no-install package (see docs/PACKAGING.md).

    uv sync --all-groups
    uv run python scripts/build_windows.py [--ffmpeg auto|download|PATH] [--js-runtime auto|deno|node|download|PATH]

Steps: PyInstaller (packaging/ultimate-playlist.spec) -> dist/UltimatePlaylist/, then bin/ with
ffmpeg.exe, ffprobe.exe and deno.exe or node.exe (each one is run once to prove it works), the
license texts (bin/*-LICENSE.txt, bin/licenses/<package>/, bin/licenses/python-runtime/),
bin/THIRD_PARTY_NOTICES.txt, README.md, LICENSE, START-HERE.txt, and finally
dist/UltimatePlaylist-<version>-windows-x64.zip.

"download" fetches the official builds over HTTPS (meant for the release workflow on GitHub's
runners); "auto" copies whatever is installed on this machine. A license text is always the
file upstream published: found next to the tool, or fetched at the exact version that ships
(Node's LICENSE is the aggregate of the V8 / OpenSSL / ICU notices its binary requires; a
hand-written MIT text would not do). --js-runtime-license PATH supplies it offline.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import logging
import platform
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

log = logging.getLogger("build_windows")

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SPEC = ROOT / "packaging" / "ultimate-playlist.spec"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_NAME = "UltimatePlaylist"
PACKAGE_DIR = DIST / APP_NAME
EXE = PACKAGE_DIR / f"{APP_NAME}.exe"
BIN_DIR = PACKAGE_DIR / "bin"
LICENSES_DIR_NAME = "licenses"  # bin/licenses/<package>/<file>
# PyInstaller keeps its work files in <workpath>/<spec file stem>/ (build/ultimate-playlist/).
BUNDLED_MODULES_FILE = BUILD / SPEC.stem / "bundled-modules.txt"
DOWNLOADS = BUILD / "downloads"

REPO_URL = "https://github.com/Serhikko/ultimate-playlist"
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
# gyan.dev publishes a bare SHA-256 digest next to every build (a .sha256 file); the 100+ MB
# GPL zip is verified against it, like the Deno zip against Deno's .sha256sum asset.
FFMPEG_SHA256_URL = FFMPEG_URL + ".sha256"
# Pinned so that two builds of the same tag ship the same runtime, and verified against the
# .sha256sum asset Deno publishes next to every release zip.
DENO_VERSION = "2.9.6"
DENO_URL = (
    "https://github.com/denoland/deno/releases/download/"
    f"v{DENO_VERSION}/deno-x86_64-pc-windows-msvc.zip"
)
DENO_SHA256_URL = DENO_URL + ".sha256sum"
USER_AGENT = "ultimate-playlist-build (+https://github.com)"
JS_RUNTIMES = ("deno", "node")
TOOL_TIMEOUT = 20  # seconds for one `-version` / `--version` run
SUPPORTED_MACHINES = ("amd64", "x86_64")  # the zip is named -x64; refuse to mislabel an ARM build

# The upstream license files, at the exact version that ships. The Deno zip holds only
# deno.exe and the Node MSI installs no LICENSE, so when none sits next to the exe it is fetched
# from the tagged source tree. The build never writes a license text of its own: Node's LICENSE
# is the aggregate of the V8, OpenSSL (Apache-2.0), ICU, libuv, zlib, c-ares, nghttp2, brotli
# ... notices that node.exe statically contains, which no hand-written MIT text could replace.
JS_LICENSE_URLS = {
    "deno": "https://raw.githubusercontent.com/denoland/deno/{tag}/LICENSE.md",
    "node": "https://raw.githubusercontent.com/nodejs/node/{tag}/LICENSE",
}
JS_LICENSE_PAGES = {
    "deno": "https://github.com/denoland/deno/blob/{tag}/LICENSE.md",
    "node": "https://github.com/nodejs/node/blob/{tag}/LICENSE",
}
DENO_LICENSE_URL = JS_LICENSE_URLS["deno"].format(tag=f"v{DENO_VERSION}")
LICENSE_FILE_RE = re.compile(r"^(LICEN[CS]E|COPYING|NOTICE|AUTHORS)", re.IGNORECASE)

# Libraries python-build-standalone links into the interpreter in _internal/ whose license texts
# CPython's own LICENSE.txt (bin/python-LICENSE.txt: PSF, Microsoft terms, bzip2, zstd, Tcl/Tk)
# does not all carry. The texts live in packaging/licenses/ (see the README there) and are
# copied to bin/licenses/python-runtime/ by populate_bin(). {pydll} is python3xx.dll.
PACKAGING_LICENSES = ROOT / "packaging" / "licenses"
RUNTIME_LICENSES_DIR_NAME = "python-runtime"  # bin/licenses/python-runtime/<file>
PYTHON_RUNTIME_LICENSES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    # (component, files in _internal/, license, files in packaging/licenses/)
    (
        "OpenSSL 3",
        "libcrypto-3-x64.dll, libssl-3-x64.dll (used by _ssl.pyd, _hashlib.pyd)",
        "Apache-2.0",
        ("openssl-LICENSE.txt",),
    ),
    ("libffi", "libffi-8.dll (used by _ctypes.pyd)", "MIT", ("libffi-LICENSE.txt",)),
    ("Expat", "pyexpat.pyd, _elementtree.pyd", "MIT", ("expat-COPYING.txt",)),
    ("mpdecimal (libmpdec)", "_decimal.pyd", "BSD-2-Clause", ("mpdecimal-LICENSE.txt",)),
    ("XZ Utils (liblzma)", "_lzma.pyd", "0BSD", ("xz-COPYING.txt", "xz-COPYING.0BSD.txt")),
    ("zlib", "statically linked into {pydll} (the zlib module)", "zlib", ("zlib-LICENSE.txt",)),
    ("Zstandard", "_zstd.pyd", "BSD-3-Clause", ("zstd-LICENSE.txt",)),
    ("bzip2", "_bz2.pyd", "bzip2 license", ("bzip2-LICENSE.txt",)),
)
# Top-level DLLs in _internal/ that the table above or CPython's own license covers; any other
# DLL there is reported so its license can be added before the zip goes out.
KNOWN_RUNTIME_DLL_RE = re.compile(
    r"^(python3\d*|vcruntime\d+(_\d+)?|msvcp\d+|ucrtbase|api-ms-win-.*|libcrypto-.*|libssl-.*"
    r"|libffi-.*|sqlite3|zlib.*|liblzma.*|libzstd.*|libexpat.*|libmpdec.*|libbz2.*|tcl.*|tk.*)\.dll$",
    re.IGNORECASE,
)


class BuildError(RuntimeError):
    """A step failed; the message is printed without a traceback."""


# -- helpers -----------------------------------------------------------------------------------


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def folder_size(path: Path) -> tuple[int, int]:
    """(bytes, files) of everything under `path`."""
    total = count = 0
    for file in path.rglob("*"):
        if file.is_file():
            total += file.stat().st_size
            count += 1
    return total, count


def run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    log.info("$ %s", " ".join(cmd))
    return subprocess.run(cmd, check=False, text=True, **kwargs)  # type: ignore[call-overload]


def first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def check_tool(exe: Path, args: Iterable[str]) -> str:
    """Run `exe args` and return its output; a tool that cannot run must never reach a user.

    Raises BuildError when the file cannot be executed, hangs, exits non-zero or prints nothing,
    so a stub, a wrong-architecture binary or a broken download stops the build here.
    """
    cmd = [str(exe), *args]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=TOOL_TIMEOUT, check=False)
    except subprocess.TimeoutExpired as exc:
        raise BuildError(
            f"{exe} did not answer `{' '.join(args)}` within {TOOL_TIMEOUT} s"
        ) from exc
    except OSError as exc:
        raise BuildError(f"Cannot run {exe}: {exc}") from exc
    text = ((out.stdout or "") + (out.stderr or "")).strip()
    if out.returncode != 0:
        raise BuildError(
            f"{exe} exited with code {out.returncode} on `{' '.join(args)}`: {first_line(text)}"
        )
    if not text:
        raise BuildError(f"{exe} printed nothing on `{' '.join(args)}`")
    return text


def app_version() -> str:
    """``ultimate_playlist.__version__``: what the exe, /api/status and the tag check report.

    pyproject.toml's version (the installed metadata) is a hand-maintained copy; the zip name
    and the notices would otherwise carry a different number from the program inside, so a
    mismatch stops the build before anything is packaged.
    """
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    from ultimate_playlist import __version__

    try:
        installed: str | None = importlib.metadata.version("ultimate-playlist")
    except importlib.metadata.PackageNotFoundError:
        installed = None
    if installed is not None and installed != __version__:
        raise BuildError(
            f"pyproject.toml says {installed} but ultimate_playlist.__version__ is "
            f"{__version__}; bump both (and run uv sync)"
        )
    return __version__


def which_resolved(name: str) -> Path | None:
    """`shutil.which` plus symlink resolution (winget installs tools through a Links folder)."""
    found = shutil.which(name)
    if not found:
        return None
    try:
        return Path(found).resolve(strict=True)
    except OSError:
        return Path(found)


def sibling(exe: Path, name: str) -> Path | None:
    candidate = exe.with_name(name)
    return candidate if candidate.is_file() else None


def license_next_to(exe: Path) -> Path | None:
    """LICENSE / LICENSE.txt in the exe's folder or its parent (ffmpeg builds, Node installs)."""
    for folder in (exe.parent, exe.parent.parent):
        for name in ("LICENSE", "LICENSE.txt", "LICENSE.md"):
            candidate = folder / name
            if candidate.is_file():
                return candidate
    return None


# -- filesystem steps that fail with one readable line ------------------------------------------


def _in_use_hint() -> str:
    return (
        f"Is {APP_NAME}.exe (or its ffmpeg) still running, or is the antivirus still scanning "
        "the new files? Close it and retry."
    )


def remove_tree(path: Path) -> None:
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise BuildError(f"Cannot remove {path}: {exc}. {_in_use_hint()}") from exc


def remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise BuildError(f"Cannot remove {path}: {exc}. {_in_use_hint()}") from exc


def copy_file(src: Path, dest: Path) -> None:
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    except OSError as exc:
        raise BuildError(f"Cannot copy {src} to {dest}: {exc}. {_in_use_hint()}") from exc


def write_text(path: Path, text: str, newline: str | None = None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline=newline)
    except OSError as exc:
        raise BuildError(f"Cannot write {path}: {exc}") from exc


# -- downloads (release workflow) --------------------------------------------------------------


def download(url: str, dest: Path) -> Path:
    """Fetch `url` to `dest`, printing the size. Any HTTP or network error aborts the build."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("Downloading %s", url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=120) as response, dest.open("wb") as out:
            status = getattr(response, "status", 200)
            if status != 200:
                raise BuildError(f"Download failed: HTTP {status} for {url}")
            expected = response.headers.get("Content-Length")
            shutil.copyfileobj(response, out, length=1024 * 1024)
    except urllib.error.HTTPError as exc:
        raise BuildError(f"Download failed: HTTP {exc.code} {exc.reason} for {url}") from exc
    except urllib.error.URLError as exc:
        raise BuildError(f"Download failed: {exc.reason} for {url}") from exc
    except OSError as exc:
        raise BuildError(f"Download failed: {exc} for {url}") from exc
    size = dest.stat().st_size
    if expected and int(expected) != size:
        raise BuildError(f"Download truncated: got {size} of {expected} bytes for {url}")
    if size == 0:
        raise BuildError(f"Download is empty: {url}")
    log.info("Downloaded %s (%s)", dest.name, human(size))
    return dest


def verify_sha256(archive: Path, checksum_file: Path) -> str:
    """Compare `archive` with the digest in a `sha256sum`-style file; BuildError on mismatch."""
    text = checksum_file.read_text("utf-8", errors="replace")
    match = re.search(r"\b([0-9a-fA-F]{64})\b", text)
    if not match:
        raise BuildError(f"{checksum_file.name} holds no SHA-256 digest: {first_line(text)!r}")
    expected = match.group(1).lower()
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    if actual != expected:
        raise BuildError(f"SHA-256 mismatch for {archive.name}: expected {expected}, got {actual}")
    log.info("SHA-256 verified for %s", archive.name)
    return actual


def extract_members(archive: Path, wanted: dict[str, str], dest_dir: Path) -> dict[str, Path]:
    """Extract the first archive member matching each regex in `wanted` (key -> pattern).

    Returns key -> extracted file path (flat, named after the member's basename). Keys whose
    pattern matches nothing are left out; the caller decides which ones are required.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    found: dict[str, Path] = {}
    with zipfile.ZipFile(archive) as zf:
        names = sorted(zf.namelist())
        for key, pattern in wanted.items():
            regex = re.compile(pattern)
            for name in names:
                if name.endswith("/") or not regex.search(name):
                    continue
                target = dest_dir / Path(name).name
                with zf.open(name) as src, target.open("wb") as out:
                    shutil.copyfileobj(src, out)
                found[key] = target
                log.info("Extracted %s -> %s (%s)", name, target, human(target.stat().st_size))
                break
    return found


# -- tools --------------------------------------------------------------------------------------


def locate_ffmpeg(spec: str) -> tuple[Path, Path, Path | None]:
    """Return (ffmpeg.exe, ffprobe.exe, license file or None) for `--ffmpeg auto|download|PATH`."""
    if spec == "download":
        archive = download(FFMPEG_URL, DOWNLOADS / "ffmpeg-release-essentials.zip")
        checksum = download(FFMPEG_SHA256_URL, DOWNLOADS / "ffmpeg-release-essentials.zip.sha256")
        verify_sha256(archive, checksum)
        found = extract_members(
            archive,
            {
                "ffmpeg": r"(^|/)bin/ffmpeg\.exe$",
                "ffprobe": r"(^|/)bin/ffprobe\.exe$",
                "license": r"^[^/]+/LICENSE(\.txt)?$",
            },
            DOWNLOADS / "ffmpeg",
        )
        missing = [k for k in ("ffmpeg", "ffprobe") if k not in found]
        if missing:
            raise BuildError(f"{archive.name} does not contain {', '.join(missing)}.exe")
        return found["ffmpeg"], found["ffprobe"], found.get("license")

    if spec == "auto":
        ffmpeg = which_resolved("ffmpeg")
        if ffmpeg is None:
            raise BuildError(
                "ffmpeg not found on PATH. Install it (winget install Gyan.FFmpeg), pass "
                "--ffmpeg <path to ffmpeg.exe>, or use --ffmpeg download."
            )
        ffprobe = sibling(ffmpeg, "ffprobe.exe") or which_resolved("ffprobe")
        if ffprobe is None:
            raise BuildError(f"ffprobe.exe not found next to {ffmpeg} nor on PATH")
        return ffmpeg, ffprobe, license_next_to(ffmpeg)

    given = Path(spec).expanduser()
    if given.is_dir():
        for candidate in (given / "ffmpeg.exe", given / "bin" / "ffmpeg.exe"):
            if candidate.is_file():
                given = candidate
                break
    if not given.is_file():
        raise BuildError(f"--ffmpeg: {given} is not a file")
    ffmpeg = given.resolve()
    # An explicit build must be shipped as the pair it came with: a stray ffprobe from PATH
    # belongs to another version and would be silently mismatched.
    ffprobe = sibling(ffmpeg, "ffprobe.exe")
    if ffprobe is None:
        raise BuildError(f"--ffmpeg: ffprobe.exe must sit next to {ffmpeg}")
    return ffmpeg, ffprobe, license_next_to(ffmpeg)


class JsRuntime(NamedTuple):
    kind: str  # "deno" or "node"
    exe: Path
    license_file: Path  # upstream's LICENSE / LICENSE.md, never a text written by the build
    tag: str  # the upstream git tag of the shipped version ("v24.16.0"), for the notices


def js_version_tag(kind: str, version_output: str) -> str:
    """The upstream tag of a runtime from its `--version` line: node prints "v24.16.0", deno
    "deno 2.9.6 (stable, release, x86_64-pc-windows-msvc)"; both are tagged vX.Y.Z."""
    match = re.search(r"\d+\.\d+\.\d+", first_line(version_output))
    if not match:
        raise BuildError(f"cannot read the {kind} version from {first_line(version_output)!r}")
    return f"v{match.group(0)}"


def js_license_for(kind: str, exe: Path, tag: str, override: str | None) -> Path:
    """The license text to ship with `exe`: `--js-runtime-license`, else the LICENSE next to the
    exe (a Node .zip install, a Deno checkout), else upstream's file at `tag`.

    Node's LICENSE aggregates the V8 / OpenSSL / ICU / libuv / zlib ... notices its binary
    requires; a build must never substitute a text upstream did not publish, so when nothing is
    available offline the download is the only way and its failure stops the build.
    """
    if override:
        given = Path(override).expanduser()
        if not given.is_file():
            raise BuildError(f"--js-runtime-license: {given} is not a file")
        return given.resolve()
    found = license_next_to(exe)
    if found is not None:
        return found
    url = JS_LICENSE_URLS[kind].format(tag=tag)
    try:
        return download(url, DOWNLOADS / kind / tag / Path(url).name)
    except BuildError as exc:
        raise BuildError(
            f"{exc}. {exe.name} has no LICENSE next to it (the Node MSI installs none) and "
            f"upstream's copy for {tag} could not be fetched; install {kind} from the .zip "
            "build, which contains LICENSE, or pass --js-runtime-license <path>."
        ) from exc


def locate_js_runtime(spec: str, license_override: str | None = None) -> JsRuntime:
    """Resolve `--js-runtime auto|deno|node|download|PATH` (plus `--js-runtime-license`)."""
    if spec == "download":
        archive = download(DENO_URL, DOWNLOADS / "deno-x86_64-pc-windows-msvc.zip")
        checksum = download(
            DENO_SHA256_URL, DOWNLOADS / "deno-x86_64-pc-windows-msvc.zip.sha256sum"
        )
        verify_sha256(archive, checksum)
        found = extract_members(archive, {"deno": r"(^|/)deno\.exe$"}, DOWNLOADS / "deno")
        if "deno" not in found:
            raise BuildError(f"{archive.name} does not contain deno.exe")
        tag = f"v{DENO_VERSION}"
        license_file = js_license_for("deno", found["deno"], tag, license_override)
        return JsRuntime("deno", found["deno"], license_file, tag)

    if spec in ("auto", *JS_RUNTIMES):
        names = JS_RUNTIMES if spec == "auto" else (spec,)
        for name in names:
            exe = which_resolved(name)
            if exe is not None:
                kind, found_exe = name, exe
                break
        else:
            raise BuildError(
                f"No JavaScript runtime ({' / '.join(names)}) found on PATH. Install Deno "
                "(winget install DenoLand.Deno) or Node.js, pass --js-runtime <path to the exe>, "
                "or use --js-runtime download."
            )
    else:
        given = Path(spec).expanduser()
        if not given.is_file():
            raise BuildError(f"--js-runtime: {given} is not a file")
        kind = given.stem.lower()
        if kind not in JS_RUNTIMES:
            raise BuildError(f"--js-runtime: expected deno.exe or node.exe, got {given.name}")
        found_exe = given.resolve()
    tag = js_version_tag(kind, check_tool(found_exe, ["--version"]))
    return JsRuntime(kind, found_exe, js_license_for(kind, found_exe, tag, license_override), tag)


def ffmpeg_license_kind(version_output: str) -> tuple[str, str]:
    """(license name, text URL) from the `configuration:` line `ffmpeg -version` prints.

    `--enable-gpl` + `--enable-version3` (gyan.dev, BtbN default) is GPL v3; GPL alone is v2+;
    `--enable-version3` alone is LGPL v3; neither is LGPL v2.1+. A `--enable-nonfree` build
    (libfdk-aac, some CUDA SDK builds) may not be redistributed at all, so it stops the build.
    """
    flags = set(re.findall(r"--enable-\S+", version_output))
    if "--enable-nonfree" in flags:
        raise BuildError(
            "this ffmpeg build is configured with --enable-nonfree and may not be redistributed; "
            "use the gyan.dev / BtbN GPL builds (--ffmpeg download)"
        )
    gpl, v3 = "--enable-gpl" in flags, "--enable-version3" in flags
    if gpl and v3:
        return "GNU General Public License v3", "https://www.gnu.org/licenses/gpl-3.0.txt"
    if gpl:
        return "GNU General Public License v2 or later", "https://www.gnu.org/licenses/gpl-2.0.txt"
    if v3:
        return "GNU Lesser General Public License v3", "https://www.gnu.org/licenses/lgpl-3.0.txt"
    return (
        "GNU Lesser General Public License v2.1 or later",
        "https://www.gnu.org/licenses/lgpl-2.1.txt",
    )


# -- notices ------------------------------------------------------------------------------------


def bundled_top_levels() -> set[str] | None:
    if not BUNDLED_MODULES_FILE.is_file():
        return None
    names = {line.strip() for line in BUNDLED_MODULES_FILE.read_text("utf-8").splitlines()}
    return {n for n in names if n}


def distribution_top_levels(dist: importlib.metadata.Distribution) -> set[str]:
    names: set[str] = set()
    top_level = dist.read_text("top_level.txt")
    if top_level:
        names.update(line.strip() for line in top_level.splitlines() if line.strip())
    for file in dist.files or []:
        first = file.parts[0] if file.parts else ""
        if not first or first.startswith("..") or first.endswith((".dist-info", ".pth")):
            continue
        if first.endswith(".py"):
            first = first[:-3]
        elif "." in first:  # foo.cp312-win_amd64.pyd, bar.libs
            first = first.split(".", 1)[0]
        names.add(first)
    return names


def license_of(dist: importlib.metadata.Distribution) -> str:
    meta = dist.metadata
    expression = meta.get("License-Expression")
    if expression:
        return str(expression)
    for classifier in meta.get_all("Classifier") or []:
        if classifier.startswith("License ::"):
            return classifier.split("::")[-1].strip()
    text = (meta.get("License") or "").strip()
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first[:80] if first else "see the project page"


def homepage_of(dist: importlib.metadata.Distribution) -> str:
    meta = dist.metadata
    if meta.get("Home-page"):
        return str(meta["Home-page"])
    urls = {
        label.strip().lower(): url.strip()
        for label, _, url in (entry.partition(",") for entry in (meta.get_all("Project-URL") or []))
    }
    # The project page, not its changelog: charset-normalizer and urllib3 list "Code" and
    # "Documentation" next to "Changelog", and the changelog must not win.
    for key in (
        "homepage",
        "home",
        "source",
        "source code",
        "repository",
        "code",
        "github",
        "documentation",
        "changelog",
    ):
        if key in urls:
            return urls[key]
    return next(iter(urls.values()), "https://pypi.org/project/" + dist.metadata["Name"] + "/")


def shipped_distributions() -> list[importlib.metadata.Distribution]:
    """Installed distributions whose modules ended up in the PyInstaller bundle.

    The spec writes the exact list of bundled top-level modules; without it the notices could
    only be guessed, and a legal notice that names the wrong packages is worse than no build.
    """
    bundled = bundled_top_levels()
    if bundled is None:
        raise BuildError(
            f"{BUNDLED_MODULES_FILE} is missing; run without --skip-pyinstaller so the notices "
            "match the bundle"
        )
    picked: dict[str, importlib.metadata.Distribution] = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        if not name or name.lower() in picked:
            continue
        if distribution_top_levels(dist) & bundled and name.lower() != "ultimate-playlist":
            picked[name.lower()] = dist
    return [picked[k] for k in sorted(picked)]


def package_license_files(dist: importlib.metadata.Distribution) -> list[tuple[Path, Path]]:
    """(source file, path relative to the package's licenses folder) for every license text.

    Modern wheels keep them under `<name>.dist-info/licenses/` (any depth: httptools ships
    `licenses/vendor/llhttp/LICENSE`); older ones put `LICENSE*`, `COPYING*`, `NOTICE*` or
    `AUTHORS*` directly in the dist-info folder.
    """
    found: list[tuple[Path, Path]] = []
    files = dist.files
    if files is None:  # no RECORD (an egg-info install): look at the folder itself
        folder = Path(str(getattr(dist, "_path", "")))
        if folder.is_dir():
            for path in sorted(folder.rglob("*")):
                rel = path.relative_to(folder)
                inside_licenses = rel.parts[0] == LICENSES_DIR_NAME and len(rel.parts) > 1
                if path.is_file() and (
                    inside_licenses or (len(rel.parts) == 1 and LICENSE_FILE_RE.match(rel.name))
                ):
                    found.append((path, Path(*rel.parts[1:]) if inside_licenses else rel))
        return found
    for file in files:
        parts = file.parts
        if len(parts) < 2 or not parts[0].endswith(".dist-info"):
            continue
        if parts[1] == LICENSES_DIR_NAME and len(parts) > 2:
            rel = Path(*parts[2:])
        elif len(parts) == 2 and LICENSE_FILE_RE.match(parts[1]):
            rel = Path(parts[1])
        else:
            continue
        source = Path(str(dist.locate_file(file)))
        if source.is_file():
            found.append((source, rel))
    return found


def copy_package_licenses(
    dists: Iterable[importlib.metadata.Distribution], dest: Path
) -> dict[str, list[str]]:
    """Copy every package's license texts to `dest/<Name>/`; returns Name -> package-relative paths.

    A package without any license file gets a one-line pointer to its project page instead, so
    the notices never reference a file that is not there.
    """
    copied: dict[str, list[str]] = {}
    for dist in dists:
        name = str(dist.metadata["Name"])
        folder = dest / name
        targets: list[str] = []
        for source, rel in package_license_files(dist):
            copy_file(source, folder / rel)
            targets.append(f"bin/{LICENSES_DIR_NAME}/{name}/{rel.as_posix()}")
        if not targets:
            pointer = folder / "LICENSE-NOT-INCLUDED.txt"
            write_text(
                pointer,
                f"{name} {dist.version} ships no license file in its Python package.\n"
                f"License: {license_of(dist)}\nProject page: {homepage_of(dist)}\n",
            )
            targets.append(f"bin/{LICENSES_DIR_NAME}/{name}/{pointer.name}")
        copied[name] = targets
    return copied


def copy_runtime_licenses(dest: Path) -> dict[str, list[str]]:
    """Copy packaging/licenses/ to `dest` (bin/licenses/python-runtime/); component -> paths.

    Every file PYTHON_RUNTIME_LICENSES names must exist in the repository: a component without
    its text would leave a "Full license text:" line pointing at nothing.
    """
    copied: dict[str, list[str]] = {}
    for component, _files, _license, names in PYTHON_RUNTIME_LICENSES:
        targets: list[str] = []
        for name in names:
            source = PACKAGING_LICENSES / name
            if not source.is_file():
                raise BuildError(
                    f"{source} is missing: the license text of {component} must be in the "
                    "repository (see packaging/licenses/README.md)"
                )
            copy_file(source, dest / name)
            targets.append(f"bin/{LICENSES_DIR_NAME}/{RUNTIME_LICENSES_DIR_NAME}/{name}")
        copied[component] = targets
    return copied


def unknown_runtime_dlls(internal_dir: Path) -> list[str]:
    """Top-level DLLs in _internal/ that neither PYTHON_RUNTIME_LICENSES nor CPython's own
    license accounts for (a new Python may add one); logged so the table can be extended."""
    if not internal_dir.is_dir():
        return []
    return sorted(
        p.name
        for p in internal_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".dll" and not KNOWN_RUNTIME_DLL_RE.match(p.name)
    )


def write_notices(
    version: str,
    *,
    ffmpeg_output: str,
    ffmpeg_license: tuple[str, str],
    ffmpeg_license_file: str | None,
    js_kind: str,
    js_output: str,
    js_license_file: str,
    js_tag: str,
    python_license_file: str | None,
    runtime_licenses: dict[str, list[str]],
    distributions: Iterable[importlib.metadata.Distribution],
    package_licenses: dict[str, list[str]],
) -> Path:
    """Write bin/THIRD_PARTY_NOTICES.txt; every "Full license text:" line names a shipped file."""
    license_name, license_url = ffmpeg_license
    lines: list[str] = [
        f"Ultimate Playlist {version} - third-party software in this package",
        "=" * 72,
        "",
        "Ultimate Playlist itself is MIT licensed (see LICENSE next to UltimatePlaylist.exe).",
        "This folder also ships the programs and libraries below. They keep their own licenses.",
        "",
        "Bundled programs (bin/)",
        "-" * 72,
        "",
        "ffmpeg.exe, ffprobe.exe",
        f"  Build bundled: {first_line(ffmpeg_output)}",
        f"  License: {license_name} (read from the build's configuration flags: FFmpeg",
        "  itself is LGPL v2.1+; a build with --enable-gpl, such as the gyan.dev and BtbN",
        "  Windows builds with libx264, is GPL, and --enable-version3 makes it version 3).",
        "  ffmpeg is run as a separate program; Ultimate Playlist does not link to it.",
        "  Source code: https://ffmpeg.org/download.html (release tarballs at",
        "  https://ffmpeg.org/releases/) and https://github.com/BtbN/FFmpeg-Builds",
        "  Build recipes: https://www.gyan.dev/ffmpeg/builds/ and https://github.com/BtbN/FFmpeg-Builds",
    ]
    if ffmpeg_license_file:
        lines.append(f"  Full license text: {ffmpeg_license_file}")
    lines.append(f"  License text online: {license_url}")
    lines.append("")
    js_page = JS_LICENSE_PAGES[js_kind].format(tag=js_tag)
    if js_kind == "deno":
        lines += [
            "deno.exe",
            f"  Version bundled: {first_line(js_output)}",
            "  License: MIT (the copyright notice is in the license file below).",
            f"  https://deno.com  -  {js_page}",
            "  (deno.exe statically includes V8 and Rust crates under their own permissive",
            "  licenses; see the Deno repository for the complete list.)",
        ]
    else:
        lines += [
            "node.exe",
            f"  Version bundled: {first_line(js_output)}",
            "  License: MIT for Node.js itself; node.exe also contains V8, OpenSSL (Apache-2.0),",
            "  ICU, libuv, zlib, c-ares, nghttp2, brotli and others under their own licenses.",
            "  The license file below is Node's own LICENSE, which lists every one of them",
            "  with its notice.",
            f"  https://nodejs.org  -  {js_page}",
        ]
    lines += [
        f"  Full license text: {js_license_file}",
        "  Used by yt-dlp to run YouTube's player JavaScript (see yt-dlp's documentation).",
        "",
        "Python runtime (_internal/)",
        "-" * 72,
        "",
        f"Python {platform.python_version()}",
        "  License: Python Software Foundation License (PSF-2.0).",
        "  https://www.python.org  -  https://docs.python.org/3/license.html",
    ]
    if python_license_file:
        lines.append(f"  Full license text: {python_license_file}")
        lines.append(
            "  (that file also carries the Microsoft Distributable Code terms for the Visual C++"
        )
        lines.append("  runtime DLLs, VCRUNTIME140*.dll, plus the bzip2, zstd and Tcl/Tk notices)")
    lines += [
        "",
        "Libraries linked into the Python runtime in _internal/, each under its own license:",
    ]
    pydll = f"python{sys.version_info[0]}{sys.version_info[1]}.dll"
    for component, files, license_name_, _names in PYTHON_RUNTIME_LICENSES:
        lines.append(f"  {component}: {files.format(pydll=pydll)}")
        lines.append(f"    License: {license_name_}")
        texts = runtime_licenses.get(component) or []
        if texts:
            lines.append(f"    Full license text: {', '.join(texts)}")
    lines += [
        "  SQLite: sqlite3.dll, _sqlite3.pyd",
        "    License: public domain (https://sqlite.org/copyright.html), no license text.",
        "  Online: https://docs.python.org/3/license.html#licenses-and-acknowledgements-for-incorporated-software",
        "",
        f"PyInstaller {importlib.metadata.version('pyinstaller')} (bootloader inside UltimatePlaylist.exe)",
        "  License: GPL v2 or later with the Bootloader Exception, which allows this bundle",
        "  to be distributed under Ultimate Playlist's own license.",
        "  https://pyinstaller.org  -  https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt",
        "",
        "Python packages (_internal/)",
        "-" * 72,
        "",
    ]
    for dist in distributions:
        name = str(dist.metadata["Name"])
        lines.append(f"{name} {dist.version}")
        lines.append(f"  License: {license_of(dist)}")
        lines.append(f"  {homepage_of(dist)}")
        texts = package_licenses.get(name) or []
        if texts:
            lines.append(f"  Full license text: {', '.join(texts)}")
        lines.append("")
    lines += [
        "Notes",
        "-" * 72,
        "",
        "- yt-dlp is released under The Unlicense (public domain).",
        "- mutagen is GPL-2.0-or-later; it is used unmodified. Source: https://github.com/quodlibet/mutagen",
        "- Package licenses are read from the installed package metadata at build time. The",
        f"  complete license texts (and NOTICE files) are in bin/{LICENSES_DIR_NAME}/<package>/,",
        "  copied from the packages as installed, and at the linked project pages.",
        "",
    ]
    path = BIN_DIR / "THIRD_PARTY_NOTICES.txt"
    write_text(path, "\n".join(lines))
    return path


# -- package files ------------------------------------------------------------------------------


START_HERE = """Ultimate Playlist {version} for Windows - nothing to install

1. If you are reading this inside the zip, extract it first (right-click the zip > Extract All),
   then double-click UltimatePlaylist.exe in the extracted folder.
2. A console window opens and your browser opens the app at http://127.0.0.1:8765
   (the window shows the address; if another program already uses that port the next free one
   is used). Double-clicking UltimatePlaylist.exe again while the app is running only reopens
   it in your browser.
3. Close the console window to stop the app.

If Windows shows "Windows protected your PC" (SmartScreen), click "More info" and then
"Run anyway". The warning appears because the program is not signed with a paid code-signing
certificate, not because anything is wrong with it.

If something does not work:
- Every download suddenly fails: YouTube changed something. Download the newest zip from
  {repo}/releases and replace this folder.
- To see what is wrong, open a terminal in this folder (right-click an empty spot in the
  folder > "Open in Terminal"; on Windows 10: Shift + right-click > "Open PowerShell window
  here") and run:  .\\UltimatePlaylist.exe doctor
- If doctor says ffmpeg or the JavaScript runtime is missing, the bin folder is incomplete:
  extract the zip again (and check your antivirus quarantine).
- UltimatePlaylist.exe disappears, or Windows Security reports a threat: a false positive that
  is common for unsigned programs built with PyInstaller. Open Windows Security > Protection
  history, choose Restore (or Allow), then run it again.

Everything the app needs is inside this folder (ffmpeg and a JavaScript runtime are in bin/).
To uninstall, delete the folder. Your music is in your Music folder, in a sub-folder called
"Ultimate Playlist", and your settings and log are in C:\\Users\\<you>\\.ultimate-playlist; both
stay where they are. To remove every trace, also delete C:\\Users\\<you>\\.ultimate-playlist
(settings, index, log). The MP3s in Music\\Ultimate Playlist are ordinary files: keep or delete
them as you like.
"""


def package_readme_text(text: str) -> str:
    """README.md as shipped in the zip: repository-relative links become absolute GitHub URLs."""
    return text.replace("](../../releases)", f"]({REPO_URL}/releases)").replace(
        "](docs/", f"]({REPO_URL}/blob/main/docs/"
    )


def copy_package_files(version: str) -> None:
    write_text(
        PACKAGE_DIR / "README.md", package_readme_text((ROOT / "README.md").read_text("utf-8"))
    )
    copy_file(ROOT / "LICENSE", PACKAGE_DIR / "LICENSE")
    write_text(
        PACKAGE_DIR / "START-HERE.txt",
        START_HERE.format(version=version, repo=REPO_URL),
        newline="\r\n",
    )


def run_pyinstaller() -> None:
    remove_tree(PACKAGE_DIR)
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC),
        "--noconfirm",
        "--clean",
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD),
    ]
    result = run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        raise BuildError(f"PyInstaller failed with exit code {result.returncode}")


def populate_bin(
    ffmpeg_spec: str, js_spec: str, version: str, js_license_spec: str | None = None
) -> None:
    ffmpeg, ffprobe, ffmpeg_license = locate_ffmpeg(ffmpeg_spec)
    runtime = locate_js_runtime(js_spec, js_license_spec)
    js_kind, js_exe, js_license = runtime.kind, runtime.exe, runtime.license_file
    remove_tree(BIN_DIR)
    BIN_DIR.mkdir(parents=True)
    for src, dest_name in (
        (ffmpeg, "ffmpeg.exe"),
        (ffprobe, "ffprobe.exe"),
        (js_exe, f"{js_kind}.exe"),
    ):
        dest = BIN_DIR / dest_name
        copy_file(src, dest)
        log.info("Bundled %s <- %s (%s)", dest_name, src, human(dest.stat().st_size))
    # Run the copies, not the originals: this is what the user gets.
    ffmpeg_output = check_tool(BIN_DIR / "ffmpeg.exe", ["-version"])
    log.info("bin/ffmpeg.exe:  %s", first_line(ffmpeg_output))
    log.info("bin/ffprobe.exe: %s", first_line(check_tool(BIN_DIR / "ffprobe.exe", ["-version"])))
    js_output = check_tool(BIN_DIR / f"{js_kind}.exe", ["--version"])
    log.info("bin/%s.exe: %s", js_kind, first_line(js_output))
    ffmpeg_license_kind_ = ffmpeg_license_kind(ffmpeg_output)
    log.info("ffmpeg license: %s", ffmpeg_license_kind_[0])

    ffmpeg_license_file: str | None = None
    if ffmpeg_license is not None:
        copy_file(ffmpeg_license, BIN_DIR / "ffmpeg-LICENSE.txt")
        ffmpeg_license_file = "bin/ffmpeg-LICENSE.txt"
    js_license_target = BIN_DIR / f"{js_kind}-LICENSE.txt"
    copy_file(js_license, js_license_target)
    log.info("bin/%s <- %s (%s)", js_license_target.name, js_license, runtime.tag)
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    python_license_file: str | None = None
    if python_license.is_file():
        copy_file(python_license, BIN_DIR / "python-LICENSE.txt")
        python_license_file = "bin/python-LICENSE.txt"
    else:
        log.warning("%s not found; the PSF license text is not bundled", python_license)
    runtime_licenses = copy_runtime_licenses(
        BIN_DIR / LICENSES_DIR_NAME / RUNTIME_LICENSES_DIR_NAME
    )
    for name in unknown_runtime_dlls(PACKAGE_DIR / "_internal"):
        log.warning(
            "_internal/%s is not covered by PYTHON_RUNTIME_LICENSES; add its license text to "
            "packaging/licenses/ and the table before releasing",
            name,
        )

    distributions = shipped_distributions()
    package_licenses = copy_package_licenses(distributions, BIN_DIR / LICENSES_DIR_NAME)
    log.info(
        "Copied license texts of %d packages to bin/%s/", len(package_licenses), LICENSES_DIR_NAME
    )
    notices = write_notices(
        version,
        ffmpeg_output=ffmpeg_output,
        ffmpeg_license=ffmpeg_license_kind_,
        ffmpeg_license_file=ffmpeg_license_file,
        js_kind=js_kind,
        js_output=js_output,
        js_license_file=f"bin/{js_license_target.name}",
        js_tag=runtime.tag,
        python_license_file=python_license_file,
        runtime_licenses=runtime_licenses,
        distributions=distributions,
        package_licenses=package_licenses,
    )
    log.info("Wrote %s", notices)


def make_zip(version: str) -> Path:
    zip_path = DIST / f"{APP_NAME}-{version}-windows-x64.zip"
    remove_file(zip_path)
    files = sorted(p for p in PACKAGE_DIR.rglob("*") if p.is_file())
    try:
        with zipfile.ZipFile(
            zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as zf:
            for file in files:
                arcname = f"{APP_NAME}/{file.relative_to(PACKAGE_DIR).as_posix()}"
                zf.write(file, arcname)
    except OSError as exc:
        raise BuildError(f"Cannot write {zip_path}: {exc}. {_in_use_hint()}") from exc
    return zip_path


# -- main ---------------------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--ffmpeg",
        default="auto",
        metavar="auto|download|PATH",
        help="auto: copy ffmpeg/ffprobe found on PATH (default); download: fetch the official "
        "gyan.dev essentials build; PATH: ffmpeg.exe (ffprobe.exe must sit next to it)",
    )
    parser.add_argument(
        "--js-runtime",
        default="auto",
        metavar="auto|deno|node|download|PATH",
        help="auto: first of deno/node on PATH (default); deno / node: that one from PATH; "
        f"download: fetch the official Deno {DENO_VERSION} build; PATH: deno.exe or node.exe",
    )
    parser.add_argument(
        "--js-runtime-license",
        default=None,
        metavar="PATH",
        help="the runtime's upstream LICENSE file to ship, for offline builds; by default the "
        "LICENSE next to the exe is used, else upstream's file is fetched at the exact version",
    )
    parser.add_argument(
        "--skip-pyinstaller",
        action="store_true",
        help="reuse dist/UltimatePlaylist from a previous run (only bin/, docs and zip are redone)",
    )
    parser.add_argument(
        "--no-zip", action="store_true", help="stop after filling dist/UltimatePlaylist"
    )
    parser.add_argument(
        "--version-suffix",
        default="",
        metavar="TEXT",
        help="appended to the version in the zip name; write it as --version-suffix=-dev42 "
        "(a separate '-dev42' argument is taken for an option)",
    )
    return parser.parse_args(argv)


def check_platform() -> None:
    if sys.platform != "win32":
        raise BuildError("This script builds the Windows package and must run on Windows.")
    machine = platform.machine().lower()
    if machine not in SUPPORTED_MACHINES:
        raise BuildError(
            f"This script builds the x64 package; this Python runs on {machine or 'unknown'} "
            "and would produce a zip that is mislabelled -windows-x64."
        )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args(argv)
    try:
        check_platform()
        version = app_version()
        log.info("Building %s %s (Python %s)", APP_NAME, version, platform.python_version())
        if args.skip_pyinstaller:
            log.info("Skipping PyInstaller (--skip-pyinstaller)")
        else:
            run_pyinstaller()
        if not EXE.is_file():
            raise BuildError(f"{EXE} is missing; PyInstaller did not produce the app")
        populate_bin(args.ffmpeg, args.js_runtime, version, args.js_runtime_license)
        copy_package_files(version)
        total, count = folder_size(PACKAGE_DIR)
        log.info("")
        log.info("Package folder: %s (%s in %d files)", PACKAGE_DIR, human(total), count)
        if args.no_zip:
            log.info("Zip skipped (--no-zip)")
            return 0
        zip_path = make_zip(version + args.version_suffix)
        log.info("Zip:            %s (%s)", zip_path, human(zip_path.stat().st_size))
    except BuildError as exc:
        log.error("ERROR: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
