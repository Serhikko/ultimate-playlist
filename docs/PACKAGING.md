# Packaging: the Windows no-install build

For people who will not install uv, Python, ffmpeg or Node: one zip, unzip anywhere, double-click
`UltimatePlaylist.exe`. This page is for whoever builds and releases that zip.

## What is in the zip

```text
UltimatePlaylist/
  UltimatePlaylist.exe          the app (console window: shows the URL, close it to stop)
  START-HERE.txt                three lines for the user + the SmartScreen note
  README.md, LICENSE
  bin/
    ffmpeg.exe, ffprobe.exe     converts to MP3 and embeds the cover art
    deno.exe  (or node.exe)     JavaScript runtime for yt-dlp
    THIRD_PARTY_NOTICES.txt     licenses of everything above and of every Python package shipped;
                                every "Full license text:" line names a file in this folder
    ffmpeg-LICENSE.txt          the GPL text shipped with the ffmpeg build (gyan.dev builds have one)
    deno-LICENSE.txt /          upstream's own license file for the runtime that ships: Deno's
    node-LICENSE.txt            LICENSE.md, or Node's LICENSE (the aggregate of the V8, OpenSSL,
                                ICU, libuv, zlib ... notices node.exe contains). A LICENSE found
                                next to the exe is used (auto / explicit mode); otherwise it is
                                fetched from the upstream repository at the exact shipped version,
                                or passed in with --js-runtime-license. Never written from a
                                template.
    python-LICENSE.txt          the PSF license (copied from the interpreter that froze the app;
                                it also carries the Microsoft, bzip2, zstd and Tcl/Tk terms)
    licenses/python-runtime/    the upstream texts of the libraries linked into the interpreter
                                (OpenSSL 3, libffi, Expat, mpdecimal, xz, zlib, zstd, bzip2),
                                copied from packaging/licenses/ (see the README there)
    licenses/<package>/...      the license / NOTICE / COPYING files of every shipped Python
                                package, copied from the installed dist-info folders
  _internal/                    PyInstaller runtime: python3xx.dll, the standard library, every
                                package, and the web UI under ultimate_playlist/server/static/
```

