# License texts for the libraries inside the frozen Python runtime

`scripts/build_windows.py` copies this folder to `bin/licenses/python-runtime/` in the Windows
package and names every file in `bin/THIRD_PARTY_NOTICES.txt`. These are the libraries that
python-build-standalone (the interpreter uv installs and the package is frozen with) links into
`_internal/` and whose license texts the interpreter's own `LICENSE.txt` does not carry
(that file, shipped as `bin/python-LICENSE.txt`, has the PSF license plus the Microsoft
Distributable Code terms, bzip2, zstd and Tcl/Tk).

| File | Component | Shipped as | License |
| --- | --- | --- | --- |
| `openssl-LICENSE.txt` | OpenSSL 3 | `libcrypto-3-x64.dll`, `libssl-3-x64.dll` (`_ssl.pyd`, `_hashlib.pyd`) | Apache-2.0 |
| `libffi-LICENSE.txt` | libffi | `libffi-8.dll` (`_ctypes.pyd`) | MIT |
| `expat-COPYING.txt` | Expat | `pyexpat.pyd`, `_elementtree.pyd` | MIT |
| `mpdecimal-LICENSE.txt` | mpdecimal (libmpdec) | `_decimal.pyd` | BSD-2-Clause |
| `xz-COPYING.txt`, `xz-COPYING.0BSD.txt` | XZ Utils (liblzma) | `_lzma.pyd` | 0BSD |
| `zlib-LICENSE.txt` | zlib | statically linked into `python3xx.dll` (`zlib` module) | zlib |
| `zstd-LICENSE.txt` | Zstandard | `_zstd.pyd` | BSD-3-Clause |
| `bzip2-LICENSE.txt` | bzip2 | `_bz2.pyd` | bzip2 |

SQLite (`sqlite3.dll`, `_sqlite3.pyd`) is public domain and has no license text. The Visual C++
runtime DLLs (`VCRUNTIME140*.dll`) are covered by the Microsoft terms in `python-LICENSE.txt`.

The texts are the upstream `LICENSE` / `COPYING` files as distributed with the libraries (the
copies here were taken from the MSYS2 packages that Git for Windows ships in
`mingw64/share/licenses/`, which carry the files verbatim; `mpdecimal-LICENSE.txt` is the
`LICENSE.txt` of mpdecimal 4.x). The wording of these licenses does not change between
versions; only the copyright years in the first line move. When bumping the interpreter, refresh
them from the upstream repositories if you want the years to match exactly:

- <https://github.com/openssl/openssl/blob/master/LICENSE.txt>
- <https://github.com/libffi/libffi/blob/master/LICENSE>
- <https://github.com/libexpat/libexpat/blob/master/expat/COPYING>
- <https://www.bytereef.org/mpdecimal/license.html>
- <https://github.com/tukaani-project/xz/blob/master/COPYING> (and `COPYING.0BSD`)
- <https://github.com/madler/zlib/blob/master/LICENSE>
- <https://github.com/facebook/zstd/blob/dev/LICENSE>
- <https://sourceware.org/git/?p=bzip2.git;a=blob;f=LICENSE>

Rule for the whole package (see `docs/PACKAGING.md`, "Licenses in the bundle"): a license text
is always the file upstream published, copied or downloaded at the version that ships; the build
never writes one from a template.
