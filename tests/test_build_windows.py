"""scripts/build_windows.py: the pure helpers (no PyInstaller run, no network, no real tools).

The script is loaded from its file so it needs no package; every test points its output folders
at tmp_path, so nothing under dist/ or build/ is touched.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_windows.py"
WINDOWS = sys.platform.startswith("win")


@pytest.fixture(scope="module")
def build() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_windows_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bin_dir(build: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "pkg" / "bin"
    folder.mkdir(parents=True)
    monkeypatch.setattr(build, "PACKAGE_DIR", folder.parent)
    monkeypatch.setattr(build, "BIN_DIR", folder)
    return folder


def make_tool(folder: Path, name: str, lines: list[str], exit_code: int = 0) -> Path:
    """An executable stub printing `lines` (a .bat on Windows, a shell script elsewhere)."""
    folder.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        path = folder / f"{name}.bat"
        body = "@echo off\r\n" + "".join(f"echo {line}\r\n" for line in lines)
        body += f"exit /b {exit_code}\r\n"
        path.write_text(body, encoding="ascii")
    else:
        path = folder / name
        body = "#!/bin/sh\n" + "".join(f'echo "{line}"\n' for line in lines) + f"exit {exit_code}\n"
        path.write_text(body, encoding="ascii")
        path.chmod(0o755)
    return path


def make_distribution(
    site: Path, name: str, version: str, license_files: dict[str, str], top_level: str = ""
) -> importlib.metadata.Distribution:
    """A real dist-info folder (METADATA + RECORD + files) read back through importlib.metadata."""
    top_level = top_level or name
    dist_info = site / f"{name}-{version}.dist-info"
    dist_info.mkdir(parents=True)
    (site / top_level).mkdir(exist_ok=True)
    (site / top_level / "__init__.py").write_text("", encoding="utf-8")
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\nLicense-Expression: MIT\n"
        f"Project-URL: Homepage, https://example.test/{name}\n",
        encoding="utf-8",
    )
    (dist_info / "top_level.txt").write_text(top_level + "\n", encoding="utf-8")
    records = [f"{top_level}/__init__.py", f"{dist_info.name}/METADATA", f"{dist_info.name}/RECORD"]
    records.append(f"{dist_info.name}/top_level.txt")
    for rel, text in license_files.items():
        target = dist_info / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        records.append(f"{dist_info.name}/{rel}")
    (dist_info / "RECORD").write_text("".join(f"{r},,\n" for r in records), encoding="utf-8")
    return importlib.metadata.PathDistribution(dist_info)


# -- argument parsing -----------------------------------------------------------------------------


def test_version_suffix_with_equals_sign(build: ModuleType) -> None:
    assert build.parse_args(["--version-suffix=-dev3"]).version_suffix == "-dev3"
    assert build.parse_args([]).version_suffix == ""
    with pytest.raises(SystemExit):  # argparse takes a bare -dev3 for an option: documented
        build.parse_args(["--version-suffix", "-dev3"])


# -- check_tool -----------------------------------------------------------------------------------


def test_app_version_refuses_a_pyproject_that_disagrees(
    build: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exe, /api/status and the tag check read __version__; the zip name must not come
    from a pyproject.toml that was bumped separately (or not at all)."""
    from ultimate_playlist import __version__

    monkeypatch.setattr(build.importlib.metadata, "version", lambda name: __version__)
    assert build.app_version() == __version__
    monkeypatch.setattr(build.importlib.metadata, "version", lambda name: "9.9.9")
    with pytest.raises(build.BuildError, match=r"pyproject.toml says 9.9.9 but .*bump both"):
        build.app_version()

    def not_installed(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(build.importlib.metadata, "version", not_installed)
    assert build.app_version() == __version__  # a bare checkout: __version__ alone


def test_check_tool_returns_the_output(build: ModuleType, tmp_path: Path) -> None:
    tool = make_tool(
        tmp_path, "ffmpeg", ["ffmpeg version 9.9.9-test", "configuration: --enable-gpl"]
    )
    out = build.check_tool(tool, ["-version"])
    assert build.first_line(out) == "ffmpeg version 9.9.9-test"
    assert "--enable-gpl" in out


def test_check_tool_rejects_a_tool_that_cannot_run(build: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(build.BuildError, match="Cannot run"):
        build.check_tool(tmp_path / "missing.exe", ["-version"])
    stub = tmp_path / "stub.exe"
    stub.write_bytes(b"MZ")
    with pytest.raises(build.BuildError):
        build.check_tool(stub, ["-version"])


def test_check_tool_rejects_failure_and_silence(build: ModuleType, tmp_path: Path) -> None:
    failing = make_tool(tmp_path / "a", "tool", ["boom"], exit_code=3)
    with pytest.raises(build.BuildError, match="exited with code 3"):
        build.check_tool(failing, ["--version"])
    silent = make_tool(tmp_path / "b", "tool", [])
    with pytest.raises(build.BuildError, match="printed nothing"):
        build.check_tool(silent, ["--version"])


# -- locate_ffmpeg --------------------------------------------------------------------------------


def test_explicit_ffmpeg_requires_its_own_ffprobe(
    build: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "ff"
    folder.mkdir()
    ffmpeg = folder / "ffmpeg.exe"
    ffmpeg.write_bytes(b"MZ")
    monkeypatch.setattr(build, "which_resolved", lambda name: tmp_path / "other" / f"{name}.exe")
    with pytest.raises(build.BuildError, match="ffprobe.exe must sit next to"):
        build.locate_ffmpeg(str(ffmpeg))
    ffprobe = folder / "ffprobe.exe"
    ffprobe.write_bytes(b"MZ")
    found_ffmpeg, found_ffprobe, license_file = build.locate_ffmpeg(str(ffmpeg))
    assert (found_ffmpeg, found_ffprobe) == (ffmpeg.resolve(), ffprobe.resolve())
    assert license_file is None
    (folder / "LICENSE").write_text("GPL", encoding="utf-8")
    assert build.locate_ffmpeg(str(folder))[2] == folder / "LICENSE"


# -- locate_js_runtime / the license text that ships with it ---------------------------------------


@pytest.mark.parametrize(
    ("kind", "output", "tag"),
    [
        ("node", "v24.16.0\n", "v24.16.0"),
        ("deno", "deno 2.9.6 (stable, release, x86_64-pc-windows-msvc)\nv8 14.0\n", "v2.9.6"),
    ],
)
def test_js_version_tag(build: ModuleType, kind: str, output: str, tag: str) -> None:
    assert build.js_version_tag(kind, output) == tag
    with pytest.raises(build.BuildError, match="cannot read the node version"):
        build.js_version_tag("node", "something without a version")


@pytest.fixture
def fake_download(build: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    """Record download() calls and write a small file; the network is never touched."""
    urls: list[str] = []

    def download(url: str, dest: Path) -> Path:
        urls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f"upstream text from {url}", encoding="utf-8")
        return dest

    monkeypatch.setattr(build, "download", download)
    monkeypatch.setattr(build, "DOWNLOADS", tmp_path / "downloads")
    return urls


def test_js_license_next_to_the_exe_is_used(
    build: ModuleType, tmp_path: Path, fake_download: list[str]
) -> None:
    exe = make_tool(tmp_path / "node", "node", ["v24.16.0"])
    (exe.parent / "LICENSE").write_text("Node.js is licensed for use as follows", "utf-8")
    runtime = build.locate_js_runtime(str(exe))
    assert runtime.kind == "node" and runtime.exe == exe.resolve()
    assert runtime.license_file == exe.parent / "LICENSE" and runtime.tag == "v24.16.0"
    assert fake_download == []


def test_js_license_is_fetched_at_the_shipped_version_when_none_is_installed(
    build: ModuleType, tmp_path: Path, fake_download: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Node MSI installs no LICENSE: upstream's file for exactly that version is fetched.
    The build never writes an MIT text of its own (Node's LICENSE is the aggregate of the V8 /
    OpenSSL / ICU notices node.exe requires)."""
    exe = make_tool(tmp_path / "nodejs", "node", ["v24.16.0"])
    monkeypatch.setattr(build, "which_resolved", lambda name: exe if name == "node" else None)
    runtime = build.locate_js_runtime("auto")
    assert runtime.kind == "node" and runtime.tag == "v24.16.0"
    assert fake_download == ["https://raw.githubusercontent.com/nodejs/node/v24.16.0/LICENSE"]
    assert runtime.license_file.name == "LICENSE" and runtime.license_file.is_file()
    assert not hasattr(build, "MIT_LICENSE") and not hasattr(build, "JS_COPYRIGHT")

    deno = make_tool(tmp_path / "deno", "deno", ["deno 2.9.6 (stable, release, x86_64)"])
    runtime = build.locate_js_runtime(str(deno))
    assert runtime.kind == "deno" and runtime.tag == "v2.9.6"
    assert fake_download[-1] == "https://raw.githubusercontent.com/denoland/deno/v2.9.6/LICENSE.md"


def test_js_license_download_failure_stops_the_build_with_the_offline_way_out(
    build: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = make_tool(tmp_path / "nodejs", "node", ["v24.16.0"])

    def refuse(url: str, dest: Path) -> Path:
        raise build.BuildError(f"Download failed: no network for {url}")

    monkeypatch.setattr(build, "download", refuse)
    with pytest.raises(build.BuildError, match="--js-runtime-license"):
        build.locate_js_runtime(str(exe))
    # the offline way out: the upstream file handed in explicitly
    given = tmp_path / "LICENSE-from-the-zip-build"
    given.write_text("Node.js is licensed for use as follows", "utf-8")
    runtime = build.locate_js_runtime(str(exe), str(given))
    assert runtime.license_file == given.resolve()
    with pytest.raises(build.BuildError, match="--js-runtime-license"):
        build.locate_js_runtime(str(exe), str(tmp_path / "missing"))
    assert build.parse_args(["--js-runtime-license", str(given)]).js_runtime_license == str(given)
    assert build.parse_args([]).js_runtime_license is None


# -- ffmpeg license detection ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("configuration", "expected"),
    [
        (
            "configuration: --enable-gpl --enable-version3 --enable-static",
            "GNU General Public License v3",
        ),
        ("configuration: --enable-gpl --enable-libx264", "GNU General Public License v2 or later"),
        (
            "configuration: --enable-version3 --enable-shared",
            "GNU Lesser General Public License v3",
        ),
        (
            "configuration: --enable-shared --disable-static",
            "GNU Lesser General Public License v2.1 or later",
        ),
        ("no configuration line at all", "GNU Lesser General Public License v2.1 or later"),
    ],
)
def test_ffmpeg_license_kind(build: ModuleType, configuration: str, expected: str) -> None:
    name, url = build.ffmpeg_license_kind(
        f"ffmpeg version 8.1.1\nbuilt with gcc\n{configuration}\n"
    )
    assert name == expected
    assert url.startswith("https://www.gnu.org/licenses/")


def test_nonfree_ffmpeg_is_refused(build: ModuleType) -> None:
    with pytest.raises(build.BuildError, match="nonfree"):
        build.ffmpeg_license_kind(
            "configuration: --enable-gpl --enable-nonfree --enable-libfdk-aac"
        )


# -- sha256 ------------------------------------------------------------------------------------------


def test_verify_sha256(build: ModuleType, tmp_path: Path) -> None:
    import hashlib

    archive = tmp_path / "deno.zip"
    archive.write_bytes(b"not really a zip")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = tmp_path / "deno.zip.sha256sum"
    checksum.write_text(f"{digest}  deno.zip\n", encoding="utf-8")
    assert build.verify_sha256(archive, checksum) == digest
    checksum.write_text(digest.upper(), encoding="utf-8")  # a bare, upper-case digest is fine too
    assert build.verify_sha256(archive, checksum) == digest
    checksum.write_text("0" * 64 + "  deno.zip\n", encoding="utf-8")
    with pytest.raises(build.BuildError, match="SHA-256 mismatch"):
        build.verify_sha256(archive, checksum)
    checksum.write_text("<html>not found</html>", encoding="utf-8")
    with pytest.raises(build.BuildError, match="no SHA-256 digest"):
        build.verify_sha256(archive, checksum)


# -- filesystem steps ------------------------------------------------------------------------------


@pytest.mark.skipif(not WINDOWS, reason="only Windows refuses to delete an open file")
def test_remove_tree_explains_a_locked_file(build: ModuleType, tmp_path: Path) -> None:
    folder = tmp_path / "pkg"
    folder.mkdir()
    locked = folder / "ffmpeg.exe"
    with locked.open("wb") as handle:
        handle.write(b"busy")
        with pytest.raises(build.BuildError, match="still running"):
            build.remove_tree(folder)
    build.remove_tree(folder)  # closed: gone without complaint
    assert not folder.exists()
    build.remove_tree(folder)  # and a second time is a no-op


def test_copy_file_reports_a_missing_source(build: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(build.BuildError, match="Cannot copy"):
        build.copy_file(tmp_path / "missing", tmp_path / "out" / "x")


# -- notices ---------------------------------------------------------------------------------------


def test_shipped_distributions_refuses_to_guess(
    build: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build, "BUNDLED_MODULES_FILE", tmp_path / "missing.txt")
    with pytest.raises(build.BuildError, match="skip-pyinstaller"):
        build.shipped_distributions()


@pytest.mark.parametrize(
    ("project_urls", "expected"),
    [
        (["Homepage, https://h.test", "Changelog, https://c.test"], "https://h.test"),
        # charset-normalizer style: Code + Documentation + Changelog, no Homepage
        (
            [
                "Changelog, https://github.com/x/y/blob/master/CHANGELOG.md",
                "Code, https://github.com/x/y",
                "Documentation, https://x.readthedocs.io",
            ],
            "https://github.com/x/y",
        ),
        # urllib3 style
        (
            [
                "Changelog, https://github.com/urllib3/urllib3/blob/main/CHANGES.rst",
                "Documentation, https://urllib3.readthedocs.io",
                "Code, https://github.com/urllib3/urllib3",
                "Issue tracker, https://github.com/urllib3/urllib3/issues",
            ],
            "https://github.com/urllib3/urllib3",
        ),
        (["Changelog, https://c.test", "Documentation, https://d.test"], "https://d.test"),
        (["Changelog, https://c.test"], "https://c.test"),
        ([], "https://pypi.org/project/pkg/"),
    ],
)
def test_homepage_of_prefers_the_project_page_over_the_changelog(
    build: ModuleType, tmp_path: Path, project_urls: list[str], expected: str
) -> None:
    dist_info = tmp_path / "pkg-1.0.dist-info"
    dist_info.mkdir()
    metadata = "Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n"
    metadata += "".join(f"Project-URL: {entry}\n" for entry in project_urls)
    (dist_info / "METADATA").write_text(metadata, encoding="utf-8")
    assert build.homepage_of(importlib.metadata.PathDistribution(dist_info)) == expected


def test_runtime_licenses_are_committed_and_copied(
    build: ModuleType, bin_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every library linked into the frozen interpreter has its upstream text in the repo."""
    dest = bin_dir / "licenses" / "python-runtime"
    copied = build.copy_runtime_licenses(dest)
    assert set(copied) == {c for c, *_ in build.PYTHON_RUNTIME_LICENSES}
    for component, _files, _license, names in build.PYTHON_RUNTIME_LICENSES:
        assert copied[component] == [f"bin/licenses/python-runtime/{n}" for n in names]
        for name in names:
            assert (dest / name).is_file() and (dest / name).stat().st_size > 100
    openssl = (dest / "openssl-LICENSE.txt").read_text("utf-8")
    assert "Apache License" in openssl and "Version 2.0" in openssl
    assert "Anthony Green" in (dest / "libffi-LICENSE.txt").read_text("utf-8")
    assert "Stefan Krah" in (dest / "mpdecimal-LICENSE.txt").read_text("utf-8")
    assert "Expat maintainers" in (dest / "expat-COPYING.txt").read_text("utf-8")
    monkeypatch.setattr(build, "PACKAGING_LICENSES", tmp_path / "nowhere")
    with pytest.raises(build.BuildError, match="packaging/licenses/README.md"):
        build.copy_runtime_licenses(dest)


def test_unknown_runtime_dlls_are_reported(build: ModuleType, tmp_path: Path) -> None:
    internal = tmp_path / "_internal"
    internal.mkdir()
    for name in (
        "python314.dll",
        "python3.dll",
        "VCRUNTIME140.dll",
        "VCRUNTIME140_1.dll",
        "libcrypto-3-x64.dll",
        "libssl-3-x64.dll",
        "libffi-8.dll",
        "sqlite3.dll",
        "_zstd.pyd",
    ):
        (internal / name).write_bytes(b"MZ")
    assert build.unknown_runtime_dlls(internal) == []
    (internal / "libsomething-new-1.dll").write_bytes(b"MZ")
    assert build.unknown_runtime_dlls(internal) == ["libsomething-new-1.dll"]
    assert build.unknown_runtime_dlls(tmp_path / "missing") == []


def test_package_licenses_are_copied_and_named_in_the_notices(
    build: ModuleType, bin_dir: Path, tmp_path: Path
) -> None:
    site = tmp_path / "site"
    fake = make_distribution(
        site,
        "fakepkg",
        "1.0",
        {"licenses/LICENSE": "MIT text", "licenses/vendor/sub/COPYING": "vendored text"},
    )
    legacy = make_distribution(site, "legacy", "2.0", {"LICENSE.txt": "BSD text", "NOTICE": "n"})
    bare = make_distribution(site, "bare", "3.0", {})
    assert [rel.as_posix() for _src, rel in build.package_license_files(fake)] == [
        "LICENSE",
        "vendor/sub/COPYING",
    ]
    copied = build.copy_package_licenses([fake, legacy, bare], bin_dir / "licenses")
    assert copied == {
        "fakepkg": ["bin/licenses/fakepkg/LICENSE", "bin/licenses/fakepkg/vendor/sub/COPYING"],
        "legacy": ["bin/licenses/legacy/LICENSE.txt", "bin/licenses/legacy/NOTICE"],
        "bare": ["bin/licenses/bare/LICENSE-NOT-INCLUDED.txt"],
    }
    assert (bin_dir / "licenses" / "fakepkg" / "LICENSE").read_text("utf-8") == "MIT text"
    assert (bin_dir / "licenses" / "fakepkg" / "vendor" / "sub" / "COPYING").is_file()
    assert (bin_dir / "licenses" / "legacy" / "NOTICE").read_text("utf-8") == "n"
    pointer = (bin_dir / "licenses" / "bare" / "LICENSE-NOT-INCLUDED.txt").read_text("utf-8")
    assert "https://example.test/bare" in pointer and "MIT" in pointer

    runtime_licenses = build.copy_runtime_licenses(bin_dir / "licenses" / "python-runtime")
    notices = build.write_notices(
        "0.0.1",
        ffmpeg_output="ffmpeg version 8.1.1-test\nconfiguration: --enable-gpl --enable-version3",
        ffmpeg_license=build.ffmpeg_license_kind("configuration: --enable-gpl --enable-version3"),
        ffmpeg_license_file="bin/ffmpeg-LICENSE.txt",
        js_kind="node",
        js_output="v24.0.0",
        js_license_file="bin/node-LICENSE.txt",
        js_tag="v24.0.0",
        python_license_file="bin/python-LICENSE.txt",
        runtime_licenses=runtime_licenses,
        distributions=[fake, legacy, bare],
        package_licenses=copied,
    )
    text = notices.read_text("utf-8")
    assert notices == bin_dir / "THIRD_PARTY_NOTICES.txt"
    assert "Build bundled: ffmpeg version 8.1.1-test" in text
    assert "License: GNU General Public License v3" in text
    assert "Version bundled: v24.0.0" in text
    assert "Full license text: bin/node-LICENSE.txt" in text
    # the version-pinned upstream file, and honesty about what node.exe contains
    assert "https://github.com/nodejs/node/blob/v24.0.0/LICENSE" in text
    assert "Node's own LICENSE, which lists every one of them" in text
    assert "all listed in the Node.js LICENSE file" not in text
    assert "Full license text: bin/python-LICENSE.txt" in text
    assert "VCRUNTIME140" in text and "OpenSSL" in text
    # every DLL / pyd component has its own shipped text, named right after it
    lines = text.splitlines()
    for component, _files, license_name, names in build.PYTHON_RUNTIME_LICENSES:
        index = next(i for i, line in enumerate(lines) if line.startswith(f"  {component}: "))
        assert lines[index + 1] == f"    License: {license_name}"
        expected = ", ".join(f"bin/licenses/python-runtime/{n}" for n in names)
        assert lines[index + 2] == f"    Full license text: {expected}"
    assert "  zlib: statically linked into python" in text
    assert "SQLite: sqlite3.dll" in text and "public domain" in text
    assert "covered by" not in text  # no more pointing at a file that lacks the text
    assert "fakepkg 1.0\n  License: MIT\n  https://example.test/fakepkg\n" in text
    assert (
        "Full license text: bin/licenses/fakepkg/LICENSE, bin/licenses/fakepkg/vendor/sub/COPYING"
        in text
    )
    assert "Full license text: bin/licenses/bare/LICENSE-NOT-INCLUDED.txt" in text
    assert "bin/licenses/<package>/" in text
    assert "unknown version" not in text
    # every "Full license text" line points at a file that exists in the package folder
    for line in text.splitlines():
        if "Full license text:" in line:
            for rel in line.split(":", 1)[1].split(","):
                rel = rel.strip()
                if rel.startswith("bin/licenses/"):
                    assert (bin_dir.parent / rel).is_file(), rel


def test_deno_notices_point_at_the_pinned_license(build: ModuleType, bin_dir: Path) -> None:
    runtime_licenses = build.copy_runtime_licenses(bin_dir / "licenses" / "python-runtime")
    text = build.write_notices(
        "0.0.1",
        ffmpeg_output="ffmpeg version 8.1.1-test",
        ffmpeg_license=build.ffmpeg_license_kind(""),
        ffmpeg_license_file=None,
        js_kind="deno",
        js_output="deno 2.9.6 (stable, release, x86_64-pc-windows-msvc)",
        js_license_file="bin/deno-LICENSE.txt",
        js_tag="v2.9.6",
        python_license_file=None,
        runtime_licenses=runtime_licenses,
        distributions=[],
        package_licenses={},
    ).read_text("utf-8")
    assert "https://github.com/denoland/deno/blob/v2.9.6/LICENSE.md" in text
    assert "Full license text: bin/deno-LICENSE.txt" in text
    assert "2018-2025" not in text  # no hard-coded copyright years: the license file has them


# -- package files ------------------------------------------------------------------------------------


def test_package_readme_links_are_absolute(build: ModuleType) -> None:
    text = "See [Releases](../../releases) and [docs/PACKAGING.md](docs/PACKAGING.md) and [x](LICENSE)."
    out = build.package_readme_text(text)
    assert f"]({build.REPO_URL}/releases)" in out
    assert f"]({build.REPO_URL}/blob/main/docs/PACKAGING.md)" in out
    assert "](LICENSE)" in out  # shipped next to the README: stays relative
    assert "../../" not in out


def test_start_here_says_extract_first(build: ModuleType) -> None:
    text = build.START_HERE.format(version="0.1.0", repo=build.REPO_URL)
    assert text.splitlines()[2].startswith(
        "1. If you are reading this inside the zip, extract it first"
    )
    assert "UltimatePlaylist.exe doctor" in text
    assert "extract the zip again" in text
    assert f"{build.REPO_URL}/releases" in text
    assert "winget" not in text
    # Windows 11 wording first, the Windows 10 label as the fallback
    assert '"Open in Terminal"' in text and "on Windows 10: Shift + right-click" in text
    assert "Protection" in text and "false positive" in text  # Defender flagging the exe itself
    assert "To remove every trace, also delete" in text and "ordinary files" in text


def test_deno_download_is_pinned_and_checksummed(build: ModuleType) -> None:
    assert "/latest/" not in build.DENO_URL
    assert f"/v{build.DENO_VERSION}/" in build.DENO_URL
    assert build.DENO_SHA256_URL == build.DENO_URL + ".sha256sum"
    assert build.DENO_LICENSE_URL.endswith(f"v{build.DENO_VERSION}/LICENSE.md")


def test_ffmpeg_download_is_checksummed(
    build: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gyan.dev publishes <zip>.sha256 (a bare digest) next to every build; the 100+ MB GPL
    archive must be verified against it before anything is extracted."""
    import hashlib
    import zipfile

    assert build.FFMPEG_SHA256_URL == build.FFMPEG_URL + ".sha256"
    archive_bytes = tmp_path / "essentials.zip"
    with zipfile.ZipFile(archive_bytes, "w") as zf:
        zf.writestr("ffmpeg-9.0-essentials_build/bin/ffmpeg.exe", "MZ")
        zf.writestr("ffmpeg-9.0-essentials_build/bin/ffprobe.exe", "MZ")
        zf.writestr("ffmpeg-9.0-essentials_build/LICENSE", "GPL")
    digest = hashlib.sha256(archive_bytes.read_bytes()).hexdigest()
    served: dict[str, bytes] = {
        build.FFMPEG_URL: archive_bytes.read_bytes(),
        build.FFMPEG_SHA256_URL: (digest + "\n").encode(),
    }
    urls: list[str] = []

    def download(url: str, dest: Path) -> Path:
        urls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(served[url])
        return dest

    monkeypatch.setattr(build, "download", download)
    monkeypatch.setattr(build, "DOWNLOADS", tmp_path / "downloads")
    ffmpeg, ffprobe, license_file = build.locate_ffmpeg("download")
    assert urls == [build.FFMPEG_URL, build.FFMPEG_SHA256_URL]
    assert ffmpeg.name == "ffmpeg.exe" and ffprobe.name == "ffprobe.exe"
    assert license_file is not None and license_file.name == "LICENSE"
    served[build.FFMPEG_SHA256_URL] = ("0" * 64).encode()  # a replaced or truncated archive
    with pytest.raises(build.BuildError, match="SHA-256 mismatch"):
        build.locate_ffmpeg("download")


def test_check_platform_refuses_other_architectures(
    build: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build.sys, "platform", "win32")
    monkeypatch.setattr(build.platform, "machine", lambda: "ARM64")
    with pytest.raises(build.BuildError, match="x64"):
        build.check_platform()
    monkeypatch.setattr(build.platform, "machine", lambda: "AMD64")
    build.check_platform()
    monkeypatch.setattr(build.sys, "platform", "linux")
    with pytest.raises(build.BuildError, match="Windows"):
        build.check_platform()