Why these tools are bundled: YouTube requires a JavaScript runtime to work out audio URLs
(yt-dlp runs the player's JavaScript through Deno or Node), and ffmpeg does the MP3 conversion
and the cover embedding. Neither is a Python package, so PyInstaller cannot pick them up; the
build script copies them into `bin/`. The app looks in `<folder of the exe>/bin` before `PATH`
(`src/ultimate_playlist/bundled.py`), so the package works on a machine with nothing installed
and a user who already has a newer ffmpeg does not get it picked up by accident.
`ULTIMATE_PLAYLIST_BIN=<folder>` makes a source checkout use the same lookup (a folder that does
not exist is ignored; the folder next to the exe is the fallback), which is how the bundled path
is tested without building. In the packaged build only the runtimes that are actually in `bin/`
are handed to yt-dlp: yt-dlp prefers Deno over Node whatever the configured order, so a Deno
installed on the user's machine would otherwise win over the bundled `node.exe`. From source
every configured runtime stays enabled.

Everything the app writes stays outside the folder: music in `~/Music/Ultimate Playlist`, config
and log in `~/.ultimate-playlist` (or `ULTIMATE_PLAYLIST_HOME`). Deleting the folder uninstalls.

## Build it locally

You need Windows, uv, and ffmpeg + Deno or Node on `PATH` (the same tools the app needs to run
from source; `winget install Gyan.FFmpeg DenoLand.Deno`).

```text
uv sync --all-groups
uv run python scripts/build_windows.py
```

Output: `dist/UltimatePlaylist/` and `dist/UltimatePlaylist-<version>-windows-x64.zip`. The
script prints the folder size, the zip path and the zip size at the end. Options:

| Flag | Meaning |
| --- | --- |
| `--ffmpeg auto` (default) | copy `ffmpeg.exe` found on `PATH` (symlinks such as winget's `Links` folder are resolved) plus the `ffprobe.exe` next to it |
| `--ffmpeg download` | fetch the official gyan.dev "essentials" build and verify it against the `.sha256` digest gyan.dev publishes next to it (the release workflow uses this) |
| `--ffmpeg C:\path\to\ffmpeg.exe` | use that build (`ffprobe.exe` must sit next to it) |
| `--js-runtime auto` (default) | first of `deno`, `node` found on `PATH` |
| `--js-runtime deno` / `node` | insist on that one |
| `--js-runtime download` | fetch the official Deno build from GitHub, pinned to `DENO_VERSION` in the script and checked against the `.sha256sum` asset Deno publishes (release workflow) |
| `--js-runtime C:\path\to\deno.exe` | use that exe (`deno.exe` or `node.exe`) |
| `--js-runtime-license C:\path\to\LICENSE` | the runtime's upstream license file to ship, for offline builds (by default the LICENSE next to the exe is used, else upstream's file at the shipped version is fetched; the build stops when neither is possible) |
| `--skip-pyinstaller` | reuse `dist/UltimatePlaylist/` from the previous run; only `bin/`, the docs and the zip are redone |
| `--no-zip` | stop after filling the folder |
| `--version-suffix=-dev3` | appended to the version in the zip name (the `=` form: argparse takes a bare `-dev3` for an option) |

What the script does, in order:

1. `python -m PyInstaller packaging/ultimate-playlist.spec --noconfirm --clean --distpath dist --workpath build`
   (with the interpreter of the current venv, so `uv run` matters).
2. Checks that `dist/UltimatePlaylist/UltimatePlaylist.exe` exists.
3. Creates `bin/`, copies ffmpeg, ffprobe and the JavaScript runtime, then runs each copy
   (`-version` / `--version`): a tool that cannot execute, exits non-zero or prints nothing
   stops the build, so a stub or a mismatched download never reaches a user. An explicit
   `--ffmpeg PATH` must have its `ffprobe.exe` next to it (no pairing with a different ffprobe
   from `PATH`). The ffmpeg license is read from the `configuration:` flags the tool prints
   (`--enable-gpl` + `--enable-version3` = GPL v3, GPL alone = GPL v2+, `--enable-version3`
   alone = LGPL v3, neither = LGPL v2.1+); a `--enable-nonfree` build is refused because it may
   not be redistributed.
4. Writes the license texts: `ffmpeg-LICENSE.txt`, `deno-LICENSE.txt` / `node-LICENSE.txt`
   (upstream's file: found next to the exe, fetched at the shipped version, or given with
   `--js-runtime-license`), `python-LICENSE.txt`, `licenses/python-runtime/` (from
   `packaging/licenses/`), and `licenses/<package>/` for every shipped Python package (copied
   from the installed dist-info folders; a package without one gets a pointer file). A top-level
   DLL in `_internal/` that the runtime table does not cover is reported as a warning. Then
   `THIRD_PARTY_NOTICES.txt`: tool versions from the runs above, package licenses from the
   installed metadata, and only packages that actually ended up in the bundle (the spec writes
   that list to `build/ultimate-playlist/bundled-modules.txt`; when the file is missing the
   build stops instead of guessing).
5. Copies `README.md` (repository-relative links rewritten to absolute GitHub URLs) and
   `LICENSE`, writes `START-HERE.txt`.
6. Zips the folder (sorted entries, forward slashes, top-level folder `UltimatePlaylist/`).

A file in use (the exe from the last smoke test still running, an ffmpeg child, Defender still
scanning the fresh binaries) makes the script stop with one line saying which file and to close
the app, instead of a traceback. The script refuses to run on anything but an x64 Windows
Python, because the zip is named `-windows-x64`, and stops when `pyproject.toml` and
`ultimate_playlist.__version__` disagree (the exe, `/api/status` and the tag check read the
latter; the zip name and the notices must not say something else).

### The spec (`packaging/ultimate-playlist.spec`)

- Entry script `packaging/entry.py` (calls `ultimate_playlist.cli.main`; on a non-zero exit it
  prints one line and waits for Enter when the process owns its console window, so a
  double-clicked exe that fails to start does not vanish unread), `pathex` includes `src/`,
  onedir, `console=True`, no UPX, icon generated at build time by `packaging/make_icon.py`
  (pure standard library, so nothing to install and no binary in git), and a version resource
  (product name, file version from `ultimate_playlist.__version__`) so Properties > Details,
  Task Manager and the SmartScreen pane show a name.
- `setuptools`, `_distutils_hack` and `pkg_resources` are excluded: the venv's
  `distutils-precedence.pth` would otherwise pull 130 setuptools modules (and `packaging`) into
  a bundle that never imports them.
- `datas = collect_data_files("ultimate_playlist", includes=["server/static/*"])` puts the web
  UI where `server/app.py` expects it (`Path(__file__).parent / "static"`).
- Hidden imports: `ultimate_playlist.providers.youtube` and `.spotify` (the registry imports them
  with `importlib`, which static analysis cannot follow: forget these and the frozen app has no
  providers; the Spotify helper modules `spotify_meta.py` and `ytmusic_match.py` are imported by
  `spotify.py` the normal way and need no entry), plus the uvicorn loop / protocol / lifespan
  modules uvicorn selects by name.
- yt-dlp ships its own PyInstaller hook (websockets, requests, urllib3, pycryptodomex, certifi,
  and the `yt_dlp_ejs` JavaScript solver files); pyinstaller-hooks-contrib covers uvicorn, anyio
  and pydantic. Test-only packages are excluded.

If a new dependency does not show up in the bundle, add it to `hiddenimports` (modules) or
`datas` (files) in the spec and rebuild. `build/ultimate-playlist/warn-ultimate-playlist.txt`
lists what PyInstaller could not find.

### Smoke test

PowerShell (the default shell on Windows 11):

```powershell
$env:ULTIMATE_PLAYLIST_HOME = "$env:TEMP\up-smoke"
dist\UltimatePlaylist\UltimatePlaylist.exe --version
dist\UltimatePlaylist\UltimatePlaylist.exe doctor
dist\UltimatePlaylist\UltimatePlaylist.exe serve --no-browser --port 8791
```

(cmd.exe: `set ULTIMATE_PLAYLIST_HOME=%TEMP%\up-smoke` instead of the first line.)

then open <http://127.0.0.1:8791/> (the UI must render: proves the static files are in the
bundle), `/api/version` and `/api/status`. `doctor` must print the bundled `bin\ffmpeg.exe` and
`bin\deno.exe` (or `node.exe`), not something from `PATH`.

## Cutting a release

```text
# bump version in pyproject.toml and src/ultimate_playlist/__init__.py, commit, then:
git tag v0.2.0
git push --tags
```

`.github/workflows/release.yml` runs on `windows-latest`: `uv sync --all-groups`, the build
script with `--ffmpeg download --js-runtime download` (official gyan.dev ffmpeg essentials build
verified against its `.sha256`, and the pinned Deno release verified against its `.sha256sum`),
a smoke test (`--version` and `doctor` must exit 0, then `serve` is started in the background
with a trap that kills it and prints its log whatever happens, and `/` must answer with the
HTML page, `/api/version` and `/api/status` with JSON, which proves the web UI is in the
bundle), then `gh release create <tag> dist/*.zip --generate-notes` (or
`gh release upload --clobber` when the release already exists). The tag must equal `v` +
`ultimate_playlist.__version__`, otherwise the workflow stops before building. Running the
workflow by hand (workflow_dispatch) builds the same zip with a `-devNN` suffix and keeps it as a
workflow artifact; the tag check and the publish step run only for a tag push, so a manual run
started from a tag never touches the real release.

The frozen app uses Python 3.14, the same interpreter the test matrix in `ci.yml` covers and
`.python-version` pins for local `uv run` builds, so a local zip matches the released one.

## SmartScreen

The exe is not code-signed (signing certificates cost money and need identity checks). Windows
shows "Windows protected your PC" the first time; the user clicks "More info" then "Run anyway".
`START-HERE.txt` and the README say so. If a certificate ever becomes available, sign
`dist/UltimatePlaylist/UltimatePlaylist.exe` (and the bundled exes if you like) between steps 2
and 5 of the build script with `signtool`.

Unsigned PyInstaller executables are also regularly flagged by Windows Defender and third-party
antivirus as generic trojans ("Wacatac" and similar names) right after extraction: the exe vanishes
into quarantine or "is not a valid Win32 application". `START-HERE.txt` and the README tell the
user to restore it from Windows Security > Protection history. If a release gets flagged, submit
the zip at <https://www.microsoft.com/en-us/wdsi/filesubmission> as a false positive; a signed
exe would mostly avoid it.

## Size

The tools are most of it: the gyan.dev / BtbN ffmpeg binaries are statically linked, so
`ffmpeg.exe` and `ffprobe.exe` are each 80-220 MB depending on the build ("essentials" is the
smaller one, which is why the release workflow uses it); Deno is about 45 MB, Node about 90 MB;
Python plus every package in `_internal/` is about 45 MB. Measured on a local build with the
winget gyan.dev "full" build and Node: 566 MB unzipped, 228 MB zipped. Expect the release build
(essentials + Deno) to come in around 250-300 MB unzipped and 100-130 MB zipped. The script
prints the exact numbers at the end of every build.

## Updating yt-dlp (YouTube broke again)

The zip contains the yt-dlp version that was installed when it was built. When YouTube changes
something, update the lock file and rebuild:

```text
uv lock --upgrade-package yt-dlp
uv sync --all-groups
uv run pytest -q
git commit -am "Update yt-dlp" && git tag v0.2.1 && git push && git push --tags
```

The same rebuild refreshes ffmpeg, because `--ffmpeg download` always takes the current
gyan.dev release build. Deno is pinned (`DENO_VERSION` in `scripts/build_windows.py`, verified
against the `.sha256sum` asset of that release): bump the constant to move to a newer Deno.
Users update by downloading the new zip and deleting the old folder; their music and settings
are outside it.

## Licenses in the bundle

- Ultimate Playlist: MIT.
- ffmpeg (gyan.dev / BtbN builds): GPL v3, detected from the build's configuration flags at
  build time (an LGPL build is labelled as such; a `--enable-nonfree` build is refused); run as
  a separate process, source at ffmpeg.org, build recipes at gyan.dev and
  github.com/BtbN/FFmpeg-Builds. The GPL text ships as `bin/ffmpeg-LICENSE.txt` when the build
  includes it.
- Deno / Node.js: MIT for the project itself, but both binaries statically contain V8 and, for
  Node, OpenSSL (Apache-2.0: the license text must accompany a redistribution), ICU, libuv,
  zlib, c-ares, nghttp2, brotli and more, each with a notice that has to travel with the
  binary. Upstream's own license file is the aggregate of those notices, so that file - and
  only that file - ships as `bin/deno-LICENSE.txt` / `bin/node-LICENSE.txt`: the LICENSE next
  to the exe when there is one (the Node .zip build, a Deno checkout), otherwise the file from
  the upstream repository at the exact shipped version (the Node MSI, the winget install,
  installs no LICENSE), or the one passed with `--js-runtime-license`. Rule: the build never
  writes a license text from a template; a package can never contain a text upstream did not
  publish, and when no upstream text is available the build stops.
- Python: PSF (`bin/python-LICENSE.txt`, which also carries the Microsoft Distributable Code
  terms for `VCRUNTIME140*.dll` plus bzip2, zstd and Tcl/Tk). The libraries linked into the
  interpreter that that file does not cover - OpenSSL 3 (`libcrypto-3-x64.dll`,
  `libssl-3-x64.dll`, Apache-2.0), libffi (MIT), Expat (MIT), mpdecimal (BSD-2), xz/liblzma
  (0BSD), zlib, plus zstd and bzip2 again - ship their upstream texts in
  `bin/licenses/python-runtime/`, copied from `packaging/licenses/` (the README there says
  where each file comes from and how to refresh it when the interpreter is bumped). SQLite is
  public domain.
- yt-dlp: Unlicense. mutagen: GPL-2.0-or-later (used unmodified). PyInstaller's bootloader: GPL
  with the bootloader exception. Every shipped Python package is listed with its license in
  `bin/THIRD_PARTY_NOTICES.txt`, generated at build time, and its license text is in
  `bin/licenses/<package>/`.
