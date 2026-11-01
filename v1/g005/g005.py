#!/data/data/com.termux/files/usr/bin/env python3
"""g005 - consolidated archive / compression toolkit.

Everything that used to live in this folder as separate scripts now lives in
this one file.  Each tool's original source is embedded verbatim below, so
nothing was lost: ``python g005.py unpack <tool>`` writes the original
file back out.

One tool per format or job. Codec-specific duplicates were dropped in
favour of the most complete implementation for each codec.

Tools
-----
extract  Extract anything: tar/zip/7z/xz/bz2/gz/zst/lz4/br/whl, safe paths, organise/rename, integrity checks, external-tool fallbacks.
         from archive_tools.py (888 lines)
convert  Re-encode archives and files: tar-codec, archive-convert, br2zst, whl-txz.
         from artools.py (958 lines)
dirs     Tar a directory (tar/tgz) in parallel, with optional cleanup of the archived contents.
         from archive_dir.py (150 lines)
7z       7z compression in three modes (basic/named/full) via py7zr.
         from 7ziper.py (613 lines)
brotli   Parallel Brotli compress/decompress/test with chunking, hashing and verification.
         from br.py (1352 lines)
zstd     zstd compress/decompress/archive-cwd/split with per-file sizing rules.
         from zstdtools.py (848 lines)
xz       xz/LZMA compress, decompress, tar-dirs, archive-cwd, 7z and lzma-chunk subcommands.
         from xztools.py (1200 lines)
lz4      LZ4 compress/decompress, including tarred folder archives.
         from lz4er.py (342 lines)
gzbz     gzip and bzip2 compression in three subcommands (gz, bz2, gzip-files) with a worker pool.
         from gzbz.py (641 lines)
text     Compress single text files with zstd/brotli/ppmd, verifying round-trips and reporting progress.
         from compress_text.py (618 lines)
subdirs  Compress every subdirectory into its own tar.xz/gz/bz2/zst/lz4/snappy archive.
         from compress_subdirs.py (598 lines)
bench    Benchmark every available codec on the same payload.
         from compression_benchmark.py (324 lines)
sniff    Magic-byte detection and best-effort decompression of unknown archives (rar/7z/cab/lzo/ace/iso/pyppmd/...).
         from trydecomp.py (705 lines)
report   Scan a tree and report archive sizes, contents and stream sizes.
         from xreport.py (592 lines)

Usage
-----
    python g005.py list                 # list the tools
    python g005.py doctor               # report missing third-party packages
    python g005.py <tool> --help        # help for one tool
    python g005.py extract <args>
    python g005.py convert <args>
    python g005.py dirs <args>
    python g005.py unpack <tool> [-o DIR] [--stdout]

Requires Python 3.10+.  Individual tools need their own third-party packages;
run ``doctor`` to see which are missing.
"""

from __future__ import annotations

import argparse
import ast
import base64
import difflib
import importlib.util
import inspect
import sys
import types
import zlib
from pathlib import Path

FOLDER = "g005"


_HERE = Path(__file__).resolve().parent if "__file__" in globals() else None

TOOLS: dict[str, dict[str, object]] = {
    "extract": {"source": "archive_tools.py", "lines": 888, "summary": 'Extract anything: tar/zip/7z/xz/bz2/gz/zst/lz4/br/whl, safe paths, organise/rename, integrity checks, external-tool fallbacks.'},
    "convert": {"source": "artools.py", "lines": 958, "summary": 'Re-encode archives and files: tar-codec, archive-convert, br2zst, whl-txz.'},
    "dirs": {"source": "archive_dir.py", "lines": 150, "summary": 'Tar a directory (tar/tgz) in parallel, with optional cleanup of the archived contents.'},
    "7z": {"source": "7ziper.py", "lines": 613, "summary": '7z compression in three modes (basic/named/full) via py7zr.'},
    "brotli": {"source": "br.py", "lines": 1352, "summary": 'Parallel Brotli compress/decompress/test with chunking, hashing and verification.'},
    "zstd": {"source": "zstdtools.py", "lines": 848, "summary": 'zstd compress/decompress/archive-cwd/split with per-file sizing rules.'},
    "xz": {"source": "xztools.py", "lines": 1200, "summary": 'xz/LZMA compress, decompress, tar-dirs, archive-cwd, 7z and lzma-chunk subcommands.'},
    "lz4": {"source": "lz4er.py", "lines": 342, "summary": 'LZ4 compress/decompress, including tarred folder archives.'},
    "gzbz": {"source": "gzbz.py", "lines": 641, "summary": 'gzip and bzip2 compression in three subcommands (gz, bz2, gzip-files) with a worker pool.'},
    "text": {"source": "compress_text.py", "lines": 618, "summary": 'Compress single text files with zstd/brotli/ppmd, verifying round-trips and reporting progress.'},
    "subdirs": {"source": "compress_subdirs.py", "lines": 598, "summary": 'Compress every subdirectory into its own tar.xz/gz/bz2/zst/lz4/snappy archive.'},
    "bench": {"source": "compression_benchmark.py", "lines": 324, "summary": 'Benchmark every available codec on the same payload.'},
    "sniff": {"source": "trydecomp.py", "lines": 705, "summary": 'Magic-byte detection and best-effort decompression of unknown archives (rar/7z/cab/lzo/ace/iso/pyppmd/...).'},
    "report": {"source": "xreport.py", "lines": 592, "summary": 'Scan a tree and report archive sizes, contents and stream sizes.'},
}

_SOURCES: dict[str, str] = {}
_LOADED: dict[str, types.ModuleType] = {}


def _decode(*chunks: str) -> str:
    """Fallback representation for sources that cannot be written as raw strings."""
    return zlib.decompress(base64.b85decode("".join(chunks))).decode("utf-8")


_SOURCES['extract'] = r'''#!/data/data/com.termux/files/usr/bin/env python
"""(No content in stream)

---"""

from __future__ import annotations

import argparse
import bz2
import contextlib
import gzip
import lzma
import multiprocessing as mp
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional, Sequence

try:
    import py7zr
except ImportError:
    py7zr = None
try:
    import zstandard as zstd
except ImportError:
    zstd = None
try:
    import brotli
except ImportError:
    brotli = None
try:
    import lz4.frame as lz4frame
except ImportError:
    lz4frame = None
DEFAULT_JOBS: int = 8
DEFAULT_TIMEOUT: int = 300
CHUNK: int = 1024 * 1024
CLI_COMMANDS: dict[str, list[str]] = {
    ".7z": ["7z", "x", "-y"],
    ".zip": ["unzip", "-o"],
    ".rar": ["unrar", "x", "-y"],
    ".tar": ["tar", "-xf"],
    ".tar.gz": ["tar", "-xzf"],
    ".tgz": ["tar", "-xzf"],
    ".tar.bz2": ["tar", "-xjf"],
    ".tbz2": ["tar", "-xjf"],
    ".tar.xz": ["tar", "-xJf"],
    ".txz": ["tar", "-xJf"],
    ".gz": ["gunzip", "-f"],
    ".bz2": ["bunzip2", "-f"],
    ".xz": ["unxz", "-f"],
    ".lz4": ["lz4", "-d", "-f"],
    ".lzma": ["unlzma", "-f"],
    ".zst": ["unzstd", "-f"],
    ".cab": ["cabextract"],
    ".arj": ["arj", "x", "-y"],
    ".ace": ["unace", "x"],
}
ALL_EXTENSIONS: tuple[str, ...] = tuple(
    sorted(
        {
            ".tar.gz",
            ".tar.bz2",
            ".tar.xz",
            ".tar.zst",
            ".tar.br",
            ".tar.lz4",
            ".tgz",
            ".tbz2",
            ".txz",
            ".whl",
            ".7z",
            ".zip",
            ".rar",
            ".tar",
            ".cab",
            ".arj",
            ".ace",
            ".gz",
            ".bz2",
            ".xz",
            ".lz4",
            ".lzma",
            ".zst",
            ".br",
        },
        key=len,
        reverse=True,
    )
)


@dataclass
class ExtractResult:
    archive_path: Path
    status: str = "failed"
    output_dir: Optional[Path] = None
    extracted_files: int = 0
    extracted_size: int = 0
    extraction_time: float = 0.0
    original_size: int = 0
    error_message: str = ""

    def __str__(self) -> str:
        mb = self.original_size / (1024 * 1024)
        icon = {"success": "✓", "failed": "✗", "skipped": "○"}.get(self.status, "?")
        out = f"{icon} {self.archive_path.name} [{mb:.1f}MB] - {self.status}"
        if self.status == "success":
            out += f" ({self.extracted_files} files, {self.extraction_time:.1f}s)"
            if self.output_dir:
                out += f" → {self.output_dir.name}"
        elif self.status == "failed" and self.error_message:
            out += f" - {self.error_message}"
        return out


def count_files(p: Path) -> int:
    try:
        return sum(1 for f in p.rglob("*") if f.is_file())
    except Exception:
        return 0


def dir_size(p: Path) -> int:
    try:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except Exception:
        return 0


def unique_dir(p: Path) -> Path:
    if not p.exists():
        return p
    i = 1
    while True:
        q = p.with_name(f"{p.name}_{i}")
        if not q.exists():
            return q
        i += 1


def strip_version(stem: str) -> str:
    parts = stem.replace(".whl", "").split("-")
    for i, part in enumerate(parts):
        if part and part[0].isdigit():
            return "-".join(parts[:i])
    return parts[0]


def detect_extension(path: Path) -> Optional[str]:
    name = path.name.lower()
    if name.endswith(".whl"):
        return ".whl"
    if name.endswith(".br"):
        return ".br"
    if name.endswith(".tar.lz4"):
        return ".tar.lz4"
    for ext in sorted(CLI_COMMANDS.keys(), key=len, reverse=True):
        if name.endswith(ext):
            return ext
    return None


@contextlib.contextmanager
def _open_decompressor(path: Path, kind: str) -> Iterator:
    if kind == "gz":
        with gzip.open(path, "rb") as f:
            yield f
    elif kind == "bz2":
        with bz2.open(path, "rb") as f:
            yield f
    elif kind == "xz":
        with lzma.open(path, "rb") as f:
            yield f
    elif kind == "zst":
        if zstd is None:
            msg = "zstandard is not installed"
            raise RuntimeError(msg)
        with open(path, "rb") as raw, zstd.ZstdDecompressor().stream_reader(raw) as f:
            yield f
    elif kind == "br":
        if brotli is None:
            msg = "brotli is not installed"
            raise RuntimeError(msg)
        import io

        yield io.BytesIO(brotli.decompress(path.read_bytes()))
    elif kind == "lz4":
        if lz4frame is None:
            msg = "lz4 is not installed"
            raise RuntimeError(msg)
        with lz4frame.open(path, "rb") as f:
            yield f
    else:
        msg = f"unknown compression kind: {kind}"
        raise ValueError(msg)


def _decompress_stream(src: Path, dst: Path, kind: str) -> None:
    with _open_decompressor(src, kind) as f, open(dst, "wb") as o:
        shutil.copyfileobj(f, o, CHUNK)


def _safe_extract_tar(tar: tarfile.TarFile, dest: Path) -> None:
    try:
        tar.extractall(path=dest, filter="data")
    except TypeError:
        tar.extractall(path=dest)


def _extract_tar_python(archive: Path, dest: Path) -> None:
    name = archive.name.lower()
    dest.mkdir(parents=True, exist_ok=True)
    for sfx, kind in ((".tar.zst", "zst"), (".tar.br", "br"), (".tar.lz4", "lz4")):
        if name.endswith(sfx):
            with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tf:
                tmp = Path(tf.name)
            try:
                _decompress_stream(archive, tmp, kind)
                with tarfile.open(tmp, "r:") as tar:
                    _safe_extract_tar(tar, dest)
            finally:
                tmp.unlink(missing_ok=True)
            return
    if name.endswith((".tar.gz", ".tgz")):
        mode = "r:gz"
    elif name.endswith((".tar.bz2", ".tbz2")):
        mode = "r:bz2"
    elif name.endswith((".tar.xz", ".txz")):
        mode = "r:xz"
    elif name.endswith(".tar"):
        mode = "r:"
    else:
        msg = f"not a tar: {archive.name}"
        raise ValueError(msg)
    with tarfile.open(archive, mode) as tar:
        _safe_extract_tar(tar, dest)


def extract_with_python(archive: Path, dest: Path) -> None:
    name = archive.name.lower()
    dest.mkdir(parents=True, exist_ok=True)
    tar_suffixes = (
        ".tar",
        ".tar.gz",
        ".tgz",
        ".tar.bz2",
        ".tbz2",
        ".tar.xz",
        ".txz",
        ".tar.zst",
        ".tar.br",
        ".tar.lz4",
    )
    if name.endswith(tar_suffixes):
        _extract_tar_python(archive, dest)
        return
    if name.endswith((".zip", ".whl")):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(path=dest)
        return
    if name.endswith(".7z"):
        if py7zr is None:
            msg = "py7zr is not installed"
            raise RuntimeError(msg)
        with py7zr.SevenZipFile(archive, mode="r") as sz:
            sz.extractall(path=dest)
        return
    for ext, kind in (
        (".gz", "gz"),
        (".bz2", "bz2"),
        (".xz", "xz"),
        (".lzma", "xz"),
        (".zst", "zst"),
        (".br", "br"),
        (".lz4", "lz4"),
    ):
        if name.endswith(ext):
            out_name = archive.name[: -len(ext)]
            _decompress_stream(archive, dest / out_name, kind)
            return
    msg = f"python engine does not support: {archive.name}"
    raise ValueError(msg)


def extract_with_external(archive: Path, dest: Path, cwd: Path) -> None:
    ext = detect_extension(archive)
    if not ext or ext not in CLI_COMMANDS:
        msg = f"no external tool for: {archive.name}"
        raise ValueError(msg)
    cmd = list(CLI_COMMANDS[ext])
    tool = cmd[0]
    if shutil.which(tool) is None:
        msg = f"tool '{tool}' not found in PATH"
        raise RuntimeError(msg)
    dest.mkdir(parents=True, exist_ok=True)
    if ext == ".7z":
        cmd += [f"-o{dest}", str(archive)]
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            cwd=cwd,
            timeout=DEFAULT_TIMEOUT,
        )
        return
    if ext == ".zip":
        cmd += [str(archive), "-d", str(dest)]
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            cwd=cwd,
            timeout=DEFAULT_TIMEOUT,
        )
        return
    if ext in (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz"):
        cmd += ["-C", str(dest), str(archive)]
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            cwd=cwd,
            timeout=DEFAULT_TIMEOUT,
        )
        return
    if ext == ".rar":
        cmd += [str(archive), str(dest)]
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            cwd=cwd,
            timeout=DEFAULT_TIMEOUT,
        )
        return
    if ext in (".gz", ".bz2", ".xz", ".lzma", ".zst", ".lz4"):
        local = dest / archive.name
        shutil.copy2(archive, local)
        cmd.append(local.name)
        try:
            subprocess.run(
                cmd,
                cwd=dest,
                capture_output=True,
                text=True,
                check=True,
                timeout=DEFAULT_TIMEOUT,
            )
        finally:
            local.unlink(missing_ok=True)
        return
    cmd.append(str(archive))
    if dest != cwd:
        cmd.append(str(dest))
    subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
        cwd=cwd,
        timeout=DEFAULT_TIMEOUT,
    )


def _try_extract(archive: Path, dest: Path, engine: str) -> None:
    if engine == "python":
        extract_with_python(archive, dest)
    elif engine == "external":
        extract_with_external(archive, dest, archive.parent)
    elif engine == "auto":
        errs: list[str] = []
        try:
            extract_with_python(archive, dest)
            return
        except Exception as e:
            errs.append(f"python: {e}")
        try:
            extract_with_external(archive, dest, archive.parent)
            return
        except Exception as e:
            errs.append(f"external: {e}")
        raise RuntimeError(" | ".join(errs))
    else:
        msg = f"unknown engine: {engine}"
        raise ValueError(msg)


def should_use_subdir(archive: Path) -> bool:
    name = archive.name.lower()
    for ext in (".gz", ".bz2", ".xz", ".lz4", ".lzma", ".zst", ".br"):
        if name.endswith(ext) and ".tar." not in name:
            return True
    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                names = zf.namelist()
        elif name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
            with tarfile.open(archive, "r:*") as tar:
                names = tar.getnames()
        else:
            return False
        parts = [Path(n).parts for n in names if n]
        if not parts:
            return False
        roots = {p[0] for p in parts}
        return len(roots) == 1 and any(len(p) > 1 for p in parts)
    except Exception:
        return False


def check_integrity(archive: Path) -> tuple[bool, str]:
    name = archive.name.lower()
    try:
        if name.endswith((".zip", ".whl")):
            if not zipfile.is_zipfile(archive):
                return False, f"Invalid zip: {archive.name}"
            with zipfile.ZipFile(archive) as zf:
                bad = zf.testzip()
                if bad is not None:
                    return False, f"Corrupted: {archive.name} (bad member {bad})"
            return True, f"Valid: {archive.name}"
        if name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")):
            if not tarfile.is_tarfile(archive):
                return False, f"Invalid tar: {archive.name}"
            with tarfile.open(archive, "r:*") as tar:
                tar.getmembers()
            return True, f"Valid: {archive.name}"
        if name.endswith(".tar.zst"):
            with _open_decompressor(archive, "zst") as f:
                while f.read(CHUNK):
                    pass
            return True, f"Valid: {archive.name}"
        if name.endswith(".tar.br"):
            with _open_decompressor(archive, "br") as f:
                while f.read(CHUNK):
                    pass
            return True, f"Valid: {archive.name}"
        if name.endswith(".7z"):
            if py7zr is None:
                return False, f"py7zr not installed: {archive.name}"
            with py7zr.SevenZipFile(archive) as sz:
                sz.getnames()
            return True, f"Valid: {archive.name}"
        for ext, kind in (
            (".gz", "gz"),
            (".bz2", "bz2"),
            (".xz", "xz"),
            (".zst", "zst"),
            (".br", "br"),
            (".lz4", "lz4"),
        ):
            if name.endswith(ext):
                with _open_decompressor(archive, kind) as f:
                    while f.read(CHUNK):
                        pass
                return True, f"Valid: {archive.name}"
    except Exception as e:
        return False, f"Corrupted: {archive.name} ({e})"
    return True, f"Skipped check: {archive.name}"


def find_archives(roots: Iterable[Path], recursive: bool, formats: Optional[set[str]] = None) -> list[Path]:
    exts = formats or set(ALL_EXTENSIONS)
    ordered = sorted(exts, key=len, reverse=True)
    found: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        root = Path(root)
        if root.is_file():
            found.append(root)
            continue
        if not root.is_dir():
            continue
        it = root.rglob("*") if recursive else root.iterdir()
        for p in it:
            if not p.is_file():
                continue
            low = p.name.lower()
            if any(low.endswith(e) for e in ordered) and p not in seen:
                found.append(p)
                seen.add(p)
    return sorted(found)


def extract_one(
    archive: Path,
    *,
    engine: str = "auto",
    keep: bool = False,
    dry_run: bool = False,
    organize: str = "flat",
    out_dir: Optional[Path] = None,
    single_file_subdir: bool = False,
    subdir_truncate: int = 0,
    quiet: bool = False,
) -> ExtractResult:
    start = time.time()
    size = archive.stat().st_size if archive.exists() else 0
    res = ExtractResult(archive_path=archive, original_size=size)
    if not archive.exists():
        res.error_message = "file not found"
        res.extraction_time = time.time() - start
        return res
    if dry_run:
        res.status = "success"
        res.extraction_time = time.time() - start
        if not quiet:
            print(f"[DRY RUN] would extract: {archive.name}")
        return res
    parent = archive.parent
    stem = archive.stem
    if subdir_truncate > 0:
        stem = stem[:subdir_truncate]
    use_subdir = False
    if organize == "flat":
        if single_file_subdir and should_use_subdir(archive):
            dest = parent / stem
            use_subdir = True
        else:
            dest = out_dir or parent
    elif organize == "stem":
        dest = parent / stem
        use_subdir = True
    elif organize == "versioned":
        dest = unique_dir(parent / strip_version(archive.stem))
        use_subdir = True
    else:
        dest = out_dir or parent
    try:
        _try_extract(archive, dest, engine)
    except Exception as e:
        res.status = "failed"
        msg = str(e)
        res.error_message = msg.splitlines()[0][:200] if msg else type(e).__name__
        res.extraction_time = time.time() - start
        return res
    res.status = "success"
    res.output_dir = dest if use_subdir else None
    res.extracted_files = count_files(dest if use_subdir else parent)
    res.extracted_size = dir_size(dest if use_subdir else parent)
    if not keep:
        try:
            archive.unlink()
        except OSError as e:
            res.error_message = f"extracted, but couldn't remove original: {e}"
    res.extraction_time = time.time() - start
    return res


def _worker(payload: tuple[Path, dict]) -> ExtractResult:
    archive, opts = payload
    return extract_one(archive, **opts)


def _parse_formats(s: Optional[str]) -> Optional[set[str]]:
    if not s:
        return None
    out: set[str] = set()
    for tok in s.split(","):
        tok = tok.strip().lower()
        if not tok:
            continue
        if not tok.startswith("."):
            tok = "." + tok
        out.add(tok)
    return out


def _summary(results: Sequence[ExtractResult], elapsed: float, *, quiet: bool) -> None:
    ok = sum(1 for r in results if r.status == "success")
    fail = sum(1 for r in results if r.status == "failed")
    skip = sum(1 for r in results if r.status == "skipped")
    if quiet:
        return
    print("\n" + "=" * 40)
    print("SUMMARY")
    print("=" * 40)
    print(f"Total: {len(results)}")
    print(f"✓ Success: {ok}")
    print(f"✗ Failed : {fail}")
    print(f"○ Skipped: {skip}")
    print(f"Time    : {elapsed:.1f}s")


def cmd_extract(args: argparse.Namespace) -> int:
    roots = [Path(p) for p in (args.paths or ["."])]
    formats = _parse_formats(args.formats)
    archives = find_archives(roots, args.recursive, formats)
    if not archives:
        print("No archives found.")
        return 0
    if args.integrity_check:
        valid: list[Path] = []
        for a in archives:
            ok, msg = check_integrity(a)
            if not args.quiet:
                print(("✓ " if ok else "✗ ") + msg)
            if ok:
                valid.append(a)
        archives = valid
        if not archives:
            print("No valid archives to extract.")
            return 0
    if not args.quiet:
        print(
            f"Processing {len(archives)} archive(s)  engine={args.engine}  jobs={args.jobs}  organize={args.organize}"
        )
    opts = dict(
        engine=args.engine,
        keep=args.keep,
        dry_run=args.dry_run,
        organize=args.organize,
        out_dir=Path(args.output_dir) if args.output_dir else None,
        single_file_subdir=args.single_file_subdir,
        subdir_truncate=args.subdir_truncate,
        quiet=args.quiet,
    )
    start = time.time()
    results: list[ExtractResult]
    if args.jobs == 1 or len(archives) == 1:
        results = [extract_one(a, **opts) for a in archives]
    else:
        jobs = args.jobs if args.jobs > 0 else min(len(archives), mp.cpu_count())
        with mp.Pool(processes=jobs) as pool:
            results = pool.map(_worker, [(a, opts) for a in archives])
    elapsed = time.time() - start
    for r in results:
        line = str(r)
        if r.status == "failed":
            print(line, file=sys.stderr)
        else:
            print(line)
    _summary(results, elapsed, quiet=args.quiet)
    return 0 if all(r.status != "failed" for r in results) else 1


def fix_misextracted(root: Path, do_fix: bool, verbose: bool) -> int:
    count = 0
    dirs = sorted(
        (p for p in root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    )
    for d in dirs:
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        if len(entries) != 1:
            continue
        f = entries[0]
        if not f.is_file() or f.name != d.name:
            continue
        count += 1
        tag = "[FIX]" if do_fix else "[DRY]"
        if verbose or not do_fix:
            print(f"{tag} {d}/  →  file {f.name}")
        if not do_fix:
            continue
        try:
            tmp = d.parent / (d.name + ".fixtmp")
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
            shutil.move(str(d), str(tmp))
            shutil.move(str(tmp / d.name), str(d.parent / d.name))
            tmp.rmdir()
        except Exception as e:
            print(f"Failed to fix {d}: {e}", file=sys.stderr)
    return count


def cmd_fix(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    if not root.exists():
        print(f"Error: {root} does not exist", file=sys.stderr)
        return 1
    print(f"Scanning {root}")
    if not args.fix:
        print("DRY RUN — pass --fix to apply changes\n")
    n = fix_misextracted(root, do_fix=args.fix, verbose=args.verbose)
    print(f"\nSummary: {n} issue(s) {'fixed' if args.fix else 'found'}.")
    return 0


def cmd_whl(args: argparse.Namespace) -> int:
    roots = [Path(p) for p in (args.paths or ["."])]
    wheels: list[Path] = []
    for r in roots:
        if r.is_file() and r.suffix == ".whl":
            wheels.append(r)
        elif r.is_dir():
            wheels.extend(r.glob("*.whl"))
    if not wheels:
        print("No .whl files found.")
        return 0
    start = time.time()
    ok = 0
    fail = 0
    for w in wheels:
        try:
            dest = w.parent / strip_version(w.name)
            dest.mkdir(exist_ok=True)
            with zipfile.ZipFile(w) as zf:
                zf.extractall(dest)
            if not args.keep:
                w.unlink()
            ok += 1
            print(f"✓ {w.name} → {dest.name}/")
        except Exception as e:
            fail += 1
            print(f"✗ {w.name}: {e}", file=sys.stderr)
    if not args.quiet:
        print(f"\nDone: {ok} extracted, {fail} failed, {time.time() - start:.1f}s")
    return 0 if fail == 0 else 1


def cmd_check(args: argparse.Namespace) -> int:
    roots = [Path(p) for p in (args.paths or ["."])]
    archives = find_archives(roots, args.recursive, _parse_formats(args.formats))
    if not archives:
        print("No archives found.")
        return 0
    start = time.time()
    fail = 0
    for a in archives:
        ok, msg = check_integrity(a)
        stream = sys.stdout if ok else sys.stderr
        print(("✓ " if ok else "✗ ") + msg, file=stream)
        if not ok:
            fail += 1
    if not args.quiet:
        print(
            f"\nChecked {len(archives)} archive(s) in {time.time() - start:.1f}s "
            f"— {len(archives) - fail} valid, {fail} invalid."
        )
    return 0 if fail == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="archive_tool.py",
        description="Unified archive extractor / fixer / checker.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  archive_tool.py extract .\n"
            "  archive_tool.py extract -r --engine python .\n"
            "  archive_tool.py extract -k -n file.zip\n"
            "  archive_tool.py extract --organize stem .\n"
            "  archive_tool.py extract --integrity-check -r .\n"
            "  archive_tool.py fix --fix .\n"
            "  archive_tool.py whl .\n"
            "  archive_tool.py check -r .\n"
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    pe = sub.add_parser("extract", help="Extract archives.")
    pe.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Files or directories to process (default: cwd).",
    )
    pe.add_argument("-r", "--recursive", action="store_true", help="Recurse into directories.")
    pe.add_argument(
        "-e",
        "--engine",
        choices=("auto", "python", "external"),
        default="auto",
        help="Extraction backend (default: auto = try python, fall back to CLI tools).",
    )
    pe.add_argument(
        "-k",
        "--keep",
        action="store_true",
        help="Keep the original archive after extraction.",
    )
    pe.add_argument("-n", "--dry-run", action="store_true", help="Only print what would be done.")
    pe.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        help=f"Parallel workers (default: {DEFAULT_JOBS}; 1 = no pooling).",
    )
    pe.add_argument(
        "--organize",
        choices=("flat", "stem", "versioned"),
        default="flat",
        help="flat = extract next to the archive; "
        "stem = subdir named after archive stem; "
        "versioned = stem with version stripped (whl-style).",
    )
    pe.add_argument(
        "--single-file-subdir",
        action="store_true",
        help="ar_extract.py behaviour: only create a subdir when the "
        "archive's contents share a single top-level directory.",
    )
    pe.add_argument(
        "--subdir-truncate",
        type=int,
        default=0,
        metavar="N",
        help="Truncate subdir names to first N characters (subdir.py uses 8).",
    )
    pe.add_argument(
        "--formats",
        type=str,
        default=None,
        help="Comma-separated extensions to process (e.g. .tar.gz,.zip).",
    )
    pe.add_argument(
        "--integrity-check",
        action="store_true",
        help="Validate each archive before extracting.",
    )
    pe.add_argument(
        "-o",
        "--output-dir",
        default=None,
        help="Force a single output directory (overrides --organize).",
    )
    pe.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress and summary output.",
    )
    pe.set_defaults(func=cmd_extract)
    pf = sub.add_parser("fix", help="Fix mis-extracted dir/file name collisions.")
    pf.add_argument("root", nargs="?", default=".", help="Root directory to scan (default: cwd).")
    pf.add_argument(
        "--fix",
        action="store_true",
        help="Actually apply fixes (default is a dry-run).",
    )
    pf.add_argument("-v", "--verbose", action="store_true", help="Print every candidate.")
    pf.set_defaults(func=cmd_fix)
    pw = sub.add_parser("whl", help="Extract .whl wheels into version-stripped dirs.")
    pw.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Directories or .whl files (default: cwd).",
    )
    pw.add_argument(
        "-k",
        "--keep",
        action="store_true",
        help="Keep the .whl files after extraction.",
    )
    pw.add_argument("-q", "--quiet", action="store_true", help="Suppress summary output.")
    pw.set_defaults(func=cmd_whl)
    pc = sub.add_parser("check", help="Integrity-check archives without extracting.")
    pc.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Files or directories to scan (default: cwd).",
    )
    pc.add_argument("-r", "--recursive", action="store_true", help="Recurse into directories.")
    pc.add_argument("--formats", type=str, default=None, help="Comma-separated extensions to check.")
    pc.add_argument("-q", "--quiet", action="store_true", help="Suppress summary output.")
    pc.set_defaults(func=cmd_check)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
'''

_SOURCES['convert'] = r'''#!/data/data/com.termux/files/usr/bin/env python
"""(No content in stream)

---"""

from __future__ import annotations

import argparse
import bz2
import contextlib
import gzip
import io
import lzma
import os
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Callable, Optional

import brotli
import cramjam
import lz4.frame
import py7zr
import zstandard as zstd
from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
CHUNK: int = 1024 * 1024
"""Read/write block size for streaming codecs (matches originals)."""
DEFAULT_WORKERS: int = 8
"""Default worker count (matches the originals' `c` / `q4`)."""
DEFAULT_LEVEL: int = 9
"""Default compression level for codecs that accept one."""
TAR_CODECS: frozenset[str] = frozenset({"gz", "zst", "xz", "bz2", "lz4", "br", "7z"})
TAR_FORMATS: frozenset[str] = frozenset({
    ".tar",
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tar.zst",
    ".tar.br",
    ".tar.lz4",
    ".tar.7z",
    ".tar.sz",
})
ZIP_FORMATS: frozenset[str] = frozenset({".zip", ".whl"})
ARCHIVE_FORMATS: frozenset[str] = TAR_FORMATS | ZIP_FORMATS
_ARCHIVE_SUFFIXES: tuple[str, ...] = tuple(sorted(ARCHIVE_FORMATS, key=len, reverse=True))
WHEEL_SCRIPT_SUFFIXES: tuple[str, ...] = (".sh", ".py", ".exe")
TAR_DEFAULT_MODE: int = 0o644
TAR_SCRIPT_MODE: int = 0o755
TAR_MODE_MASK: int = 0o7777
TAR_UID: int = 0
TAR_GID: int = 0
TAR_UNAME: str = "root"
TAR_GNAME: str = "root"


def fmt_size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024.0:
            return f"{n:6.2f} {unit}"
        n /= 1024.0
    return f"{n:6.2f} PiB"


def dir_total_size(root: Path) -> int:
    total = 0
    for p in root.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def get_files(root: Path, exts: Iterable[str]) -> list[Path]:
    exts = tuple(exts)
    return sorted(p for p in root.iterdir() if p.is_file() and any(p.name.endswith(e) for e in exts))


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    stem, suf, parent = p.stem, p.suffix, p.parent
    i = 1
    while True:
        cand = parent / f"{stem}_{i}{suf}"
        if not cand.exists():
            return cand
        i += 1


def parallel_map(func: Callable, items: list[Any], workers: int = DEFAULT_WORKERS) -> list[Any]:
    if not items:
        return []
    if workers <= 1:
        return [func(x) for x in items]
    with Pool(processes=workers) as pool:
        return list(pool.map(func, items))


def parallel_imap(func: Callable, items: list[Any], workers: int = DEFAULT_WORKERS) -> list[Any]:
    if not items:
        return []
    if workers <= 1:
        return [func(x) for x in items]
    with Pool(processes=workers) as pool:
        return list(pool.imap_unordered(func, items))


def _copy_stream(src: BinaryIO, dst: BinaryIO) -> int:
    total = 0
    while True:
        chunk = src.read(CHUNK)
        if not chunk:
            break
        dst.write(chunk)
        total += len(chunk)
    return total


@contextlib.contextmanager
def open_decompressed(src: Path, codec: str) -> Iterator[BinaryIO]:
    if codec == "gz":
        with gzip.open(src, "rb") as f:
            yield f
    elif codec == "bz2":
        with bz2.open(src, "rb") as f:
            yield f
    elif codec == "xz":
        with lzma.open(src, "rb", format=lzma.FORMAT_XZ) as f:
            yield f
    elif codec == "zst":
        dec = zstd.ZstdDecompressor()
        with src.open("rb") as raw, dec.stream_reader(raw) as f:
            yield f
    elif codec == "lz4":
        with lz4.frame.open(src, "rb") as f:
            yield f
    elif codec == "br":
        yield io.BytesIO(brotli.decompress(src.read_bytes()))
    elif codec == "sz":
        yield io.BytesIO(bytes(cramjam.snappy.decompress(src.read_bytes())))
    else:
        msg = f"Unsupported source codec: {codec}"
        raise ValueError(msg)


@contextlib.contextmanager
def open_compressor(dst: Path, codec: str, level: int = DEFAULT_LEVEL) -> Iterator[BinaryIO]:
    if codec == "gz":
        with gzip.open(dst, "wb", compresslevel=level) as f:
            yield f
    elif codec == "bz2":
        with bz2.open(dst, "wb", compresslevel=level) as f:
            yield f
    elif codec == "xz":
        with lzma.open(dst, "wb", format=lzma.FORMAT_XZ, preset=level) as f:
            yield f
    elif codec == "zst":
        comp = zstd.ZstdCompressor(level=level)
        with dst.open("wb") as raw, comp.stream_writer(raw) as f:
            yield f
    elif codec == "lz4":
        with lz4.frame.open(dst, "wb") as f:
            yield f
    elif codec == "br":
        buf = io.BytesIO()
        yield buf
        dst.write_bytes(brotli.compress(buf.getvalue(), quality=11))
    elif codec == "sz":
        buf = io.BytesIO()
        yield buf
        dst.write_bytes(bytes(cramjam.snappy.compress(buf.getvalue())))
    else:
        msg = f"Unsupported target codec: {codec}"
        raise ValueError(msg)


def _parse_tar_codec_name(p: Path) -> Optional[tuple[str, str]]:
    parts = p.name.split(".")
    if len(parts) < 3 or parts[-2] != "tar":
        return None
    codec = parts[-1].lower()
    stem = ".".join(parts[:-2]) or "archive"
    return stem, codec


def _extract_7z_to_tar(src: Path, dst: Path) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tar7z_dec_"))
    try:
        with py7zr.SevenZipFile(src, mode="r") as z:
            z.extractall(path=tmp)
        inner = next(tmp.glob("*.tar"), None)
        if inner is None:
            files = [p for p in tmp.rglob("*") if p.is_file()]
            if not files:
                msg = "No files extracted from .tar.7z"
                raise RuntimeError(msg)
            inner = files[0]
        inner.replace(dst)
    finally:
        for p in tmp.rglob("*"):
            with contextlib.suppress(Exception):
                if p.is_file():
                    p.unlink()
        with contextlib.suppress(Exception):
            tmp.rmdir()


def _tar_codec_job(job: tuple[str, str, int]) -> tuple[str, bool, str]:
    src_str, target, level = job
    src = Path(src_str)
    parsed = _parse_tar_codec_name(src)
    if parsed is None:
        return src.name, False, "Not a *.tar.<codec> file"
    stem, src_codec = parsed
    if src_codec == target:
        return src.name, True, "Skipped (already target codec)"
    dst = src.with_name(f"{stem}.tar.{target}")
    if dst.exists():
        return src.name, True, f"Skipped (exists): {dst.name}"
    tmp = src.with_name(f".__tmp_tar_conv_{os.getpid()}_{stem}.tar")
    try:
        if src_codec == "7z":
            _extract_7z_to_tar(src, tmp)
        else:
            with open_decompressed(src, src_codec) as i, tmp.open("wb") as o:
                _copy_stream(i, o)
        if target == "7z":
            with py7zr.SevenZipFile(dst, mode="w") as z:
                z.write(tmp, arcname=tmp.name)
        else:
            with open_compressor(dst, target, level) as o, tmp.open("rb") as i:
                _copy_stream(i, o)
        src.unlink()
        return src.name, True, f"converted -> {dst.name} (removed original)"
    except Exception as e:
        with contextlib.suppress(Exception):
            if dst.exists():
                dst.unlink()
        return src.name, False, f"error: {e}"
    finally:
        with contextlib.suppress(Exception):
            if tmp.exists():
                tmp.unlink()


def run_tar_codec(args: argparse.Namespace) -> int:
    root: Path = args.root
    target: str = args.target
    files: list[Path] = []
    for p in root.rglob("*.tar.*"):
        if not p.is_file():
            continue
        parsed = _parse_tar_codec_name(p)
        if parsed and parsed[1] in TAR_CODECS:
            files.append(p)
    files.sort()
    if not files:
        print("No *.tar.<codec> files found recursively in current directory.")
        return 0
    initial = dir_total_size(root)
    results = parallel_imap(_tar_codec_job, [(str(p), target, args.level) for p in files], args.workers)
    final = dir_total_size(root)
    delta = final - initial
    ok = sum(1 for _, o, _ in results if o)
    print(f"Converted inputs: {len(files)}; OK: {ok}; Failed/Skipped: {len(files) - ok}")
    for name, o, msg in sorted(results, key=lambda x: x[0]):
        print(f"[{'OK' if o else 'FAIL'}] {name}: {msg}")
    print(f"Disk usage initial: {fmt_size(initial)}")
    print(f"Disk usage final:   {fmt_size(final)}")
    if delta < 0:
        print(f"Saved: {fmt_size(-delta)}")
    elif delta > 0:
        print(f"Extra used: {fmt_size(delta)}")
    else:
        print("No disk usage change (by summed file sizes).")
    return 0


def detect_archive_ext(p: Path) -> Optional[str]:
    name = p.name.lower()
    for ext in _ARCHIVE_SUFFIXES:
        if name.endswith(ext):
            return ext
    return None


def _iter_archive_members(src: Path, ext: str) -> Iterator[tuple[str, bytes]]:
    if ext in ZIP_FORMATS:
        with zipfile.ZipFile(src, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                with zf.open(info) as f:
                    yield info.filename, f.read()
        return
    if ext in TAR_FORMATS:
        codec = ext[5:] if ext != ".tar" else "tar"
        if codec == "tar":
            with src.open("rb") as raw, tarfile.open(fileobj=raw, mode="r|") as tf:
                for m in tf:
                    if not m.isfile():
                        continue
                    f = tf.extractfile(m)
                    if f is None:
                        continue
                    yield m.name, f.read()
        elif codec == "7z":
            with py7zr.SevenZipFile(str(src), mode="r") as z:
                payload = z.readall()
                for bio in payload.values():
                    data = bio.read()
                    with tarfile.open(fileobj=io.BytesIO(data), mode="r|") as tf:
                        for m in tf:
                            if not m.isfile():
                                continue
                            f = tf.extractfile(m)
                            if f is None:
                                continue
                            yield m.name, f.read()
                    return
            msg = f"empty 7z archive: {src}"
            raise ValueError(msg)
        else:
            with (
                open_decompressed(src, codec) as stream,
                tarfile.open(fileobj=stream, mode="r|") as tf,
            ):
                for m in tf:
                    if not m.isfile():
                        continue
                    f = tf.extractfile(m)
                    if f is None:
                        continue
                    yield m.name, f.read()
        return
    msg = f"unsupported input extension: {ext}"
    raise ValueError(msg)


def _write_archive(
    members: Iterable[tuple[str, bytes]],
    dst: Path,
    ext: str,
    level: int = DEFAULT_LEVEL,
) -> int:
    if ext in ZIP_FORMATS:
        total = 0
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in members:
                zf.writestr(name, data)
                total += len(data)
        return total
    if ext not in TAR_FORMATS:
        msg = f"unsupported output extension: {ext}"
        raise ValueError(msg)
    codec = ext[5:] if ext != ".tar" else "tar"
    if codec == "7z":
        tmp_tar: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
                tmp_tar = Path(tmp.name)
            with tmp_tar.open("wb") as raw, tarfile.open(fileobj=raw, mode="w|") as tf:
                total = _tar_add_members(tf, members)
            with py7zr.SevenZipFile(str(dst), mode="w") as z:
                z.write(str(tmp_tar), arcname="archive.tar")
            return total
        finally:
            if tmp_tar is not None:
                tmp_tar.unlink(missing_ok=True)
    if codec == "tar":
        with dst.open("wb") as raw, tarfile.open(fileobj=raw, mode="w|") as tf:
            return _tar_add_members(tf, members)
    with (
        open_compressor(dst, codec, level) as stream,
        tarfile.open(fileobj=stream, mode="w|") as tf,
    ):
        return _tar_add_members(tf, members)


def _tar_add_members(tf: tarfile.TarFile, members: Iterable[tuple[str, bytes]]) -> int:
    total = 0
    for name, data in members:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        total += len(data)
    return total


def _archive_convert_job(job: tuple[str, str, int]) -> tuple[str, int, bool, str]:
    src_str, target_ext, level = job
    src = Path(src_str)
    ext = detect_archive_ext(src)
    if ext is None:
        return src_str, 0, False, f"unsupported input format: {src.name}"
    stem = src.name
    for suf in _ARCHIVE_SUFFIXES:
        if stem.lower().endswith(suf):
            stem = stem[: -len(suf)]
            break
    dst = src.with_name(stem + target_ext)
    if dst == src:
        return src_str, 0, True, f"skipped (already {target_ext}): {src.name}"
    if dst.exists():
        return src_str, 0, True, f"skipped (exists): {dst.name}"
    try:
        in_size = src.stat().st_size
        _write_archive(_iter_archive_members(src, ext), dst, target_ext, level)
        out_size = dst.stat().st_size
        src.unlink()
        return (
            src_str,
            out_size - in_size,
            True,
            f"converted -> {dst.name}, removed original",
        )
    except Exception as e:
        with contextlib.suppress(Exception):
            if dst.exists():
                dst.unlink()
        return src_str, 0, False, f"error: {e}"


def run_archive_convert(args: argparse.Namespace) -> int:
    inputs: list[Path] = []
    if not args.inputs:
        inputs.extend(Path.cwd().iterdir())
    else:
        for a in args.inputs:
            p = Path(a)
            if p.is_dir():
                inputs.extend(p.iterdir())
            else:
                inputs.append(p)
    files: list[Path] = []
    for p in inputs:
        if not p.is_file():
            continue
        if detect_archive_ext(p) is None:
            logger.warning("Skipping unsupported file: {}", p.name)
            continue
        files.append(p)
    if not files:
        logger.warning("No convertible archives found.")
        return 0
    logger.info("Found {} archive(s); converting to {}", len(files), args.to)
    jobs = [(str(p), args.to, args.level) for p in files]
    results = parallel_imap(_archive_convert_job, jobs, args.workers)
    ok = sum(1 for _, _, o, _ in results if o)
    delta = sum(r for _, r, _, _ in results)
    logger.info("Summary: total={} ok={} failed/skipped={}", len(files), ok, len(files) - ok)
    for src, _, o, msg in sorted(results, key=lambda x: x[0]):
        logger.info("[{}] {}: {}", "OK" if o else "FAIL", Path(src).name, msg)
    if delta < 0:
        logger.info("Net space saved: {}", fmt_size(-delta))
    elif delta > 0:
        logger.info("Net extra used: {}", fmt_size(delta))
    else:
        logger.info("Net disk usage change: none")
    return 0


def run_br2zst(args: argparse.Namespace) -> int:
    root: Path = args.root
    files = sorted(p for p in root.rglob("*.json.br") if p.is_file())
    if not files:
        print(f"No .json.br files found under {root}")
        return 0
    comp = zstd.ZstdCompressor(level=args.level)
    total_in = total_out = 0
    print(f"Found {len(files)} file(s) under {root}\n")
    print(f"{'file':<60} {'br':>12} {'zst':>12} {'diff':>12}  {'%':>7}")
    print("-" * 108)
    for src in files:
        dst = src.with_suffix("").with_suffix(".json.zst")
        try:
            raw_bytes = src.read_bytes()
            raw = brotli.decompress(raw_bytes)
            zst_bytes = comp.compress(raw)
            dst.write_bytes(zst_bytes)
            src.unlink()
        except Exception as e:
            print(f"ERROR {src}: {e}")
            continue
        in_size = len(raw_bytes)
        out_size = len(zst_bytes)
        diff = out_size - in_size
        pct = diff / in_size * 100 if in_size else 0.0
        total_in += in_size
        total_out += out_size
        try:
            rel = src.relative_to(root)
        except ValueError:
            rel = src
        label = str(rel)
        if len(label) > 58:
            label = "..." + label[-55:]
        print(f"{label:<60} {fmt_size(in_size):>12} {fmt_size(out_size):>12} {fmt_size(diff):>12}  {pct:>6.2f}%")
    print("-" * 108)
    diff = total_out - total_in
    pct = diff / total_in * 100 if total_in else 0.0
    print(
        f"{'TOTAL (' + str(len(files)) + ' files)':<60} {fmt_size(total_in):>12} "
        f"{fmt_size(total_out):>12} {fmt_size(diff):>12}  {pct:>6.2f}%"
    )
    return 0


def _single_transcode(
    src: Path,
    dst_suffix: str,
    src_codec: str,
    dst_codec: str,
    level: int,
    legacy_percent: bool,
) -> tuple[str, bool, str]:
    dst = src.with_suffix(dst_suffix)
    try:
        with open_decompressed(src, src_codec) as f:
            raw = f.read()
        if dst_codec == "xz":
            out = lzma.compress(raw, format=lzma.FORMAT_XZ, preset=level)
        elif dst_codec == "gz":
            out = gzip.compress(raw, compresslevel=level)
        else:
            msg = f"unsupported dst codec: {dst_codec}"
            raise ValueError(msg)
        dst.write_bytes(out)
        if dst.exists() and dst.stat().st_size > 0:
            src_size = src.stat().st_size
            dst_size = dst.stat().st_size
            src.unlink()
            pct = (dst_size / src_size * (40 if legacy_percent else 100)) if src_size else 0
            return (
                str(src),
                True,
                f"Converted to {dst.name} ({src_size} -> {dst_size} bytes, {pct:.1f}%)",
            )
        return (str(src), False, "Output file is empty or missing")
    except Exception as e:
        with contextlib.suppress(Exception):
            if dst.exists():
                dst.unlink()
        return (str(src), False, f"Error: {e!s}")


def _report_single_transcode(results: list[tuple[str, bool, str]]) -> int:
    ok = fail = 0
    total_in = total_out = 0
    print("\n" + "=" * 40)
    print("CONVERSION RESULTS")
    print("-" * 40)
    for src, good, msg in results:
        if good:
            ok += 1
            print(f"✓ {msg}")
            if "bytes" in msg:
                with contextlib.suppress(Exception):
                    nums = msg.split("(")[1].split(")")[0].split("->")
                    total_in += int(nums[0].strip().split()[0])
                    total_out += int(nums[1].strip().split()[0])
        else:
            fail += 1
            print(f"✗ {src}: {msg}", file=sys.stderr)
    print("-" * 40)
    print(f"Summary: {ok} successful, {fail} failed")
    print(f"Total files processed: {len(results)}")
    if ok > 0 and total_in > 0:
        pct = (1 - total_out / total_in) * 100
        print(f"Total space saved: {total_in - total_out:,} bytes ({pct:.1f}%)")
        print(f"Original total: {total_in:,} bytes")
        print(f"New total: {total_out:,} bytes")
    if ok > 0:
        print("\nNote: Original files have been removed.")
    return 0 if fail == 0 else 1


def run_gz2xz(args: argparse.Namespace) -> int:
    files = get_files(args.root, [".gz"])
    if not files:
        print("No .gz files found to convert.")
        return 0
    jobs = [(str(p), ".xz", "gz", "xz", args.level, args.legacy_percent) for p in files]
    results = (
        [_single_transcode(Path(j[0]), j[1], j[2], j[3], j[4], j[5]) for j in jobs]
        if args.workers <= 1
        else parallel_map(
            lambda j: _single_transcode(Path(j[0]), j[1], j[2], j[3], j[4], j[5]),
            jobs,
            args.workers,
        )
    )
    return _report_single_transcode(results)


def run_xz2gz(args: argparse.Namespace) -> int:
    files = get_files(args.root, [".xz"])
    if not files:
        print("No .xz files found to convert.")
        return 0
    good_files = []
    for p in files:
        if p.is_symlink():
            print(f"symlink {p}: skipped")
            continue
        good_files.append(p)
    jobs = [(str(p), ".gz", "xz", "gz", args.level, args.legacy_percent) for p in good_files]
    results = (
        [_single_transcode(Path(j[0]), j[1], j[2], j[3], j[4], j[5]) for j in jobs]
        if args.workers <= 1
        else parallel_map(
            lambda j: _single_transcode(Path(j[0]), j[1], j[2], j[3], j[4], j[5]),
            jobs,
            args.workers,
        )
    )
    return _report_single_transcode(results)


def _zip_dt_to_ts(date_time: tuple[int, ...]) -> float:
    try:
        return datetime(*date_time).timestamp()
    except (ValueError, TypeError):
        return datetime.now().timestamp()


def _tar_xz_name_for_whl(whl: Path) -> Path:
    return whl.with_suffix(".tar.xz")


def _whl_name_for_tar_xz(src: Path) -> Path:
    name = src.name
    for suf in (".tar.xz", ".txz"):
        if name.lower().endswith(suf):
            return src.with_name(name[: -len(suf)] + ".whl")
    return src.with_suffix(".whl")


def _is_tar_xz(p: Path) -> bool:
    n = p.name.lower()
    return n.endswith((".tar.xz", ".txz"))


def _copy_zipinfo_to_tarinfo(zinfo: zipfile.ZipInfo, tinfo: tarfile.TarInfo) -> tarfile.TarInfo:
    tinfo.size = zinfo.file_size
    if zinfo.date_time:
        tinfo.mtime = _zip_dt_to_ts(zinfo.date_time)
    if zinfo.external_attr:
        mode = (zinfo.external_attr >> 16) & TAR_MODE_MASK
        tinfo.mode = mode or (TAR_SCRIPT_MODE if zinfo.filename.endswith(WHEEL_SCRIPT_SUFFIXES) else TAR_DEFAULT_MODE)
    else:
        tinfo.mode = TAR_DEFAULT_MODE
    tinfo.type = tarfile.REGTYPE
    tinfo.uid = TAR_UID
    tinfo.gid = TAR_GID
    tinfo.uname = TAR_UNAME
    tinfo.gname = TAR_GNAME
    return tinfo


def _copy_tarinfo_to_zipinfo(tinfo: tarfile.TarInfo, zinfo: zipfile.ZipInfo) -> zipfile.ZipInfo:
    if getattr(tinfo, "mtime", None):
        dt = datetime.fromtimestamp(tinfo.mtime)
        zinfo.date_time = (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
    if getattr(tinfo, "mode", None):
        zinfo.external_attr = (tinfo.mode & 0xFFFF) << 16
    return zinfo


def _whl_to_tar_xz(whl: Path, remove_original: bool) -> tuple[bool, str, Optional[Path]]:
    if not whl.exists() or not whl.is_file():
        return False, f"Invalid file: {whl}", None
    if whl.suffix.lower() != ".whl":
        return False, f"Not a wheel file: {whl.name}", None
    dst = _tar_xz_name_for_whl(whl)
    if dst.exists():
        dst = unique_path(dst)
        print(f"Target exists, using: {dst.name}")
    count = 0
    with zipfile.ZipFile(whl, "r") as zf:
        bad = zf.testzip()
        if bad:
            return False, f"Corrupt ZIP file: {bad}", None
        with tarfile.open(dst, "w:xz") as tf:
            for zinfo in zf.infolist():
                if zinfo.is_dir():
                    continue
                with zf.open(zinfo) as stream:
                    tinfo = tarfile.TarInfo(name=zinfo.filename)
                    tinfo = _copy_zipinfo_to_tarinfo(zinfo, tinfo)
                    tf.addfile(tinfo, stream)
                    count += 1
    if not (dst.exists() and dst.stat().st_size > 0):
        return False, "Output file is empty or missing", None
    if remove_original:
        try:
            whl.unlink()
            print(f"Removed original: {whl.name}")
        except OSError as e:
            return (
                False,
                f"Conversion succeeded but failed to remove original: {e}",
                dst,
            )
    return True, f"Converted {count} files to tar.xz", dst


def _tar_xz_to_whl(src: Path, remove_original: bool) -> tuple[bool, str, Optional[Path]]:
    if not src.exists() or not src.is_file():
        return False, f"Invalid file: {src}", None
    if not _is_tar_xz(src):
        return False, f"Not a tar.xz file: {src.name}", None
    dst = _whl_name_for_tar_xz(src)
    if dst.exists():
        dst = unique_path(dst)
        print(f"Target exists, using: {dst.name}")
    count = 0
    try:
        with (
            tarfile.open(src, "r:xz") as tf,
            zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf,
        ):
            for tinfo in tf.getmembers():
                if tinfo.isdir():
                    continue
                stream = tf.extractfile(tinfo)
                if stream is None:
                    continue
                zinfo = zipfile.ZipInfo(filename=tinfo.name)
                zinfo = _copy_tarinfo_to_zipinfo(tinfo, zinfo)
                zinfo.file_size = tinfo.size
                zf.writestr(zinfo, stream.read())
                stream.close()
                count += 1
    except tarfile.TarError as e:
        return False, f"Tar error: {e}", None
    except Exception as e:
        return False, f"Conversion error: {e}", None
    if not (dst.exists() and dst.stat().st_size > 0):
        return False, "Output file is empty or missing", None
    try:
        with zipfile.ZipFile(dst, "r") as zf:
            bad = zf.testzip()
            if bad:
                return False, f"Created corrupt zip file: {bad}", None
    except Exception as e:
        return False, f"Verification failed: {e}", None
    if remove_original:
        try:
            src.unlink()
            print(f"Removed original: {src.name}")
        except OSError as e:
            return (
                False,
                f"Conversion succeeded but failed to remove original: {e}",
                dst,
            )
    return True, f"Converted {count} files to wheel", dst


def _whl_txz_job(job: tuple[str, bool]) -> tuple[Path, bool, str, Optional[Path]]:
    p = Path(job[0])
    remove_original = job[1]
    if not p.exists():
        return p, False, f"File not found: {p}", None
    suf = p.suffix.lower()
    if suf == ".whl":
        ok, msg, out = _whl_to_tar_xz(p, remove_original)
    elif _is_tar_xz(p):
        ok, msg, out = _tar_xz_to_whl(p, remove_original)
    else:
        return p, False, f"Unsupported file type: {p.suffix}", None
    return p, ok, msg, out


def _setup_logger(verbose: bool, quiet: bool) -> None:
    logger.remove()
    if quiet:
        level = "ERROR"
    elif verbose:
        level = "DEBUG"
    else:
        level = "INFO"
    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> - <level>{level}</level> - <level>{message}</level>",
    )


def _collect_whl_txz_paths(paths: list[str], recursive: bool, to: str) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            logger.error(f"Path does not exist: {p}")
            continue
        if p.is_file():
            if p.suffix.lower() == ".whl" or _is_tar_xz(p):
                out.append(p)
            else:
                logger.warning(f"Skipping unsupported file: {p}")
        elif p.is_dir():
            glob = "**/*" if recursive else "*"
            found: list[Path] = []
            for f in p.glob(glob):
                if not f.is_file():
                    continue
                if f.suffix.lower() == ".whl":
                    if to in ("auto", "tar.xz"):
                        found.append(f)
                elif _is_tar_xz(f) and to in ("auto", "whl"):
                    found.append(f)
            out.extend(found)
            print(f"Found {len(found)} convertible files in {p}")
    return out


def run_whl_txz(args: argparse.Namespace) -> int:
    _setup_logger(args.verbose, args.quiet)
    files = _collect_whl_txz_paths(args.paths, args.recursive, args.to)
    if not files:
        if args.paths == ["."]:
            print("No .whl or .tar.xz files found in current directory")
        else:
            logger.error("No convertible files found")
        return 1
    print(f"Processing {len(files)} file(s)")
    if args.remove_original:
        print("Original files will be removed after successful conversion")
    jobs = [(str(p), args.remove_original) for p in files]
    results = parallel_imap(_whl_txz_job, jobs, args.workers) if args.workers > 1 else [_whl_txz_job(j) for j in jobs]
    ok = fail = 0
    lines = ["", "=" * 40, "CONVERSION RESULTS", "-" * 40]
    for src, good, msg, out in results:
        if good:
            ok += 1
            src_kind = "whl" if src.suffix.lower() == ".whl" else "tar.xz"
            dst_kind = "tar.xz" if out and out.suffix == ".xz" else "whl"
            size = ""
            if out and out.exists():
                size = f" ({out.stat().st_size / 1024:.1f} KB)"
            lines.append(f"✓ OK {src.name} [{src_kind}] → {out.name if out else 'unknown'} [{dst_kind}]{size}")
            if args.verbose:
                lines.append(f"   {msg}")
        else:
            fail += 1
            lines.append(f"✗ FAIL {src.name}: {msg}")
    lines.append("-" * 40)
    lines.append(f"Summary: {ok} successful, {fail} failed")
    if args.remove_original and ok > 0:
        lines.append("✓ Original files were removed after successful conversion")
    print("\n".join(lines))
    return 0 if fail == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="merged_archive_tools",
        description="Unified archive/compression converter (see module docstring for original→merged mapping).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("tar-codec", help="Change the codec of *.tar.<codec> files (recursive).")
    p.add_argument("target", choices=sorted(TAR_CODECS), help="Target codec, e.g. xz, zst, br, 7z.")
    p.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Directory to scan recursively (default: cwd).",
    )
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    p.add_argument("--level", type=int, default=DEFAULT_LEVEL)
    p.set_defaults(func=run_tar_codec)
    p = sub.add_parser("archive-convert", help="Convert archives between tar/zip/whl formats.")
    p.add_argument(
        "inputs",
        nargs="*",
        default=[],
        help="Files or directories (default: cwd entries).",
    )
    p.add_argument(
        "-t",
        "--to",
        required=True,
        choices=sorted(ARCHIVE_FORMATS),
        help="Target format, e.g. .tar.xz or .zip.",
    )
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    p.add_argument("--level", type=int, default=DEFAULT_LEVEL)
    p.set_defaults(func=run_archive_convert)
    p = sub.add_parser("br2zst", help="Recursively convert *.json.br -> *.json.zst.")
    p.add_argument("--root", type=Path, default=Path.cwd())
    p.add_argument("--level", type=int, default=DEFAULT_LEVEL)
    p.set_defaults(func=run_br2zst)
    for name, fn in (("gz2xz", run_gz2xz), ("xz2gz", run_xz2gz)):
        p = sub.add_parser(name, help=f"Convert *.{name[:2]} -> *.{name[3:]} in --root.")
        p.add_argument("--root", type=Path, default=Path.cwd())
        p.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
        p.add_argument("--level", type=int, default=DEFAULT_LEVEL)
        p.add_argument(
            "--legacy-percent",
            action="store_true",
            help="Reproduce the original buggy '*40' percent display.",
        )
        p.set_defaults(func=fn)
    p = sub.add_parser("whl-txz", help="Bidirectional .whl <-> .tar.xz converter.")
    p.add_argument(
        "paths",
        nargs="*",
        default=["."],
        help="Files or directories (default: current directory).",
    )
    p.add_argument("-r", "--recursive", action="store_true", help="Search directories recursively.")
    p.add_argument(
        "--remove-original",
        action="store_true",
        help="Delete source file after successful conversion.",
    )
    p.add_argument(
        "--to",
        choices=["auto", "whl", "tar.xz"],
        default="auto",
        help="Restrict direction when scanning directories.",
    )
    p.add_argument("-w", "--workers", type=int, default=DEFAULT_WORKERS)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")
    p.set_defaults(func=run_whl_txz)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "verbose", False) and getattr(args, "quiet", False):
        parser.error("--verbose and --quiet are mutually exclusive")
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        return 130
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''

_SOURCES['dirs'] = r'''#!/data/data/com.termux/files/usr/bin/env python
"""(No content in stream)

---"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import shutil
import sys
import tarfile
from pathlib import Path
from typing import Sequence


def positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 1:
        msg = "value must be >= 1"
        raise argparse.ArgumentTypeError(msg)
    return ivalue


def resolve_directory(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        msg = f"Error: Folder '{resolved}' not found."
        raise FileNotFoundError(msg)
    if not resolved.is_dir():
        msg = f"Error: '{resolved}' is not a directory."
        raise NotADirectoryError(msg)
    return resolved


def default_tar_output(source: Path) -> Path:
    return source.parent / f"{source.name}.tar"


def default_tgz_output(source: Path) -> Path:
    return source.parent / f"{source.name}.tar.gz"


def create_archive(source: Path, output: Path, mode: str) -> None:
    with tarfile.open(output, mode) as tar:
        tar.add(source, arcname=source.name)


def remove_path(path: Path, *, safe_symlinks: bool = False, quiet: bool = False) -> None:
    try:
        if path.is_file():
            path.unlink()
            if not quiet:
                print(f"Removed file:{path}")
        elif path.is_dir():
            if safe_symlinks and path.is_symlink():
                path.unlink()
                if not quiet:
                    print(f"Removed symlink:{path}")
            else:
                shutil.rmtree(path)
                if not quiet:
                    print(f"Removed directory:{path}")
    except Exception as e:
        print(f"Error removing '{path}':{e}", file=sys.stderr)


def _remove_path_worker(path_str: str) -> None:
    remove_path(Path(path_str), safe_symlinks=True, quiet=True)


def cleanup_entries_parallel(entries: Sequence[Path], workers: int) -> None:
    if not entries:
        return
    with mp.Pool(processes=workers) as pool:
        results = [pool.apply_async(_remove_path_worker, (str(p),)) for p in entries]
        for result in results:
            result.get()


def cleanup_contents(source: Path, output: Path, workers: int) -> None:
    output_resolved = output.resolve()
    entries = [p for p in source.iterdir() if p.resolve() != output_resolved]
    cleanup_entries_parallel(entries, workers)


def run_tar(args: argparse.Namespace) -> int:
    source = resolve_directory(args.folder)
    output = args.output or default_tar_output(source)
    output = output.expanduser().resolve()
    print(f"Creating archive:{output}")
    create_archive(source, output, "w")
    print("Archive created.")
    if not args.keep:
        print("Removing original folder...")
        remove_path(source, safe_symlinks=False, quiet=args.quiet)
    return 0


def run_tgz(args: argparse.Namespace) -> int:
    source = resolve_directory(args.folder)
    output = args.output or default_tgz_output(source)
    output = output.expanduser().resolve()
    print(f"Creating archive:{output}")
    create_archive(source, output, "w:gz")
    print("Archive created. Removing original files...")
    if not args.keep:
        if args.cleanup_scope == "folder":
            remove_path(source, safe_symlinks=False, quiet=True)
        elif args.cleanup_scope == "contents":
            cleanup_contents(source, output, args.workers)
    print("Cleanup complete.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="folder_archiver.py")
    subparsers = parser.add_subparsers(dest="command", required=True)
    tar_parser = subparsers.add_parser("tar")
    tar_parser.add_argument("folder", type=Path)
    tar_parser.add_argument("--output", type=Path, default=None)
    tar_parser.add_argument("--keep", action="store_true")
    tar_parser.add_argument("--quiet", action="store_true")
    tar_parser.set_defaults(func=run_tar)
    tgz_parser = subparsers.add_parser("tgz")
    tgz_parser.add_argument("folder", type=Path, nargs="?", default=Path.cwd())
    tgz_parser.add_argument("--output", type=Path, default=None)
    tgz_parser.add_argument("--workers", type=positive_int, default=8)
    tgz_parser.add_argument("--keep", action="store_true")
    tgz_parser.add_argument(
        "--cleanup-scope",
        choices=("contents", "folder", "none"),
        default="contents",
    )
    tgz_parser.set_defaults(func=run_tgz)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as e:
        print(f"Error:{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
'''

_SOURCES['7z'] = r'''#!/data/data/com.termux/files/usr/bin/env python
"""(No content in stream)

---"""

from __future__ import annotations

import argparse
import logging
import mmap
import multiprocessing
import shutil
import sys
import tarfile
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Optional

import py7zr

try:
    from loguru import logger as _log
except ImportError:
    _log = None
_BASIC_SKIP_SUFFIXES = {".7z", ".xz", ".br", ".zst", ".gz", ".zip", ".whl", ".log"}
_NAMED_SKIP_SUFFIXES = {".tar", ".7z", ".br", ".gz", ".xz", ".zip", ".whl"}
_FULL_SKIP_SUFFIXES = (".7z", ".xz", ".gz", ".bz2", ".br", ".zst", ".zip", ".rar")
_NAMED_METHODS = ("LZMA2", "LZMA", "PPMd")
_FULL_SETTINGS: dict[str, Any] = {
    "filters": [{"id": py7zr.FILTER_LZMA2, "preset": 9}],
    "dictionary_size": 268435456,
    "solid": True,
    "header_compression": True,
    "block_size": 4194304,
}


def _log_info(msg: str, *args: Any) -> None:
    if _log is not None:
        _log.info(msg.format(*args) if args else msg)
    else:
        logging.info(msg, *args)


def _log_warning(msg: str, *args: Any) -> None:
    if _log is not None:
        _log.warning(msg.format(*args) if args else msg)
    else:
        logging.warning(msg, *args)


def _log_error(msg: str, *args: Any) -> None:
    if _log is not None:
        _log.error(msg.format(*args) if args else msg)
    else:
        logging.error(msg, *args)


def _human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024.0:
            return f"{num:.2f} {unit}"
        num /= 1024.0
    return f"{num:.2f} PB"


def _sevenzip_write(
    dst: Path,
    preset: int = 9,
    filters: Optional[list] = None,
    **kwargs: Any,
) -> py7zr.SevenZipFile:
    if filters is None:
        filters = [{"id": py7zr.FILTER_LZMA2, "preset": preset}]
    return py7zr.SevenZipFile(dst, mode="w", filters=filters, **kwargs)


def _basic_setup_logging(log_file: Path) -> None:
    fmt = logging.Formatter("%(asctime)s [%(levelname)s]%(processName)s%(message)s")
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(fh)
    root.addHandler(sh)


def _basic_iter_dirs(base: Path):
    for p in base.iterdir():
        if p.is_dir() and not p.is_symlink():
            yield p


def _basic_iter_files(base: Path):
    for p in base.iterdir():
        if p.is_file() and not p.is_symlink() and p.suffix not in _BASIC_SKIP_SUFFIXES:
            yield p


def _basic_compress_dir(payload: tuple[str, int]) -> tuple[str, bool, str]:
    path_str, preset = payload
    path = Path(path_str)
    tar_path = path.with_suffix(".tar")
    archive_path = path.with_suffix(".tar.7z")
    try:
        if tar_path.exists():
            tar_path.unlink()
        with tarfile.open(tar_path, mode="w") as tar:
            tar.add(path, arcname=path.name)
        if archive_path.exists():
            archive_path.unlink()
        with _sevenzip_write(archive_path, preset=preset) as archive:
            archive.write(tar_path, arcname=tar_path.name)
        shutil.rmtree(path)
        tar_path.unlink(missing_ok=True)
        return (str(path), True, f"Compressed directory -> {archive_path.name}")
    except Exception as exc:
        logging.exception("Error compressing directory %s", path)
        try:
            if tar_path.exists():
                tar_path.unlink()
        except Exception:
            logging.exception("Failed to cleanup tar %s", tar_path)
        return (str(path), False, f"{type(exc).__name__}: {exc}")


def _basic_compress_file(payload: tuple[str, int]) -> tuple[str, bool, str]:
    path_str, preset = payload
    path = Path(path_str)
    archive_path = path.with_suffix(path.suffix + ".7z") if path.suffix else path.with_name(path.name + ".7z")
    try:
        if archive_path.exists():
            archive_path.unlink()
        with _sevenzip_write(archive_path, preset=preset) as archive:
            archive.write(path, arcname=path.name)
        path.unlink()
        return (str(path), True, f"Compressed file -> {archive_path.name}")
    except Exception as exc:
        logging.exception("Error compressing file %s", path)
        try:
            if archive_path.exists():
                archive_path.unlink()
        except Exception:
            logging.exception("Failed to cleanup archive %s", archive_path)
        return (str(path), False, f"{type(exc).__name__}: {exc}")


def run_basic(args: argparse.Namespace) -> int:
    base = Path.cwd()
    log_file = base / args.log_file
    _basic_setup_logging(log_file)
    logging.info("Starting compression in %s", base)
    preset = args.preset
    dirs = list(_basic_iter_dirs(base))
    if dirs:
        logging.info("Found %d top-level directories", len(dirs))
        with multiprocessing.Pool(processes=args.workers) as pool:
            for src, ok, msg in pool.imap_unordered(_basic_compress_dir, [(str(d), preset) for d in dirs]):
                (logging.info if ok else logging.error)("%s: %s", src, msg)
    else:
        logging.info("No top-level directories found")
    files = list(_basic_iter_files(base))
    if files:
        logging.info("Found %d top-level files", len(files))
        file_workers = max(1, multiprocessing.cpu_count() - 1)
        with multiprocessing.Pool(processes=file_workers) as pool:
            for src, ok, msg in pool.imap_unordered(_basic_compress_file, [(str(f), preset) for f in files]):
                (logging.info if ok else logging.error)("%s: %s", src, msg)
    else:
        logging.info("No top-level files found")
    logging.info("Done.")
    return 0


class NamedResult:
    def __init__(
        self,
        src: Optional[str] = None,
        dst: Optional[str] = None,
        ok: bool = False,
        error: Optional[str] = None,
    ) -> None:
        self.src = src
        self.dst = dst
        self.ok = ok
        self.error = error


def _named_iter_entries(base: Path, exclude_names: set):
    for p in base.iterdir():
        if p.name in exclude_names:
            continue
        if p.suffix in _NAMED_SKIP_SUFFIXES:
            continue
        yield p


def _named_remove(path: Path) -> None:
    if path.is_file() or path.is_symlink():
        path.unlink(missing_ok=True)
        return
    if path.is_dir():
        for child in path.iterdir():
            _named_remove(child)
        path.rmdir()


def _named_best_method():
    compressor = getattr(py7zr, "compressor", None)
    if compressor is None:
        return py7zr.FILTER_LZMA2
    for name in _NAMED_METHODS:
        if hasattr(compressor, name):
            return getattr(compressor, name)
    return py7zr.FILTER_LZMA2


def _named_compress_dir(path_str: str) -> NamedResult:
    path = Path(path_str)
    tar_path = path.parent / f"{path.name}.tar"
    try:
        if tar_path.exists():
            msg = f"{tar_path} exists"
            raise FileExistsError(msg)
        logging.info("Tar directory: %s -> %s", path, tar_path)
        with tarfile.open(tar_path, "w") as tar:
            tar.add(path, arcname=path.name)
        _named_remove(path)
        return NamedResult(str(path), str(tar_path), True)
    except Exception as exc:
        logging.exception("Directory failed: %s", path)
        return NamedResult(str(path), str(tar_path), False, str(exc))


def _named_compress_file(path_str: str) -> NamedResult:
    path = Path(path_str)
    archive_path = path.parent / f"{path.name}.7z"
    method = _named_best_method()
    try:
        if archive_path.exists():
            msg = f"{archive_path} exists"
            raise FileExistsError(msg)
        logging.info("Compress file: %s -> %s", path, archive_path)
        filters = [{"id": method, "preset": 9}]
        with py7zr.SevenZipFile(archive_path, mode="w", filters=filters) as archive:
            archive.write(path, arcname=path.name)
        _named_remove(path)
        return NamedResult(str(path), str(archive_path), True)
    except Exception as exc:
        logging.exception("File failed: %s", path)
        return NamedResult(str(path), str(archive_path), False, str(exc))


def run_named(args: argparse.Namespace) -> int:
    base = Path.cwd()
    log_file = base / args.log_file
    _basic_setup_logging(log_file)
    workers = max(1, multiprocessing.cpu_count() - 1)
    method = _named_best_method()
    logging.info("Base dir: %s", base)
    logging.info("Workers: %d", workers)
    logging.info("Best py7zr method: %s", method)
    exclude = {log_file.name}
    if "__file__" in globals():
        exclude.add(Path(__file__).name)
    entries = list(_named_iter_entries(base, exclude))
    dirs = [p for p in entries if p.is_dir()]
    files = [p for p in entries if p.is_file()]
    logging.info("Found %d dirs and %d files", len(dirs), len(files))
    results: list[NamedResult] = []
    if dirs:
        with multiprocessing.Pool(processes=min(workers, len(dirs))) as pool:
            results.extend(pool.map(_named_compress_dir, [str(d) for d in dirs]))
    if files:
        with multiprocessing.Pool(processes=min(workers, len(files))) as pool:
            results.extend(pool.map(_named_compress_file, [str(f) for f in files]))
    ok = sum(1 for r in results if r.ok)
    fail = len(results) - ok
    logging.info("Completed. success=%d fail=%d", ok, fail)
    for r in results:
        if not r.ok:
            logging.error("FAILED: %s -> %s | %s", r.src, r.dst, r.error)
    return 0


def _full_is_compressible(path: Path, min_size: int) -> bool:
    try:
        if not path.is_file() or path.is_symlink():
            return False
        if path.suffix in _FULL_SKIP_SUFFIXES:
            return False
        return path.stat().st_size >= min_size
    except (OSError, PermissionError):
        return False


def _full_list_files(base: Path, min_size: int, mode: str = "compress") -> list[Path]:
    if mode == "compress":
        return sorted(
            p for p in base.glob("*") if p.is_file() and not p.is_symlink() and _full_is_compressible(p, min_size)
        )
    return sorted(p for p in base.glob("*.7z") if p.is_file() and not p.is_symlink())


def _full_list_dirs(base: Path) -> list[Path]:
    return sorted(p for p in base.glob("*") if not p.is_symlink() and p.is_dir())


def _full_compress_inmem(src: Path, dst: Path) -> bool:
    try:
        data = src.read_bytes()
        if not data:
            return False
        tmp = Path(tempfile.mkstemp(suffix=".7z")[1])
        try:
            with py7zr.SevenZipFile(
                tmp,
                mode="w",
                filters=_FULL_SETTINGS["filters"],
                dictionary_size=_FULL_SETTINGS["dictionary_size"],
                solid=_FULL_SETTINGS["solid"],
                header_compression=_FULL_SETTINGS["header_compression"],
            ) as archive:
                archive.write(src, arcname=src.name)
            dst.write_bytes(tmp.read_bytes())
            return True
        finally:
            tmp.unlink(missing_ok=True)
    except Exception as exc:
        _log_error("Memory compression failed for {}: {}", src.name, exc)
        return False


def _full_compress_chunk(payload: tuple[bytes, int, str, int]) -> Optional[str]:
    chunk_bytes, index, temp_dir_str, preset = payload
    temp_dir = Path(temp_dir_str)
    bin_path = temp_dir / f"chunk_{index:06d}.bin"
    archive_path = temp_dir / f"chunk_{index:06d}.7z"
    try:
        bin_path.write_bytes(chunk_bytes)
        with py7zr.SevenZipFile(
            archive_path,
            mode="w",
            filters=[{"id": py7zr.FILTER_LZMA2, "preset": preset}],
        ) as archive:
            archive.write(bin_path, arcname=bin_path.name)
        return str(archive_path)
    finally:
        if bin_path.exists():
            bin_path.unlink()


def _full_compress_chunked(
    src: Path,
    dst: Path,
    size: int,
    chunk_size: int,
    workers: int,
) -> bool:
    temp_dir = Path(tempfile.gettempdir()) / "py7zr_temp" / f"compress_{src.stem}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        num_chunks = (size + chunk_size - 1) // chunk_size
        payloads: list[tuple[bytes, int, str, int]] = []
        with src.open("rb") as fh, mmap.mmap(fh.fileno(), length=0, access=mmap.ACCESS_READ) as mm:
            for i in range(num_chunks):
                start = i * chunk_size
                end = min((i + 1) * chunk_size, size)
                payloads.append((mm[start:end], i, str(temp_dir), 9))
        chunk_paths: list[Optional[str]] = [None] * num_chunks
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for i, result in enumerate(pool.map(_full_compress_chunk, payloads)):
                chunk_paths[i] = result
        with py7zr.SevenZipFile(
            dst,
            mode="w",
            filters=_FULL_SETTINGS["filters"],
            dictionary_size=_FULL_SETTINGS["dictionary_size"],
            solid=_FULL_SETTINGS["solid"],
            header_compression=_FULL_SETTINGS["header_compression"],
        ) as archive:
            for chunk_path in chunk_paths:
                if chunk_path is not None:
                    cp = Path(chunk_path)
                    if cp.exists():
                        archive.write(cp, arcname=cp.name)
        return True
    except Exception as exc:
        _log_error("Chunked compression failed for {}: {}", src.name, exc)
        return False
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


def _full_compress_dir(dir_path: Path, archive_path: Path) -> bool:
    try:
        with py7zr.SevenZipFile(
            archive_path,
            mode="w",
            filters=_FULL_SETTINGS["filters"],
            dictionary_size=_FULL_SETTINGS["dictionary_size"],
            solid=_FULL_SETTINGS["solid"],
            header_compression=_FULL_SETTINGS["header_compression"],
            recursive=True,
        ) as archive:
            archive.writeall(dir_path, arcname=dir_path.name)
        if not archive_path.exists():
            return False
        orig = sum(f.stat().st_size for f in dir_path.rglob("*") if f.is_file())
        comp = archive_path.stat().st_size
        if comp < orig:
            shutil.rmtree(dir_path)
            saved = (orig - comp) / orig * 100
            print(f"  ✓ Compressed archive: {saved:.1f}% saved ({_human_size(orig)} -> {_human_size(comp)})")
            return True
        _log_warning("  ✗ Archive compression didn't save space")
        archive_path.unlink()
        return False
    except Exception as exc:
        _log_error("Failed to compress folder {}: {}", dir_path.name, exc)
        if archive_path.exists():
            archive_path.unlink()
        return False


def _full_compress_file(
    src: Path,
    chunk_size: int,
    min_size: int,
    workers: int,
) -> tuple[bool, int, int]:
    dst = src.with_suffix(src.suffix + ".7z")
    if dst.exists():
        print(f"Skipping {src.name} - output already exists")
        return (False, 0, 0)
    try:
        size = src.stat().st_size
        if not size:
            return (False, 0, 0)
        if size < chunk_size:
            ok = _full_compress_inmem(src, dst)
        else:
            ok = _full_compress_chunked(src, dst, size, chunk_size, workers)
        if ok and dst.exists():
            comp = dst.stat().st_size
            if comp == 0:
                _log_warning("Compressed file empty for {}", src.name)
                dst.unlink()
                return (False, 0, 0)
            if comp < size:
                src.unlink()
                saved = (size - comp) / size * 100
                print(f"  ✓ {src.name}: {saved:.1f}% saved ({_human_size(size)} -> {_human_size(comp)})")
                return (True, size, comp)
            _log_warning("  ✗ {}: No space saved, removing compressed file", src.name)
            dst.unlink()
            return (False, 0, 0)
        return (False, 0, 0)
    except Exception as exc:
        _log_error("  ✗ Failed to compress {}: {}", src.name, exc)
        return (False, 0, 0)


def _full_decompress_one(archive: Path) -> tuple[bool, int, int]:
    out = archive.with_suffix("")
    try:
        with py7zr.SevenZipFile(archive, mode="r") as zf:
            comp_size = archive.stat().st_size
            if out.exists():
                _log_warning("  Output already exists, skipping...")
                return (False, 0, 0)
            zf.extractall(path=out)
            if out.is_file():
                decomp_size = out.stat().st_size
            else:
                decomp_size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
            print(f"  ✓ Decompressed {archive.name}: {_human_size(comp_size)} → {_human_size(decomp_size)}")
            archive.unlink()
            return (True, comp_size, decomp_size)
    except Exception as exc:
        _log_error("  ✗ Failed to decompress {}: {}", archive.name, exc)
        return (False, 0, 0)


def _full_compress(base: Path, args: argparse.Namespace) -> None:
    base.mkdir(parents=True, exist_ok=True)
    print("\n🔧 7-Zip Compression Settings:")
    print("   Format: 7z")
    print(f"   Filter: LZMA2 (preset {args.preset} - maximum)")
    print(f"   Dictionary size: {_FULL_SETTINGS['dictionary_size'] // (1024 * 1024)} MB")
    print("   Solid compression: Yes")
    print("   Header compression: Yes")
    print("   Block size: 4 MB")
    print(f"   Parallel workers: {args.workers}")
    print(f"   Chunk size: {_human_size(args.chunk_size)}")
    dirs = _full_list_dirs(base)
    if dirs:
        print(f"\n📁 Compressing {len(dirs)} directories...")
        for d in dirs:
            rel = d.relative_to(base)
            print(f"\n  Processing {rel}...")
            archive = d.parent / f"{d.name}.7z"
            if _full_compress_dir(d, archive):
                _log_info("  ✓ Successfully compressed {} to {}.7z", rel, d.name)
            else:
                _log_error("  ✗ Failed to compress {}", rel)
    files = _full_list_files(base, args.min_size, mode="compress")
    if not files:
        print("\n📄 No files to compress")
        return
    print(f"\n📄 Compressing {len(files)} files with 7-Zip max compression...")
    orig_total = comp_total = ok_count = 0
    for i, f in enumerate(files, 1):
        print(f"\n[{i}/{len(files)}] {f.name}")
        ok, orig, comp = _full_compress_file(f, args.chunk_size, args.min_size, args.workers)
        if ok:
            ok_count += 1
            orig_total += orig
            comp_total += comp
    if ok_count > 0:
        saved = orig_total - comp_total
        pct = saved / orig_total * 100 if orig_total else 0.0
        print("\n" + "=" * 40)
        print(f"✅ Compressed {ok_count}/{len(files)} files")
        print(f"📊 Original size: {_human_size(orig_total)}")
        print(f"📦 Compressed size: {_human_size(comp_total)}")
        print(f"💾 Space saved: {_human_size(saved)} ({pct:.1f}%)")
        print("=" * 40)
    elif files:
        _log_error("\n❌ No files were successfully compressed")


def _full_decompress(base: Path, args: argparse.Namespace) -> None:
    archives = _full_list_files(base, args.min_size, mode="decompress")
    if not archives:
        print("\n📄 No .7z files to decompress")
        return
    print(f"\n📄 Decompressing {len(archives)} 7-Zip archives...")
    comp_total = decomp_total = ok_count = 0
    for i, a in enumerate(archives, 1):
        print(f"\n[{i}/{len(archives)}] {a.name}")
        ok, comp, decomp = _full_decompress_one(a)
        if ok:
            ok_count += 1
            comp_total += comp
            decomp_total += decomp
    if ok_count > 0:
        print("\n" + "=" * 40)
        print(f"✅ Decompressed {ok_count}/{len(archives)} archives")
        print(f"📦 Compressed size: {_human_size(comp_total)}")
        print(f"📊 Decompressed size: {_human_size(decomp_total)}")
        print("=" * 40)
    elif archives:
        _log_error("\n❌ No files were successfully decompressed")


def run_full(args: argparse.Namespace) -> int:
    base = Path.cwd()
    if args.mode == "compress":
        _full_compress(base, args)
    else:
        _full_decompress(base, args)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Merged 7-Zip compression tool")
    sub = parser.add_subparsers(dest="command", required=True)
    p_basic = sub.add_parser("basic", help="7zer.py: tar->7z dirs + 7z files")
    p_basic.add_argument("--log-file", default="compress.log")
    p_basic.add_argument("--workers", type=int, default=4)
    p_basic.add_argument("--preset", type=int, default=9)
    p_basic.set_defaults(func=run_basic)
    p_named = sub.add_parser("named", help="7zer2.py: dirs->tar, files->7z, best method")
    p_named.add_argument("--log-file", default="compress.log")
    p_named.set_defaults(func=run_named)
    p_full = sub.add_parser("full", help="7zr.py: full compress/decompress")
    p_full.add_argument(
        "--mode",
        choices=["compress", "decompress"],
        default="compress",
        help="Compress or decompress; matches the original -c/-d flags",
    )
    p_full.add_argument("--workers", type=int, default=8)
    p_full.add_argument("--chunk-size", type=int, default=524288)
    p_full.add_argument("--min-size", type=int, default=1024)
    p_full.add_argument("--preset", type=int, default=9)
    p_full.set_defaults(func=run_full)
    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        _log_warning("\n⚠️  Interrupted by user")
        return 1
    except Exception as exc:
        _log_error("\n❌ Unexpected error: {}", exc)
        return 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
'''

_SOURCES['brotli'] = r'''#!/data/data/com.termux/files/usr/bin/env python
import argparse
import hashlib
import multiprocessing as mp
import os
import shutil
import struct
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from multiprocessing import Pool, cpu_count
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence

import brotli

try:
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
    from rich.table import Table
    from rich.text import Text

    RICH_AVAILABLE: bool = True
except ImportError:
    RICH_AVAILABLE = False
    print("💡 Tip: Install 'rich' for prettier output: pip install rich")
DEFAULT_CHUNK_SIZE: int = 4 * 1024 * 1024
MIN_CHUNK_SIZE: int = 64 * 1024
EXCLUDED_EXTENSIONS: set[str] = {
    ".br",
    ".xz",
    ".zst",
    ".zstd",
    ".7z",
    ".gz",
    ".bz2",
    ".zip",
    ".rar",
    ".tar",
    ".tgz",
    ".tbz2",
    ".txz",
    ".tlz",
    ".lz",
    ".lz4",
    ".lzma",
    ".lzo",
    ".sz",
    ".snappy",
    ".zlib",
    ".deflate",
    ".flac",
    ".mp3",
    ".aac",
    ".ogg",
    ".wma",
    ".opus",
    ".m4a",
    ".wavpack",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".avif",
    ".heic",
    ".heif",
    ".mp4",
    ".avi",
    ".mkv",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".m4v",
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".odt",
    ".ods",
    ".odp",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".wasm",
    ".whl",
    ".egg",
    ".deb",
    ".rpm",
    ".apk",
    ".ipa",
    ".pyc",
    ".pyo",
    ".class",
    ".o",
    ".obj",
    ".lib",
    ".a",
    ".iso",
    ".img",
    ".dmg",
    ".vdi",
    ".vmdk",
    ".qcow2",
}
EXCLUDED_DIRS: set[str] = {
    ".git",
    ".svn",
    ".hg",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".env",
}


class ParallelBrotliCompressor:
    MAGIC: bytes = b"PBRT0001"

    def __init__(
        self,
        quality: int = 1,
        chunk_size: int = 4 * 1024 * 1024,
        workers: Optional[int] = None,
        mode: int = brotli.MODE_GENERIC,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        if not 0 <= quality <= 11:
            raise ValueError("quality must be between 0 and 11")
        if chunk_size < 64 * 1024:
            raise ValueError("chunk_size should be at least 64KB")
        self.quality: int = quality
        self.chunk_size: int = chunk_size
        self.workers: int = workers or cpu_count()
        self.mode: int = mode
        self.progress_callback: Optional[Callable[[int, int], None]] = progress_callback

    def compress_file(self, input_path: str, output_path: str) -> None:
        in_path: Path = Path(input_path)
        out_path: Path = Path(output_path)
        total_size: int = in_path.stat().st_size
        tasks = self._iter_chunk_tasks(in_path)
        bytes_done: int = 0
        with open(out_path, "wb") as f_out:
            f_out.write(self.MAGIC)
            count_pos: int = f_out.tell()
            f_out.write(struct.pack("<I", 0))
            chunk_count: int = 0
            with Pool(processes=self.workers) as pool:
                for chunk_index, compressed, orig_len in pool.imap(
                    _compress_worker,
                    tasks,
                    chunksize=1,
                ):
                    f_out.write(struct.pack("<I", len(compressed)))
                    f_out.write(struct.pack("<I", orig_len))
                    f_out.write(compressed)
                    chunk_count += 1
                    bytes_done += orig_len
                    if self.progress_callback:
                        self.progress_callback(bytes_done, total_size)
            end_pos: int = f_out.tell()
            f_out.seek(count_pos)
            f_out.write(struct.pack("<I", chunk_count))
            f_out.seek(end_pos)

    @staticmethod
    def decompress_file(input_path: str, output_path: str) -> None:
        with open(input_path, "rb") as f_in, open(output_path, "wb") as f_out:
            magic: bytes = f_in.read(len(ParallelBrotliCompressor.MAGIC))
            if magic != ParallelBrotliCompressor.MAGIC:
                raise ValueError("Not a ParallelBrotli container")
            (chunk_count,) = struct.unpack("<I", f_in.read(4))
            for _ in range(chunk_count):
                (comp_len,) = struct.unpack("<I", f_in.read(4))
                (orig_len,) = struct.unpack("<I", f_in.read(4))
                payload: bytes = f_in.read(comp_len)
                if len(payload) != comp_len:
                    raise IOError("Truncated chunk payload")
                f_out.write(brotli.decompress(payload))

    def compress_bytes(self, data: bytes) -> bytes:
        with tempfile.NamedTemporaryFile(delete=False) as tmp_in:
            tmp_in.write(data)
            tmp_in_path: str = tmp_in.name
        tmp_out_fd, tmp_out_path = tempfile.mkstemp()
        os.close(tmp_out_fd)
        try:
            self.compress_file(tmp_in_path, tmp_out_path)
            with open(tmp_out_path, "rb") as f:
                return f.read()
        finally:
            os.unlink(tmp_in_path)
            os.unlink(tmp_out_path)

    def _iter_chunk_tasks(self, input_path: Path) -> Iterator[tuple[int, bytes, int, int]]:
        index: int = 0
        with open(input_path, "rb") as f:
            while True:
                chunk: bytes = f.read(self.chunk_size)
                if not chunk:
                    break
                yield (index, chunk, self.quality, self.mode)
                index += 1


def _compress_worker(task: tuple[int, bytes, int, int]) -> tuple[int, bytes, int]:
    index, chunk, quality, mode = task
    compressed: bytes = brotli.compress(chunk, quality=quality, mode=mode)
    return index, compressed, len(chunk)


@dataclass
class CompressionResult:
    file_path: Path
    original_size: int
    processed_size: int
    success: bool
    error: Optional[str] = None
    duration: float = 0.0
    original_deleted: bool = False
    operation: str = "compress"
    was_tarred: bool = False
    verified: bool = False


def format_size(size_bytes: float) -> str:
    size: float = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"


def directory_size(directory: Path) -> int:
    total: int = 0
    for item in directory.rglob("*"):
        if item.is_file() and not item.is_symlink():
            total += item.stat().st_size
    return total


def compressed_output_path(path: Path) -> Path:
    return path.with_name(path.name + ".br")


def decompressed_output_path(path: Path) -> Path:
    if path.name.endswith(".br") and len(path.name) > 3:
        return path.with_name(path.name[:-3])
    return path.with_name(path.name + ".out")


def relative_display(path: Path, directory: Path) -> str:
    try:
        return str(path.relative_to(directory))
    except ValueError:
        return str(path)


def _hash_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def compress_with_parallel(
    input_path: Path,
    output_path: Path,
    quality: int,
    chunk_size: int,
    workers: int,
    hash_input: bool,
) -> tuple[int, Optional[str]]:
    compressor: ParallelBrotliCompressor = ParallelBrotliCompressor(
        quality=quality,
        chunk_size=chunk_size,
        workers=workers,
    )
    compressor.compress_file(str(input_path), str(output_path))
    written: int = output_path.stat().st_size
    source_digest: Optional[str] = None
    if hash_input:
        source_digest = _hash_file(input_path)
    return written, source_digest


def decompress_with_parallel(
    input_path: Path,
    output_path: Optional[Path],
    hash_output: bool,
    allow_truncated: bool = False,
) -> tuple[int, Optional[str]]:
    tmp_path: Optional[Path] = None
    target: Path
    if output_path is None:
        fd, tmp_name = tempfile.mkstemp()
        os.close(fd)
        tmp_path = Path(tmp_name)
        target = tmp_path
    else:
        target = output_path
    try:
        try:
            ParallelBrotliCompressor.decompress_file(str(input_path), str(target))
        except (IOError, ValueError):
            if not allow_truncated:
                raise
        written: int = target.stat().st_size
        digest: Optional[str] = None
        if hash_output:
            digest = _hash_file(target)
        return written, digest
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()


def tar_directory(directory: Path, output_path: Path, delete_original: bool = False) -> tuple[int, bool]:
    try:
        with tarfile.open(output_path, "w") as tar:
            tar.add(directory, arcname=directory.name)
        tar_size: int = output_path.stat().st_size
        if delete_original and output_path.exists():
            shutil.rmtree(directory)
        return tar_size, True
    except Exception:
        if output_path.exists():
            output_path.unlink()
        raise


def compress_file_streaming(
    input_path: Path,
    output_path: Path,
    quality: int = 11,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    keep_original: bool = False,
    was_tarred: bool = False,
    verify: bool = False,
    workers: int = 1,
) -> CompressionResult:
    start_time: float = time.time()
    try:
        original_size: int = input_path.stat().st_size
        if original_size == 0:
            return CompressionResult(
                file_path=input_path,
                original_size=0,
                processed_size=0,
                success=False,
                error="Empty file",
                duration=time.time() - start_time,
                operation="compress",
                was_tarred=was_tarred,
            )
        compressed_size, source_digest = compress_with_parallel(
            input_path, output_path, quality, chunk_size, workers, verify
        )
        if verify:
            check_size, check_digest = decompress_with_parallel(output_path, None, True)
            if check_size != original_size or check_digest != source_digest:
                msg = "Verification failed: round-trip does not match source"
                raise ValueError(msg)
        original_deleted: bool = False
        if not keep_original and output_path.exists():
            input_path.unlink()
            original_deleted = True
        return CompressionResult(
            file_path=input_path,
            original_size=original_size,
            processed_size=compressed_size,
            success=True,
            duration=time.time() - start_time,
            original_deleted=original_deleted,
            operation="compress",
            was_tarred=was_tarred,
            verified=verify,
        )
    except Exception as e:
        if output_path.exists():
            output_path.unlink()
        return CompressionResult(
            file_path=input_path,
            original_size=input_path.stat().st_size if input_path.exists() else 0,
            processed_size=0,
            success=False,
            error=f"{type(e).__name__}: {e}" if str(e) else type(e).__name__,
            duration=time.time() - start_time,
            operation="compress",
            was_tarred=was_tarred,
        )


def decompress_file_streaming(
    input_path: Path,
    output_path: Path,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    keep_original: bool = False,
    allow_truncated: bool = False,
    workers: int = 1,
) -> CompressionResult:
    start_time: float = time.time()
    try:
        original_size: int = input_path.stat().st_size
        if original_size == 0:
            return CompressionResult(
                file_path=input_path,
                original_size=0,
                processed_size=0,
                success=False,
                error="Empty file",
                duration=time.time() - start_time,
                operation="decompress",
            )
        decompressed_size, _ = decompress_with_parallel(input_path, output_path, False, allow_truncated)
        original_deleted: bool = False
        if not keep_original and output_path.exists():
            input_path.unlink()
            original_deleted = True
        return CompressionResult(
            file_path=input_path,
            original_size=original_size,
            processed_size=decompressed_size,
            success=True,
            duration=time.time() - start_time,
            original_deleted=original_deleted,
            operation="decompress",
        )
    except Exception as e:
        if output_path.exists():
            output_path.unlink()
        return CompressionResult(
            file_path=input_path,
            original_size=input_path.stat().st_size if input_path.exists() else 0,
            processed_size=0,
            success=False,
            error=f"{type(e).__name__}: {e}" if str(e) else type(e).__name__,
            duration=time.time() - start_time,
            operation="decompress",
        )


def untar_file(tar_path: Path, extract_dir: Path, delete_tar: bool = False) -> bool:
    try:
        with tarfile.open(tar_path, "r") as tar:
            try:
                tar.extractall(extract_dir, filter="data")
            except TypeError:
                tar.extractall(extract_dir)
        if delete_tar:
            tar_path.unlink()
        return True
    except Exception as e:
        print(f"  ❌ Error extracting tar {tar_path.name}: {e}")
        return False


def find_subdirs_to_tar(directories: Sequence[Path], exclude_patterns: Optional[list[str]] = None) -> list[Path]:
    patterns: list[str] = exclude_patterns or []
    subdirs: list[Path] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        for d in directory.iterdir():
            if not d.is_dir() or d.is_symlink():
                continue
            if d.name in EXCLUDED_DIRS:
                continue
            if patterns and any(pattern in str(d) for pattern in patterns):
                continue
            subdirs.append(d)
    return sorted(set(subdirs))


def report_tar_result(result: CompressionResult, index: int, total: int) -> None:
    if result.success:
        ratio: float = (1 - result.processed_size / result.original_size) * 100 if result.original_size else 0.0
        print(f"  ✅ [{index}/{total}] {result.file_path.name} → {result.file_path.name}.br")
        print(
            f"     {format_size(result.original_size)} → {format_size(result.processed_size)} "
            f"({ratio:.1f}% compression)"
        )
    else:
        print(f"  ❌ [{index}/{total}] {result.file_path.name}: {result.error}")


def process_subdirs_with_tar(
    subdirs: list[Path],
    quality: int = 11,
    workers: int = 4,
    keep_original: bool = False,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    verify: bool = False,
) -> list[CompressionResult]:
    results: list[CompressionResult] = []
    if not subdirs:
        print("📁 No subdirectories found to tar")
        return results
    print(f"📁 Found {len(subdirs)} subdirectories to tar first")
    print("🗜️  Step 1: Creating tar archives of subdirectories...")
    tar_files: list[Path] = []
    tar_errors: list[tuple[Path, str]] = []
    for i, subdir in enumerate(subdirs, 1):
        try:
            tar_path: Path = subdir.parent / f"{subdir.name}.tar"
            if tar_path.exists():
                print(f"  ⚠️  [{i}/{len(subdirs)}] {tar_path.name} already exists, skipping tar creation")
                tar_files.append(tar_path)
                continue
            print(f"  📦 [{i}/{len(subdirs)}] Tarring {subdir.name}...")
            dir_size: int = directory_size(subdir)
            tar_size, success = tar_directory(subdir, tar_path, delete_original=not keep_original)
            if success:
                tar_files.append(tar_path)
                state: str = "directory kept" if keep_original else "directory deleted"
                print(f"    ✅ Tarred {format_size(dir_size)} → {format_size(tar_size)} ({state})")
            else:
                tar_errors.append((subdir, "Failed to create tar"))
        except Exception as e:
            print(f"  ❌ [{i}/{len(subdirs)}] Error tarring {subdir.name}: {e}")
            tar_errors.append((subdir, str(e)))
    jobs: list[tuple[Path, Path]] = []
    for tar_path in tar_files:
        output_path: Path = compressed_output_path(tar_path)
        if output_path.exists():
            print(f"  ⚠️  {output_path.name} already exists, skipping")
            continue
        jobs.append((tar_path, output_path))
    if jobs:
        print(f"\n🗜️  Step 2: Compressing {len(jobs)} tar files with Brotli (quality: {quality})...")
        for i, (tar_path, output_path) in enumerate(jobs, 1):
            result = compress_file_streaming(
                tar_path,
                output_path,
                quality=quality,
                chunk_size=chunk_size,
                keep_original=False,
                was_tarred=True,
                verify=verify,
                workers=workers,
            )
            results.append(result)
            report_tar_result(result, i, len(jobs))
    if tar_errors:
        print(f"\n❌ Failed to tar {len(tar_errors)} directories:")
        for subdir, error in tar_errors:
            print(f"  • {subdir.name}: {error}")
    return results


def should_compress_file(file_path: Path, exclude_extensions: set[str], exclude_patterns: list[str]) -> bool:
    if file_path.is_symlink():
        return False
    if not file_path.is_file():
        return False
    if file_path.suffix.lower() in exclude_extensions:
        return False
    if file_path.suffix == ".br":
        return False
    if file_path.suffix == ".tar":
        return False
    if exclude_patterns:
        path_str: str = str(file_path)
        if any(pattern in path_str for pattern in exclude_patterns):
            return False
    return True


def find_files_to_compress(
    directory: Path,
    exclude_extensions: Optional[set[str]] = None,
    exclude_patterns: Optional[list[str]] = None,
    extensions_filter: Optional[Sequence[str]] = None,
    skip_subdirs: bool = False,
) -> list[Path]:
    extensions: set[str] = EXCLUDED_EXTENSIONS if exclude_extensions is None else exclude_extensions
    patterns: list[str] = exclude_patterns or []
    files: list[Path] = []
    if extensions_filter:
        for raw_ext in extensions_filter:
            ext: str = raw_ext if raw_ext.startswith(".") else f".{raw_ext}"
            for file_path in directory.rglob(f"*{ext}"):
                if should_compress_file(file_path, extensions, patterns):
                    if skip_subdirs and file_path.parent != directory:
                        continue
                    files.append(file_path)
    else:
        for file_path in directory.rglob("*"):
            if should_compress_file(file_path, extensions, patterns):
                if skip_subdirs and file_path.parent != directory:
                    continue
                files.append(file_path)
    return sorted(set(files))


def find_files_to_decompress(directory: Path, exclude_patterns: Optional[list[str]] = None) -> list[Path]:
    patterns: list[str] = exclude_patterns or []
    files: list[Path] = []
    for file_path in directory.rglob("*.br"):
        if file_path.is_symlink():
            continue
        if not file_path.is_file():
            continue
        if patterns and any(pattern in str(file_path) for pattern in patterns):
            continue
        files.append(file_path)
    return sorted(set(files))


def collect_compress_targets(
    input_paths: Sequence[Path],
    exclude_extensions: set[str],
    exclude_patterns: list[str],
    extensions_filter: Optional[Sequence[str]],
    tar_subdirs_first: bool,
) -> tuple[list[Path], list[Path]]:
    files: list[Path] = []
    subdirs: list[Path] = []
    for p in input_paths:
        if p.is_file():
            if should_compress_file(p, exclude_extensions, exclude_patterns):
                files.append(p)
        elif p.is_dir():
            if tar_subdirs_first:
                subdirs.extend(find_subdirs_to_tar([p], exclude_patterns))
                files.extend(
                    find_files_to_compress(
                        p,
                        exclude_extensions,
                        exclude_patterns,
                        extensions_filter,
                        skip_subdirs=True,
                    )
                )
            else:
                files.extend(
                    find_files_to_compress(
                        p,
                        exclude_extensions,
                        exclude_patterns,
                        extensions_filter,
                        skip_subdirs=False,
                    )
                )
    return sorted(set(files)), sorted(set(subdirs))


def collect_decompress_targets(input_paths: Sequence[Path], exclude_patterns: list[str]) -> list[Path]:
    files: list[Path] = []
    for p in input_paths:
        if p.is_file():
            if p.name.endswith(".br") and p.is_file() and not p.is_symlink():
                if not exclude_patterns or not any(pattern in str(p) for pattern in exclude_patterns):
                    files.append(p)
        elif p.is_dir():
            files.extend(find_files_to_decompress(p, exclude_patterns))
    return sorted(set(files))


def get_file_type_stats(files: list[Path]) -> dict[str, int]:
    type_stats: dict[str, int] = {}
    for file_path in files:
        ext: str = file_path.suffix.lower() or "[no extension]"
        type_stats[ext] = type_stats.get(ext, 0) + 1
    return dict(sorted(type_stats.items(), key=lambda x: x[1], reverse=True))


def run_jobs(
    jobs: list[tuple[Path, Path]],
    operation: str,
    quality: int,
    chunk_size: int,
    keep_original: bool,
    workers: int,
    verify: bool,
    allow_truncated: bool,
) -> Iterator[CompressionResult]:
    for input_path, output_path in jobs:
        if operation == "compress":
            yield compress_file_streaming(
                input_path,
                output_path,
                quality=quality,
                chunk_size=chunk_size,
                keep_original=keep_original,
                was_tarred=False,
                verify=verify,
                workers=workers,
            )
        else:
            yield decompress_file_streaming(
                input_path,
                output_path,
                chunk_size=chunk_size,
                keep_original=keep_original,
                allow_truncated=allow_truncated,
                workers=workers,
            )


def process_files(
    files: list[Path],
    base_dir: Path,
    operation: str,
    quality: int = 11,
    workers: int = 4,
    keep_original: bool = False,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    verify: bool = False,
    allow_truncated: bool = False,
) -> list[CompressionResult]:
    results: list[CompressionResult] = []
    jobs: list[tuple[Path, Path]] = []
    for file_path in files:
        output_path: Path = (
            compressed_output_path(file_path) if operation == "compress" else decompressed_output_path(file_path)
        )
        if output_path.exists():
            results.append(
                CompressionResult(
                    file_path=file_path,
                    original_size=file_path.stat().st_size if file_path.exists() else 0,
                    processed_size=0,
                    success=False,
                    error=f"Output already exists: {output_path.name}",
                    operation=operation,
                )
            )
            continue
        jobs.append((file_path, output_path))
    if not jobs:
        return results
    label: str = "Compressing" if operation == "compress" else "Decompressing"
    stream: Iterator[CompressionResult] = run_jobs(
        jobs,
        operation,
        quality,
        chunk_size,
        keep_original,
        workers,
        verify,
        allow_truncated,
    )
    if RICH_AVAILABLE:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
        ) as progress:
            task = progress.add_task(label, total=len(jobs))
            for result in stream:
                results.append(result)
                progress.advance(task)
    else:
        for i, result in enumerate(stream, 1):
            results.append(result)
            status: str = "✅" if result.success else "❌"
            detail: str = (
                f"{format_size(result.original_size)} → {format_size(result.processed_size)}"
                if result.success
                else str(result.error)
            )
            print(f"  {status} [{i}/{len(jobs)}] {relative_display(result.file_path, base_dir)}: {detail}")
    return results


def extract_decompressed_tars(results: list[CompressionResult], keep_tar: bool) -> tuple[int, int, int]:
    extracted: int = 0
    failed: int = 0
    processed: int = 0
    for result in results:
        if not result.success:
            continue
        if not result.file_path.name.endswith(".tar.br"):
            continue
        tar_path: Path = decompressed_output_path(result.file_path)
        if tar_path.suffix != ".tar" or not tar_path.exists():
            continue
        processed += 1
        if untar_file(tar_path, tar_path.parent, delete_tar=not keep_tar):
            extracted += 1
            print(f"  📂 Extracted {tar_path.name}")
        else:
            failed += 1
    return extracted, failed, processed


def print_results_rich(results: list[CompressionResult], base_dir: Path, operation: str) -> None:
    console = Console()
    successful: list[CompressionResult] = [r for r in results if r.success]
    failed: list[CompressionResult] = [r for r in results if not r.success]
    total_original: int = sum(r.original_size for r in successful)
    total_processed: int = sum(r.processed_size for r in successful)
    total_duration: float = sum(r.duration for r in results)
    deleted_count: int = sum(1 for r in successful if r.original_deleted)
    tarred_count: int = sum(1 for r in successful if r.was_tarred)
    verified_count: int = sum(1 for r in successful if r.verified)
    if operation == "compress":
        space_saved: int = total_original - total_processed
        avg_ratio: float = (
            sum((1 - r.processed_size / r.original_size) * 100 for r in successful) / len(successful)
            if successful
            else 0.0
        )
        operation_emoji: str = "📦"
        operation_name: str = "Compression"
        size_label: str = "Compressed"
    else:
        space_saved = total_processed - total_original
        avg_ratio = (
            sum((r.processed_size / r.original_size - 1) * 100 for r in successful) / len(successful)
            if successful
            else 0.0
        )
        operation_emoji = "📂"
        operation_name = "Decompression"
        size_label = "Decompressed"
    table = Table(
        title=f"{operation_emoji} Brotli {operation_name} Results",
        box=box.ROUNDED,
        title_style="bold cyan",
        header_style="bold white",
    )
    table.add_column("File", style="cyan", no_wrap=False)
    table.add_column("Original", justify="right", style="yellow")
    table.add_column(size_label, justify="right", style="green")
    table.add_column("Ratio", justify="right", style="magenta")
    table.add_column("Time", justify="right", style="dim")
    table.add_column("Type", justify="center")
    table.add_column("Status", justify="center")
    for result in sorted(successful, key=lambda x: x.original_size, reverse=True)[:20]:
        if operation == "compress":
            ratio: float = (1 - result.processed_size / result.original_size) * 100 if result.original_size > 0 else 0.0
        else:
            ratio = (result.processed_size / result.original_size - 1) * 100 if result.original_size > 0 else 0.0
        status: str = "🗑️ ✅" if result.original_deleted else "✅"
        if result.verified:
            status += "🔐"
        file_type: str = "📦 tar" if result.was_tarred else "📄 file"
        table.add_row(
            relative_display(result.file_path, base_dir),
            format_size(result.original_size),
            format_size(result.processed_size),
            f"{ratio:.1f}%",
            f"{result.duration:.2f}s",
            file_type,
            status,
        )
    if len(successful) > 20:
        table.add_row(f"... and {len(successful) - 20} more files", "", "", "", "", "", "")
    console.print(table)
    if failed:
        fail_table = Table(title="❌ Failed Files", box=box.ROUNDED, title_style="bold red")
        fail_table.add_column("File", style="red")
        fail_table.add_column("Error", style="dim")
        for result in failed[:10]:
            fail_table.add_row(
                relative_display(result.file_path, base_dir),
                result.error or "Unknown error",
            )
        if len(failed) > 10:
            fail_table.add_row(f"... and {len(failed) - 10} more failures", "")
        console.print(fail_table)
    summary_text = Text()
    summary_text.append(f"📊 {operation_name} Summary\n\n", style="bold cyan")
    summary_text.append("📁 Base: ", style="dim")
    summary_text.append(f"{base_dir}\n", style="bold white")
    summary_text.append("Total files processed: ", style="dim")
    summary_text.append(f"{len(results)}\n", style="bold white")
    summary_text.append("✅ Successful: ", style="dim")
    summary_text.append(f"{len(successful)}\n", style="bold green")
    summary_text.append("❌ Failed: ", style="dim")
    summary_text.append(f"{len(failed)}\n", style="bold red")
    if tarred_count > 0:
        summary_text.append("📦 From tarred directories: ", style="dim")
        summary_text.append(f"{tarred_count}\n", style="bold yellow")
    if verified_count > 0:
        summary_text.append("🔐 Round-trip verified: ", style="dim")
        summary_text.append(f"{verified_count}\n", style="bold green")
    summary_text.append("🗑️  Originals deleted: ", style="dim")
    summary_text.append(f"{deleted_count}\n", style="bold yellow")
    summary_text.append("\n💾 Total original size: ", style="dim")
    summary_text.append(f"{format_size(total_original)}\n", style="bold yellow")
    summary_text.append(
        f"{'📦' if operation == 'compress' else '📂'} Total {size_label.lower()} size: ",
        style="dim",
    )
    summary_text.append(f"{format_size(total_processed)}\n", style="bold green")
    if operation == "compress":
        summary_text.append("📈 Average compression: ", style="dim")
        summary_text.append(f"{avg_ratio:.1f}%\n", style="bold magenta")
        summary_text.append("🎉 Disk space freed: ", style="dim")
        summary_text.append(f"{format_size(space_saved)} ", style="bold cyan")
        if total_original > 0:
            summary_text.append(f"({space_saved / total_original * 100:.1f}%)\n", style="bold cyan")
        else:
            summary_text.append("\n", style="bold cyan")
    else:
        summary_text.append("📈 Average expansion: ", style="dim")
        summary_text.append(f"{avg_ratio:.1f}%\n", style="bold magenta")
        summary_text.append("💾 Disk space used: ", style="dim")
        summary_text.append(f"{format_size(space_saved)}\n", style="bold cyan")
    summary_text.append("⏱️  Total time: ", style="dim")
    summary_text.append(f"{total_duration:.2f}s ", style="bold white")
    if results:
        summary_text.append(f"(avg {total_duration / len(results):.2f}s per file)", style="dim")
    console.print(Panel(summary_text, border_style="cyan"))


def print_results_basic(results: list[CompressionResult], base_dir: Path, operation: str) -> None:
    successful: list[CompressionResult] = [r for r in results if r.success]
    failed: list[CompressionResult] = [r for r in results if not r.success]
    total_original: int = sum(r.original_size for r in successful)
    total_processed: int = sum(r.processed_size for r in successful)
    total_duration: float = sum(r.duration for r in results)
    deleted_count: int = sum(1 for r in successful if r.original_deleted)
    tarred_count: int = sum(1 for r in successful if r.was_tarred)
    verified_count: int = sum(1 for r in successful if r.verified)
    if operation == "compress":
        space_saved: int = total_original - total_processed
        avg_ratio: float = (
            sum((1 - r.processed_size / r.original_size) * 100 for r in successful) / len(successful)
            if successful
            else 0.0
        )
        operation_name: str = "Compression"
        size_label: str = "Compressed"
    else:
        space_saved = total_processed - total_original
        avg_ratio = (
            sum((r.processed_size / r.original_size - 1) * 100 for r in successful) / len(successful)
            if successful
            else 0.0
        )
        operation_name = "Decompression"
        size_label = "Decompressed"
    print("\n" + "=" * 80)
    print(f"📦 Brotli {operation_name} Results")
    print(f"📁 Base: {base_dir}")
    print("=" * 80)
    print(f"\n{'File':<40} {'Original':>12} {size_label:>12} {'Ratio':>8} {'Time':>8}")
    print("-" * 80)
    for result in sorted(successful, key=lambda x: x.original_size, reverse=True)[:20]:
        if operation == "compress":
            ratio: float = (1 - result.processed_size / result.original_size) * 100 if result.original_size > 0 else 0.0
        else:
            ratio = (result.processed_size / result.original_size - 1) * 100 if result.original_size > 0 else 0.0
        name: str = result.file_path.name
        file_name: str = name[:37] + "..." if len(name) > 40 else name
        type_indicator: str = "[tar]" if result.was_tarred else ""
        print(
            f"{file_name:<40} {format_size(result.original_size):>12} "
            f"{format_size(result.processed_size):>12} {ratio:>7.1f}% {result.duration:>7.2f}s {type_indicator}"
        )
    if len(successful) > 20:
        print(f"... and {len(successful) - 20} more files")
    if failed:
        print(f"\n❌ Failed files ({len(failed)}):")
        for result in failed[:10]:
            print(f"  • {relative_display(result.file_path, base_dir)}: {result.error}")
        if len(failed) > 10:
            print(f"  ... and {len(failed) - 10} more failures")
    print("\n" + "=" * 80)
    print(f"📊 {operation_name} Summary")
    print("=" * 80)
    print(f"Total files processed: {len(results)}")
    print(f"✅ Successful: {len(successful)}")
    print(f"❌ Failed: {len(failed)}")
    if tarred_count > 0:
        print(f"📦 From tarred directories: {tarred_count}")
    if verified_count > 0:
        print(f"🔐 Round-trip verified: {verified_count}")
    print(f"🗑️  Originals deleted: {deleted_count}")
    print(f"\n💾 Total original size: {format_size(total_original)}")
    print(
        f"{'📦' if operation == 'compress' else '📂'} Total {size_label.lower()} size: {format_size(total_processed)}"
    )
    if operation == "compress":
        pct: float = (space_saved / total_original * 100) if total_original > 0 else 0.0
        print(f"📈 Average compression: {avg_ratio:.1f}%")
        print(f"🎉 Disk space freed: {format_size(space_saved)} ({pct:.1f}%)")
    else:
        print(f"📈 Average expansion: {avg_ratio:.1f}%")
        print(f"💾 Disk space used: {format_size(space_saved)}")
    if results:
        print(f"⏱️  Total time: {total_duration:.2f}s (avg {total_duration / len(results):.2f}s per file)")
    print("=" * 80 + "\n")


def print_dry_run(
    files: list[Path],
    base_dir: Path,
    operation: str,
    subdirs: Optional[list[Path]] = None,
) -> None:
    total_size: int = 0
    for file_path in files:
        try:
            total_size += file_path.stat().st_size
        except OSError:
            continue
    label: str = "compressed" if operation == "compress" else "decompressed"
    print("\n" + "=" * 80)
    print(f"🔍 DRY RUN — nothing will be modified ({operation})")
    print(f"📁 Base: {base_dir}")
    print("=" * 80)
    if subdirs:
        print(
            f"\n📦 Subdirectories to tar first: {len(subdirs)} ({format_size(sum(directory_size(d) for d in subdirs))})"
        )
        for subdir in subdirs[:20]:
            print(f"  • {subdir.name}/  {format_size(directory_size(subdir))}")
        if len(subdirs) > 20:
            print(f"  ... and {len(subdirs) - 20} more directories")
    print(f"\n📄 Files to be {label}: {len(files)} ({format_size(total_size)})")
    if files:
        print("\nBy type:")
        for ext, count in list(get_file_type_stats(files).items())[:15]:
            print(f"  {ext:<20} {count}")
        print("\nLargest files:")
        for file_path in sorted(files, key=lambda p: p.stat().st_size if p.exists() else 0, reverse=True)[:20]:
            size: int = file_path.stat().st_size if file_path.exists() else 0
            print(f"  {relative_display(file_path, base_dir):<60} {format_size(size):>12}")
        if len(files) > 20:
            print(f"  ... and {len(files) - 20} more files")
    print("=" * 80 + "\n")


def print_header(
    operation: str,
    input_paths: Sequence[Path],
    quality: int,
    workers: int,
    keep_original: bool,
    tar_mode: bool,
    verify: bool,
) -> None:
    emoji: str = "📦" if operation == "compress" else "📂"
    action: str = "Compressing" if operation == "compress" else "Decompressing"
    details: str = f"workers: {workers} | {'keep originals' if keep_original else 'delete originals'}"
    if operation == "compress":
        details = f"quality: {quality} | " + details
        if tar_mode:
            details += " | tar subdirs first"
        if verify:
            details += " | verify round-trip"
    paths_str: str = ", ".join(str(p) for p in input_paths)
    header: str = f"{emoji} Brotli {action} — {paths_str}"
    if RICH_AVAILABLE:
        Console().print(
            Panel(
                f"[bold cyan]{header}[/bold cyan]\n[dim]{details}[/dim]",
                border_style="cyan",
            )
        )
    else:
        print("\n" + "=" * 80)
        print(header)
        print(details)
        print("=" * 80)


def swallowed_directory(args: argparse.Namespace) -> Optional[str]:
    if args.paths:
        return None
    for values in (args.exclude, args.extensions):
        if not values:
            continue
        candidate: str = values[-1]
        if ("/" in candidate or candidate in (".", "..")) and Path(candidate).expanduser().is_dir():
            return candidate
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "📦 Recursively compress/decompress files using Brotli with parallel processing "
            "(deletes originals by default)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                          # Compress all files in current dir (tars subdirs first)
  %(prog)s -c                       # Compress all files (explicit)
  %(prog)s -c file1 file2 dir/      # Compress multiple inputs
  %(prog)s -d                       # Decompress all .br files (auto-untar .tar.br)
  %(prog)s -d archive.tar.br        # Decompress and untar archive.tar.br
  %(prog)s -c -t /path/to/dir       # Tar subdirs first, then compress
  %(prog)s -c -e txt log csv        # Compress only specific extensions
  %(prog)s -c -q 11 -w 8            # Custom quality and workers
  %(prog)s -c --verify              # Verify round-trip before deleting originals
  %(prog)s -c --keep-originals      # Keep original files when compressing
  %(prog)s -d --keep-originals      # Keep compressed files when decompressing
  %(prog)s -c --dry-run             # Preview compression without modifying
  %(prog)s --exclude .git -- .      # Exclude patterns, use '.' as target
        """,
    )
    operation_group = parser.add_mutually_exclusive_group()
    operation_group.add_argument(
        "-c",
        "--compress",
        action="store_true",
        default=True,
        help="Compress files (default)",
    )
    operation_group.add_argument("-d", "--decompress", action="store_true", help="Decompress .br files")
    parser.add_argument(
        "-t",
        "--tar-subdirs-first",
        action="store_true",
        help="Tar subdirectories first, then apply Brotli compression on the .tar files (only valid with -c/--compress)",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=[],
        type=str,
        help="Files and/or directories to process. If omitted, uses current directory and tars subdirs first.",
    )
    parser.add_argument(
        "-e",
        "--extensions",
        nargs="+",
        help="Compress only specific file extensions (e.g., txt log csv). Only valid with -c/--compress.",
    )
    parser.add_argument(
        "-q",
        "--quality",
        type=int,
        default=11,
        choices=range(12),
        help="Brotli compression quality (0-11, default: 11). Only valid with -c/--compress.",
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=mp.cpu_count(),
        help=f"Number of parallel workers (default: {mp.cpu_count()})",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"Streaming chunk size in bytes (default: {DEFAULT_CHUNK_SIZE}, minimum: {MIN_CHUNK_SIZE})",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Decompress each result in memory and compare SHA-256 with the source before deleting originals",
    )
    parser.add_argument(
        "--keep-originals",
        action="store_true",
        help="Keep original files after processing (default: delete originals)",
    )
    parser.add_argument(
        "--allow-truncated",
        action="store_true",
        help="Accept Brotli streams without an end-of-stream marker (only valid with -d/--decompress)",
    )
    parser.add_argument(
        "--exclude",
        nargs="+",
        default=[],
        help="Directory/file patterns to exclude from processing (e.g., node_modules .git)",
    )
    parser.add_argument("--no-parallel", action="store_true", help="Disable parallel processing")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be processed without actually modifying files",
    )
    parser.add_argument(
        "--no-skip-compressed",
        action="store_true",
        help="Do not skip already compressed files (dangerous, may double-compress). Only valid with -c/--compress.",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt shown before deleting originals",
    )
    args = parser.parse_args()
    stolen: Optional[str] = swallowed_directory(args)
    if stolen is not None:
        print(f"❌ Error: '{stolen}' was consumed by -e/--extensions or --exclude, so no target path was provided.")
        print(f"   Separate the target with --:  {parser.prog} -c --exclude ... -- {stolen}")
        return 2
    operation: str = "decompress" if args.decompress else "compress"
    if operation == "decompress":
        if args.tar_subdirs_first:
            print("❌ Error: -t/--tar-subdirs-first is only valid with -c/--compress")
            return 1
        if args.extensions:
            print("❌ Error: -e/--extensions is only valid with -c/--compress")
            return 1
        if args.no_skip_compressed:
            print("❌ Error: --no-skip-compressed is only valid with -c/--compress")
            return 1
        if args.verify:
            print("❌ Error: --verify is only valid with -c/--compress")
            return 1
    if args.chunk_size < MIN_CHUNK_SIZE:
        print(f"❌ Error: --chunk-size must be at least {MIN_CHUNK_SIZE} bytes")
        return 1
    auto_tar: bool = False
    if not args.paths:
        args.paths = ["."]
        if operation == "compress":
            args.tar_subdirs_first = True
            auto_tar = True
    input_paths: list[Path] = []
    for raw in args.paths:
        p: Path = Path(raw).expanduser().resolve()
        if not p.exists():
            print(f"❌ Error: path does not exist: {p}")
            return 1
        input_paths.append(p)
    if not input_paths:
        input_paths = [Path.cwd().resolve()]
    base_dir: Path = Path.cwd().resolve()
    for p in input_paths:
        if p.is_dir() and p == Path(p.anchor) and not args.keep_originals and not args.dry_run:
            print(f"❌ Error: refusing to run destructively on the filesystem root: {p}")
            return 1
    workers: int = 1 if args.no_parallel else max(1, args.workers)
    keep_original: bool = args.keep_originals
    exclude_patterns: list[str] = list(args.exclude)
    exclude_extensions: set[str] = set() if args.no_skip_compressed else set(EXCLUDED_EXTENSIONS)
    chunk_size: int = args.chunk_size
    results: list[CompressionResult] = []
    wall_start: float = time.time()
    try:
        if operation == "compress":
            files, subdirs = collect_compress_targets(
                input_paths,
                exclude_extensions,
                exclude_patterns,
                args.extensions,
                args.tar_subdirs_first,
            )
            if args.dry_run:
                print_dry_run(files, base_dir, operation, subdirs)
                return 0
            if not files and not subdirs:
                print("✨ Nothing to compress")
                return 0
            print_header(
                operation,
                input_paths,
                args.quality,
                workers,
                keep_original,
                args.tar_subdirs_first,
                args.verify,
            )
            if args.tar_subdirs_first and subdirs:
                results.extend(
                    process_subdirs_with_tar(
                        subdirs,
                        args.quality,
                        workers,
                        keep_original,
                        chunk_size,
                        args.verify,
                    )
                )
            if files:
                print(f"\n🗜️  Compressing {len(files)} files with Brotli (quality: {args.quality})...")
                results.extend(
                    process_files(
                        files,
                        base_dir,
                        operation,
                        args.quality,
                        workers,
                        keep_original,
                        chunk_size,
                        args.verify,
                    )
                )
        else:
            files = collect_decompress_targets(input_paths, exclude_patterns)
            if args.dry_run:
                print_dry_run(files, base_dir, operation)
                return 0
            if not files:
                print("✨ No .br files found to decompress")
                return 0
            print_header(
                operation,
                input_paths,
                args.quality,
                workers,
                keep_original,
                False,
                False,
            )
            print(f"\n📂 Decompressing {len(files)} files...")
            results = process_files(
                files,
                base_dir,
                operation,
                args.quality,
                workers,
                keep_original,
                chunk_size,
                False,
                args.allow_truncated,
            )
            extracted, extract_failed, tar_processed = extract_decompressed_tars(results, keep_original)
            if tar_processed > 0:
                print(
                    f"\n📦 Extracted {extracted} tar archive(s)"
                    + (f", {extract_failed} failed" if extract_failed else "")
                )
    except KeyboardInterrupt:
        print("\n⚠️  Interrupted by user")
        return 130
    except Exception as e:
        print(f"\n❌ Fatal error: {type(e).__name__}: {e}")
        return 1
    if not results:
        print("✨ Nothing processed")
        return 0
    if RICH_AVAILABLE:
        print_results_rich(results, base_dir, operation)
    else:
        print_results_basic(results, base_dir, operation)
    print(f"⏲️  Wall clock: {time.time() - wall_start:.2f}s")
    successful: int = sum(1 for r in results if r.success)
    return 0 if successful else 1


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
'''

_SOURCES['zstd'] = r"""#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import argparse
import io
import os
import shutil
import sys
import tarfile
import threading
import time
from dataclasses import dataclass
from functools import partial
from multiprocessing import Pool
from pathlib import Path
from typing import Iterable, Optional, Sequence

import zstandard as zstd

try:
    from loguru import logger
except ImportError:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger = logging.getLogger("zstd_toolkit")
DEFAULT_LEVEL: int = 19
DEFAULT_THREADS: int = 4
DEFAULT_WORKERS: int = min(os.cpu_count() or 4, 8)
DEFAULT_ARCHIVE_LEVEL: int = 3
ZST_EXT: str = ".zst"
TAR_ZST_EXT: str = ".tar.zst"
SKIP_EXTENSIONS: frozenset[str] = frozenset({
    ".zst",
    ".zstd",
    ".gz",
    ".bz2",
    ".xz",
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".tgz",
    ".tbz2",
    ".txz",
    ".lz",
    ".lz4",
    ".lzma",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".avif",
    ".heic",
    ".mp4",
    ".avi",
    ".mkv",
    ".mov",
    ".webm",
    ".wmv",
    ".flv",
    ".mp3",
    ".flac",
    ".aac",
    ".ogg",
    ".opus",
    ".wma",
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".whl",
    ".egg",
    ".pyc",
    ".pyo",
    ".class",
    ".o",
    ".obj",
    ".iso",
    ".img",
    ".dmg",
    ".exe",
    ".dll",
    ".so",
})
SKIP_DIRS: frozenset[str] = frozenset({
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
})


def parse_size(value: str) -> int:
    s = value.strip().upper().replace(" ", "")
    units = {
        "B": 1,
        "K": 1024,
        "KB": 1024,
        "M": 1024**2,
        "MB": 1024**2,
        "G": 1024**3,
        "GB": 1024**3,
        "T": 1024**4,
        "TB": 1024**4,
    }
    for suffix, factor in sorted(units.items(), key=lambda kv: -len(kv[0])):
        if s.endswith(suffix):
            num = s[: -len(suffix)] or "0"
            return int(float(num) * factor)
    return int(float(s))


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _file_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def dir_size(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                total += _file_size(p)
    except OSError:
        pass
    return total


def _safe_extractall(tar: tarfile.TarFile, path: Path) -> None:
    try:
        tar.extractall(path, filter="data")
    except TypeError:
        tar.extractall(path)


@dataclass(slots=True)
class TaskResult:
    path: Path
    original_size: int = 0
    processed_size: int = 0
    success: bool = False
    operation: str = "compress"
    duration: float = 0.0
    error: Optional[str] = None
    output: Optional[Path] = None
    original_deleted: bool = False
    tar_extracted: bool = False

    @property
    def savings_pct(self) -> float:
        if self.original_size <= 0:
            return 0.0
        if self.operation == "compress":
            return (1 - self.processed_size / self.original_size) * 100
        return (self.processed_size / self.original_size - 1) * 100


def run_parallel(
    tasks: Sequence[Path],
    worker,
    workers: int,
    operation: str,
    dry_run: bool = False,
) -> list[TaskResult]:
    if not tasks:
        return []
    if dry_run:
        for t in tasks:
            print(f"[dry-run] would {operation} {t}")
        return []
    total = len(tasks)
    lock = threading.Lock()
    state = {"done": 0, "orig": 0, "proc": 0, "start": time.time()}

    def render(last: Optional[TaskResult], final: bool = False) -> None:
        done = state["done"]
        pct = done / total * 100 if total else 0.0
        saved = (1 - state["proc"] / state["orig"]) * 100 if state["orig"] else 0.0
        elapsed = time.time() - state["start"]
        speed = state["orig"] / (1024 * 1024) / elapsed if elapsed else 0.0
        bar_len = 30
        filled = max(0, min(int(bar_len * pct / 100), bar_len - 1))
        bar = "=" * filled + ">" + "." * (bar_len - filled - 1)
        name = last.path.name if last else ""
        if len(name) > 30:
            name = name[:27] + "..."
        ending = "\n" if final else ""
        sys.stdout.write(
            f"\r{operation.upper():10} [{bar}] {pct:5.1f}% {done}/{total} files "
            f"({saved:5.1f}% saved, {speed:5.1f} MB/s) - {name:<30}{ending}"
        )
        sys.stdout.flush()

    results: list[TaskResult] = []
    try:
        with Pool(processes=workers) as pool:
            for res in pool.imap_unordered(worker, tasks):
                results.append(res)
                with lock:
                    state["done"] += 1
                    state["orig"] += res.original_size
                    state["proc"] += res.processed_size
                    render(res)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return results
    render(results[-1] if results else None, final=True)
    return results


def compress_file(
    src: Path,
    level: int,
    threads: int,
    keep: bool,
    only_if_smaller: bool = False,
) -> TaskResult:
    t0 = time.perf_counter()
    dst = src.with_suffix(src.suffix + ZST_EXT)
    tmp = src.with_suffix(src.suffix + ZST_EXT + ".tmp")
    if dst.exists():
        return TaskResult(
            path=src,
            success=False,
            error="output exists",
            operation="compress",
            duration=time.perf_counter() - t0,
        )
    try:
        original = _file_size(src)
        if original == 0:
            return TaskResult(
                path=src,
                success=False,
                error="empty file",
                operation="compress",
                duration=time.perf_counter() - t0,
            )
        cctx = zstd.ZstdCompressor(level=level, threads=threads)
        with (
            src.open("rb") as fin,
            tmp.open("wb") as fout,
            cctx.stream_writer(fout) as writer,
        ):
            shutil.copyfileobj(fin, writer, length=1024 * 1024)
        compressed = tmp.stat().st_size
        if only_if_smaller and compressed >= original:
            tmp.unlink(missing_ok=True)
            return TaskResult(
                path=src,
                original_size=original,
                processed_size=original,
                success=False,
                error="compressed not smaller",
                operation="compress",
                duration=time.perf_counter() - t0,
            )
        tmp.rename(dst)
        deleted = False
        if not keep:
            src.unlink()
            deleted = True
        return TaskResult(
            path=src,
            original_size=original,
            processed_size=compressed,
            success=True,
            operation="compress",
            output=dst,
            original_deleted=deleted,
            duration=time.perf_counter() - t0,
        )
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        return TaskResult(
            path=src,
            success=False,
            error=str(exc),
            operation="compress",
            duration=time.perf_counter() - t0,
        )


def tar_compress_dir(
    src_dir: Path,
    level: int,
    threads: int,
    keep: bool,
) -> TaskResult:
    t0 = time.perf_counter()
    dst = src_dir.with_name(src_dir.name + TAR_ZST_EXT)
    tmp_tar = src_dir.parent / f".tmp_{src_dir.name}.tar"
    if dst.exists():
        return TaskResult(
            path=src_dir,
            success=False,
            error="output exists",
            operation="compress",
            duration=time.perf_counter() - t0,
        )
    try:
        original = dir_size(src_dir)
        with tarfile.open(tmp_tar, "w") as tar:
            tar.add(src_dir, arcname=src_dir.name)
        cctx = zstd.ZstdCompressor(level=level, threads=threads)
        with (
            tmp_tar.open("rb") as fin,
            dst.open("wb") as fout,
            cctx.stream_writer(fout) as writer,
        ):
            shutil.copyfileobj(fin, writer, length=1024 * 1024)
        tmp_tar.unlink(missing_ok=True)
        compressed = dst.stat().st_size
        deleted = False
        if not keep:
            shutil.rmtree(src_dir)
            deleted = True
        return TaskResult(
            path=src_dir,
            original_size=original,
            processed_size=compressed,
            success=True,
            operation="compress",
            output=dst,
            original_deleted=deleted,
            duration=time.perf_counter() - t0,
        )
    except Exception as exc:
        tmp_tar.unlink(missing_ok=True)
        dst.unlink(missing_ok=True)
        return TaskResult(
            path=src_dir,
            success=False,
            error=str(exc),
            operation="compress",
            duration=time.perf_counter() - t0,
        )


def decompress_file(
    src: Path,
    keep: bool = True,
    untar: bool = True,
) -> TaskResult:
    t0 = time.perf_counter()
    suffixes = src.suffixes
    if len(suffixes) >= 2 and suffixes[-2:] == [".tar", ".zst"]:
        dst = src.with_suffix("").with_suffix("")
        is_tar = True
    elif src.suffix == ZST_EXT:
        dst = src.with_suffix("")
        is_tar = False
    else:
        return TaskResult(
            path=src,
            success=False,
            error="not a .zst file",
            operation="decompress",
            duration=time.perf_counter() - t0,
        )
    tmp = dst.with_name(dst.name + ".tmp")
    try:
        original = _file_size(src)
        dctx = zstd.ZstdDecompressor()
        with (
            src.open("rb") as fin,
            tmp.open("wb") as fout,
            dctx.stream_reader(fin) as reader,
        ):
            shutil.copyfileobj(reader, fout, length=1024 * 1024)
        tmp.rename(dst)
        tar_extracted = False
        if untar and is_tar:
            with tarfile.open(dst, "r") as tar:
                _safe_extractall(tar, dst.parent)
            dst.unlink()
            tar_extracted = True
        processed = _file_size(dst) if dst.exists() else original
        deleted = False
        if not keep:
            src.unlink()
            deleted = True
        return TaskResult(
            path=src,
            original_size=original,
            processed_size=processed,
            success=True,
            operation="decompress",
            output=dst,
            original_deleted=deleted,
            tar_extracted=tar_extracted,
            duration=time.perf_counter() - t0,
        )
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        return TaskResult(
            path=src,
            success=False,
            error=str(exc),
            operation="decompress",
            duration=time.perf_counter() - t0,
        )


def _compress_file_task(path: Path, *, level: int, threads: int, keep: bool, only_if_smaller: bool) -> TaskResult:
    return compress_file(path, level, threads, keep, only_if_smaller)


def _tar_compress_dir_task(path: Path, *, level: int, threads: int, keep: bool) -> TaskResult:
    return tar_compress_dir(path, level, threads, keep)


def _decompress_task(path: Path, *, keep: bool, untar: bool) -> TaskResult:
    return decompress_file(path, keep=keep, untar=untar)


def _should_skip_path(p: Path) -> bool:
    return any(part in SKIP_DIRS for part in p.parts)


def discover_targets(root: Path, args: argparse.Namespace) -> tuple[list[Path], list[Path]]:
    files: list[Path] = []
    dirs: list[Path] = []
    if args.targets in ("files", "both"):
        it: Iterable[Path] = root.rglob("*") if args.recursive else root.iterdir()
        whitelist = None
        if args.ext:
            whitelist = {e if e.startswith(".") else f".{e}" for e in args.ext}
        for p in it:
            if not p.is_file():
                continue
            if _should_skip_path(p):
                continue
            if args.exclude and any(pat in str(p) for pat in args.exclude):
                continue
            if whitelist is not None:
                if p.suffix.lower() not in whitelist:
                    continue
            elif p.suffix.lower() in SKIP_EXTENSIONS:
                continue
            if args.min_size and _file_size(p) < args.min_size:
                continue
            files.append(p)
    if args.targets in ("dirs", "both"):
        for d in root.iterdir():
            if not d.is_dir():
                continue
            if d.name in SKIP_DIRS:
                continue
            if args.exclude and any(pat in str(d) for pat in args.exclude):
                continue
            if args.min_size and dir_size(d) < args.min_size:
                continue
            dirs.append(d)
    return files, dirs


def print_summary(results: Sequence[TaskResult], operation: str) -> None:
    total = len(results)
    ok = [r for r in results if r.success]
    fail = [r for r in results if not r.success]
    orig = sum(r.original_size for r in ok)
    proc = sum(r.processed_size for r in ok)
    duration = sum(r.duration for r in results)
    print()
    print("=" * 60)
    print(f"{operation.capitalize()} summary")
    print("=" * 60)
    print(f"Total: {total}   Success: {len(ok)}   Failed: {len(fail)}")
    print(f"Original size : {human_size(orig)}")
    print(f"Processed size: {human_size(proc)}")
    if operation == "compress" and orig:
        print(f"Space saved   : {(1 - proc / orig) * 100:.1f}%  ({human_size(orig - proc)})")
    if duration:
        print(f"CPU time      : {duration:.2f}s")
    for r in fail:
        logger.warning(f"FAIL {r.path}: {r.error}")


def cmd_compress(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.is_dir():
        logger.error(f"{root} is not a directory")
        return 1
    files, dirs = discover_targets(root, args)
    if not files and not dirs:
        print("Nothing to compress.")
        return 0
    print(
        f"Targets: {len(files)} file(s), {len(dirs)} dir(s) "
        f"(level {args.level}, threads {args.threads}, workers {args.workers})"
    )
    results: list[TaskResult] = []
    if dirs:
        worker = partial(
            _tar_compress_dir_task,
            level=args.level,
            threads=args.threads,
            keep=args.keep,
        )
        results += run_parallel(dirs, worker, args.workers, "compress", dry_run=args.dry_run)
    if files:
        worker = partial(
            _compress_file_task,
            level=args.level,
            threads=args.threads,
            keep=args.keep,
            only_if_smaller=args.only_if_smaller,
        )
        results += run_parallel(files, worker, args.workers, "compress", dry_run=args.dry_run)
    print_summary(results, "compress")
    return 0 if all(r.success or not r.original_size for r in results) else 1


def cmd_decompress(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.is_dir():
        logger.error(f"{root} is not a directory")
        return 1
    it = root.rglob("*") if args.recursive else root.iterdir()
    targets = [p for p in it if p.is_file() and p.suffix == ZST_EXT]
    if not targets:
        print("No .zst files found.")
        return 0
    print(f"Decompressing {len(targets)} file(s) (workers {args.workers}, keep={args.keep})")
    worker = partial(_decompress_task, keep=args.keep, untar=not args.no_untar)
    results = run_parallel(targets, worker, args.workers, "decompress", dry_run=args.dry_run)
    print_summary(results, "decompress")
    return 0 if all(r.success for r in results) else 1


def _verify_archive(archive: Path) -> None:
    print(f"\nVerifying {archive.name} …")
    try:
        dctx = zstd.ZstdDecompressor()
        with (
            archive.open("rb") as f,
            dctx.stream_reader(f) as reader,
            tarfile.open(fileobj=reader, mode="r|") as tar,
        ):
            count = 0
            for member in tar:
                count += 1
                if count <= 5:
                    kind = "dir" if member.isdir() else "file"
                    print(f"  {member.name} ({kind}, {member.size} B)")
            if count > 5:
                print(f"  … (+{count - 5} more)")
        print(f"OK — {count} entries")
    except Exception as exc:
        logger.error(f"Verification failed: {exc}")


def cmd_archive_cwd(args: argparse.Namespace) -> int:
    cwd = Path.cwd().resolve()
    parent = cwd.parent
    if str(cwd) == "/" or cwd == Path.home():
        logger.error("Refusing to archive root or home directory")
        return 1
    archive = parent / f"{cwd.name}{TAR_ZST_EXT}"
    if archive.exists() and not args.force:
        ans = input(f"Archive '{archive}' exists. Overwrite? (y/n): ").strip().lower()
        if ans not in ("y", "yes"):
            print("Cancelled.")
            return 1
    try:
        cctx = zstd.ZstdCompressor(level=args.level, threads=args.threads or 0)
        print(f"Creating archive: {archive}")
        with (
            archive.open("wb") as f,
            cctx.stream_writer(f) as sw,
            tarfile.open(fileobj=sw, mode="w|") as tar,
        ):
            for p in cwd.rglob("*"):
                if ".git" in p.parts:
                    continue
                if p.name.endswith(TAR_ZST_EXT):
                    continue
                try:
                    arc = p.relative_to(parent)
                    tar.add(p, arcname=arc, recursive=False)
                except (OSError, ValueError) as exc:
                    logger.warning(f"Skip {p}: {exc}")
    except KeyboardInterrupt:
        archive.unlink(missing_ok=True)
        print("\nInterrupted.")
        return 130
    except Exception as exc:
        archive.unlink(missing_ok=True)
        logger.error(f"Archive failed: {exc}")
        return 1
    if not archive.exists() or archive.stat().st_size == 0:
        logger.error("Archive creation produced an empty file")
        archive.unlink(missing_ok=True)
        return 1
    print(f"Archive created: {archive} ({human_size(archive.stat().st_size)})")
    if args.verify:
        _verify_archive(archive)
    if not args.no_remove:
        ans = input(f"Remove original directory '{cwd}'? (y/n): ").strip().lower()
        if ans in ("y", "yes"):
            shutil.rmtree(cwd)
            print("Original directory removed.")
        else:
            print("Original directory preserved.")
    return 0


def cmd_split(args: argparse.Namespace) -> int:
    src = Path(args.input).resolve()
    if not src.is_file():
        logger.error(f"Input file not found: {src}")
        return 1
    if args.parts < 1:
        logger.error("N must be >= 1")
        return 1
    out_dir = Path(args.output_dir).resolve() if args.output_dir else src.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    base = src.stem
    base = base.removesuffix(".tar")
    print(f"Reading {src} …")
    dctx = zstd.ZstdDecompressor()
    with src.open("rb") as f:
        tar_bytes = dctx.stream_reader(f).read()
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r|") as tar:
        total_members = sum(1 for _ in tar)
    print(f"Total members: {total_members}")
    n = min(args.parts, total_members) or 1
    if n != args.parts:
        print(f"Warning: only {n} part(s) produced (fewer members than requested).")
    base_count, extra = divmod(total_members, n)
    part_num = 1
    current = 0
    target = base_count + (1 if part_num <= extra else 0)
    buf = io.BytesIO()
    writer = tarfile.open(fileobj=buf, mode="w|")
    cctx = zstd.ZstdCompressor(level=args.level)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r|") as tar:
        for member in tar:
            if member.isfile():
                writer.addfile(member, tar.extractfile(member))
            else:
                writer.addfile(member)
            current += 1
            if current >= target and part_num < n:
                writer.close()
                buf.seek(0)
                out_path = out_dir / f"{base}.part{part_num:02d}{TAR_ZST_EXT}"
                with out_path.open("wb") as f:
                    f.write(cctx.compress(buf.read()))
                print(f"  wrote {out_path.name}")
                part_num += 1
                current = 0
                target = base_count + (1 if part_num <= extra else 0)
                buf = io.BytesIO()
                writer = tarfile.open(fileobj=buf, mode="w|")
    writer.close()
    buf.seek(0)
    out_path = out_dir / f"{base}.part{part_num:02d}{TAR_ZST_EXT}"
    with out_path.open("wb") as f:
        f.write(cctx.compress(buf.read()))
    print(f"  wrote {out_path.name}")
    print(f"\nSplit into {part_num} parts.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zstd_toolkit.py",
        description="Unified zstandard compression / decompression toolkit.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_c = sub.add_parser("compress", help="Compress files and/or directories")
    p_c.add_argument("directory", nargs="?", default=".", help="Root directory (default: current)")
    p_c.add_argument(
        "-t",
        "--targets",
        choices=("files", "dirs", "both"),
        default="files",
        help="What to compress (default: files)",
    )
    p_c.add_argument(
        "-l",
        "--level",
        type=int,
        default=DEFAULT_LEVEL,
        help=f"zstd level 1-22 (default: {DEFAULT_LEVEL})",
    )
    p_c.add_argument(
        "-T",
        "--threads",
        type=int,
        default=DEFAULT_THREADS,
        help=f"zstd intra-file threads (default: {DEFAULT_THREADS})",
    )
    p_c.add_argument(
        "-w",
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Parallel worker processes (default: {DEFAULT_WORKERS})",
    )
    p_c.add_argument(
        "-k",
        "--keep",
        action="store_true",
        help="Keep original files/dirs after compression",
    )
    p_c.add_argument(
        "-m",
        "--min-size",
        type=parse_size,
        default=0,
        help="Minimum size (bytes or '10MB'-style) to consider",
    )
    p_c.add_argument(
        "-e",
        "--ext",
        nargs="+",
        default=None,
        help="Whitelist of file extensions (e.g. .txt .log)",
    )
    p_c.add_argument("--exclude", nargs="+", default=[], help="Substrings of paths to exclude")
    p_c.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="Descend into subdirectories when targeting files",
    )
    p_c.add_argument(
        "--only-if-smaller",
        action="store_true",
        help="Only keep the .zst if it is smaller than the original",
    )
    p_c.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Show what would be done, change nothing",
    )
    p_c.set_defaults(func=cmd_compress)
    p_d = sub.add_parser("decompress", help="Decompress .zst / .tar.zst files")
    p_d.add_argument("directory", nargs="?", default=".", help="Root directory (default: current)")
    p_d.add_argument(
        "-w",
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Parallel worker processes (default: {DEFAULT_WORKERS})",
    )
    p_d.add_argument("-r", "--recursive", action="store_true", help="Recurse into subdirectories")
    p_d.add_argument(
        "-k",
        "--keep",
        action="store_true",
        help="Keep the .zst archive after decompression",
    )
    p_d.add_argument(
        "--no-untar",
        action="store_true",
        help="Do not extract .tar.zst archives, just leave the .tar",
    )
    p_d.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Show what would be done, change nothing",
    )
    p_d.set_defaults(func=cmd_decompress)
    p_a = sub.add_parser("archive-cwd", help="Archive the current directory into its parent")
    p_a.add_argument(
        "-l",
        "--level",
        type=int,
        default=DEFAULT_ARCHIVE_LEVEL,
        help=f"zstd level (default: {DEFAULT_ARCHIVE_LEVEL})",
    )
    p_a.add_argument(
        "-T",
        "--threads",
        type=int,
        default=0,
        help="zstd threads (0 = single-threaded, default)",
    )
    p_a.add_argument("--verify", action="store_true", help="List the archive after creation")
    p_a.add_argument(
        "--no-remove",
        action="store_true",
        help="Do not prompt to remove the original directory",
    )
    p_a.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite an existing archive without prompting",
    )
    p_a.set_defaults(func=cmd_archive_cwd)
    p_s = sub.add_parser("split", help="Split a .tar.zst archive into N parts")
    p_s.add_argument("input", help="Path to a .tar.zst file")
    p_s.add_argument("parts", type=int, help="Number of parts to create")
    p_s.add_argument(
        "-o",
        "--output-dir",
        default=None,
        help="Output directory (default: same as input)",
    )
    p_s.add_argument(
        "-l",
        "--level",
        type=int,
        default=DEFAULT_LEVEL,
        help=f"zstd level (default: {DEFAULT_LEVEL})",
    )
    p_s.set_defaults(func=cmd_split)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
"""

_SOURCES['xz'] = r"""#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import argparse
import contextlib
import io
import lzma
import os
import shutil
import sys
import tarfile
import time
from dataclasses import dataclass
from datetime import datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Any, Optional, Sequence

try:
    import lzma_mt

    _HAS_LZMA_MT: bool = True
except ImportError:
    _HAS_LZMA_MT = False
try:
    import pylzma

    _HAS_PYLZMA: bool = True
except ImportError:
    _HAS_PYLZMA = False
try:
    from loguru import logger

    _HAS_LOGURU = True
except ImportError:
    _HAS_LOGURU = False

    class _FallbackLogger:
        def info(self, m: str) -> None:
            print(m)

        def warning(self, m: str) -> None:
            print(f"WARN: {m}", file=sys.stderr)

        def error(self, m: str) -> None:
            print(f"ERROR: {m}", file=sys.stderr)

        def success(self, m: str) -> None:
            print(m)

        def debug(self, m: str) -> None:
            pass

        def remove(self) -> None:
            pass

        def add(self, *a: Any, **k: Any) -> None:
            pass

    logger = _FallbackLogger()
DEFAULT_SKIP_DIRS: set[str] = {
    ".git",
    ".svn",
    ".hg",
    "__pycache__",
    ".venv",
    "venv",
    ".env",
    "node_modules",
}
DEFAULT_SKIP_EXTS: set[str] = {
    ".zip",
    ".br",
    ".xz",
    ".gz",
    ".bz2",
    ".bz3",
    ".zst",
    ".7z",
    ".lz4",
    ".rar",
    ".tar",
    ".tgz",
    ".tbz",
    ".tbz2",
    ".z",
    ".lz",
    ".lzma",
    ".xza",
    ".mkv",
    ".mp4",
    ".webm",
    ".avi",
    ".mov",
    ".flv",
    ".wmv",
    ".m4v",
    ".mpg",
    ".mpeg",
    ".mp3",
    ".aac",
    ".flac",
    ".wav",
    ".m4a",
    ".opus",
    ".ogg",
    ".wma",
    ".alac",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".bmp",
    ".webp",
    ".svg",
    ".tiff",
    ".ico",
    ".heic",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
    ".iso",
    ".img",
    ".pyc",
    ".pyo",
    ".class",
    ".o",
    ".obj",
}
DEFAULT_WORKERS: int = 8
DEFAULT_THREADS: int = 4
DEFAULT_PRESET: int = 9
DEFAULT_CHUNK_SIZE: int = 1024 * 1024
DEFAULT_PYLZMA_CHUNK: int = 512 * 1024
DEFAULT_PYLZMA_DICT: int = 256 * 1024 * 1024
PYLZMA_MEM_THRESHOLD: int = 512 * 1024
XZ_SUFFIX: str = ".xz"
SEVENZ_SUFFIX: str = ".7z"
TAR_SUFFIX: str = ".tar"
TAR_XZ_SUFFIX: str = ".tar.xz"
LZMA_SUFFIX: str = ".lzma"
if _HAS_PYLZMA:
    _PYLZMA_FILTERS = [
        {
            "id": pylzma.FILTER_LZMA1,
            "preset": 9 | pylzma.PRESET_EXTREME,
            "dict_size": DEFAULT_PYLZMA_DICT,
        },
    ]
else:
    _PYLZMA_FILTERS = []


def format_size(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} PB"


def dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _is_skipped_dir(path: Path, skip_dirs: set[str]) -> bool:
    return any(part in skip_dirs for part in path.parts)


def _matches_exclude(path: Path, patterns: Sequence[str]) -> bool:
    if not patterns:
        return False
    s = str(path)
    return any(p in s for p in patterns)


def discover_files(
    root: Path,
    *,
    mode: str = "compress",
    extensions: Optional[Sequence[str]] = None,
    skip_extensions: Optional[set[str]] = None,
    exclude_patterns: Optional[Sequence[str]] = None,
    skip_dirs: Optional[set[str]] = None,
    recursive: bool = True,
    skip_symlinks: bool = True,
) -> list[Path]:
    skip_dirs = skip_dirs or DEFAULT_SKIP_DIRS
    exclude_patterns = list(exclude_patterns or [])
    ext_filter: set[str] = set()
    if extensions:
        for e in extensions:
            ext_filter.add((e if e.startswith(".") else "." + e).lower())
    iterator = root.rglob("*") if recursive else root.glob("*")
    out: list[Path] = []
    for p in iterator:
        if skip_symlinks and p.is_symlink():
            continue
        if not p.is_file():
            continue
        if _is_skipped_dir(p, skip_dirs):
            continue
        if _matches_exclude(p, exclude_patterns):
            continue
        suffix = p.suffix.lower()
        if mode == "decompress":
            if suffix != XZ_SUFFIX:
                continue
        else:
            if ext_filter and suffix not in ext_filter:
                continue
            if skip_extensions and suffix in skip_extensions:
                continue
        out.append(p)
    return sorted(out)


def compress_bytes(
    data: bytes,
    *,
    preset: int = DEFAULT_PRESET,
    threads: int = DEFAULT_THREADS,
    backend: str = "auto",
) -> bytes:
    if backend in ("auto", "lzma_mt", "lzmamt") and _HAS_LZMA_MT:
        return lzma_mt.compress(data, preset=preset, threads=threads)
    if backend in ("lzma_mt", "lzmamt"):
        msg = "lzma_mt requested but not installed. `pip install lzma_mt` or pass --backend lzma"
        raise RuntimeError(msg)
    lvl = preset | lzma.PRESET_EXTREME if preset == 9 else preset
    return lzma.compress(data, format=lzma.FORMAT_XZ, preset=lvl)


def decompress_bytes(data: bytes, *, backend: str = "auto") -> bytes:
    if backend in ("auto", "lzma_mt", "lzmamt") and _HAS_LZMA_MT:
        return lzma_mt.decompress(data)
    if backend in ("lzma_mt", "lzmamt"):
        msg = "lzma_mt requested but not installed."
        raise RuntimeError(msg)
    return lzma.decompress(data, format=lzma.FORMAT_XZ)


@dataclass
class FileResult:
    path: Path
    success: bool
    message: str = ""
    original_size: int = 0
    processed_size: int = 0
    duration: float = 0.0
    was_tarred: bool = False


def _worker_compress_xz(args: tuple[str, int, int, str, bool, bool]) -> FileResult:
    path_str, preset, threads, backend, remove_orig, dry_run = args
    src = Path(path_str)
    dst = src.with_name(src.name + XZ_SUFFIX)
    t0 = time.monotonic()
    if dry_run:
        return FileResult(src, True, f"[dry-run] would compress → {dst.name}")
    if dst.exists():
        return FileResult(src, False, f"{dst.name} already exists — skipped")
    try:
        data = src.read_bytes()
        cdata = compress_bytes(data, preset=preset, threads=threads, backend=backend)
        dst.write_bytes(cdata)
        if remove_orig:
            src.unlink()
        return FileResult(
            path=src,
            success=True,
            message=f"→ {dst.name}",
            original_size=len(data),
            processed_size=len(cdata),
            duration=time.monotonic() - t0,
        )
    except Exception as exc:
        with contextlib.suppress(OSError):
            if dst.exists():
                dst.unlink()
        return FileResult(src, False, f"Error: {exc}", duration=time.monotonic() - t0)


def _worker_decompress_xz(args: tuple[str, bool, bool]) -> FileResult:
    path_str, remove_orig, handle_tar_xz = args
    src = Path(path_str)
    t0 = time.monotonic()
    if src.suffix.lower() != XZ_SUFFIX:
        return FileResult(src, False, "not a .xz file")
    try:
        if handle_tar_xz and src.name.endswith(TAR_XZ_SUFFIX):
            out_dir = src.with_name(src.name[: -len(TAR_XZ_SUFFIX)])
            out_dir.mkdir(exist_ok=True)
            with tarfile.open(src, "r:xz") as tar:
                tar.extractall(path=out_dir, filter="data")
            if remove_orig:
                src.unlink()
            size = dir_size(out_dir)
            return FileResult(
                src,
                True,
                f"extracted → {out_dir.name}/",
                original_size=src.stat().st_size if src.exists() else 0,
                processed_size=size,
                duration=time.monotonic() - t0,
            )
        data = src.read_bytes()
        out = decompress_bytes(data)
        dst = src.with_suffix("")
        dst.write_bytes(out)
        if remove_orig:
            src.unlink()
        return FileResult(
            path=src,
            success=True,
            message=f"→ {dst.name}",
            original_size=len(data),
            processed_size=len(out),
            duration=time.monotonic() - t0,
        )
    except Exception as exc:
        return FileResult(src, False, f"Error: {exc}", duration=time.monotonic() - t0)


def _worker_tar_dir(args: tuple[str, int, bool, bool]) -> dict[str, Any]:
    dir_str, preset, verify, remove_orig = args
    src = Path(dir_str)
    dst = src.with_name(src.name + TAR_XZ_SUFFIX)
    t0 = time.monotonic()
    start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    orig_size = 0
    try:
        if not src.is_dir():
            msg = f"Not a directory: {src}"
            raise FileNotFoundError(msg)
        if dst.exists():
            msg = f"archive exists: {dst.name}"
            raise FileExistsError(msg)
        orig_size = dir_size(src)
        with (
            lzma.open(dst, "wb", preset=preset, check=lzma.CHECK_CRC64) as xz_f,
            tarfile.open(fileobj=xz_f, mode="w", dereference=False) as tar,
        ):
            tar.add(src, arcname=src.name, recursive=True)
        if verify:
            with tarfile.open(dst, "r:xz") as tar:
                for member in tar:
                    if member.isfile():
                        fh = tar.extractfile(member)
                        if fh is not None:
                            while fh.read(1024 * 1024):
                                pass
        comp_size = dst.stat().st_size
        if comp_size <= 0:
            msg = "Created archive is empty"
            raise OSError(msg)
        if remove_orig:
            shutil.rmtree(src)
        return {
            "subdir": src.name,
            "success": True,
            "message": f"OK   {src.name}",
            "start": start,
            "end": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed": time.monotonic() - t0,
            "original_size": orig_size,
            "compressed_size": comp_size,
        }
    except Exception as exc:
        with contextlib.suppress(OSError):
            if dst.exists():
                dst.unlink()
        return {
            "subdir": src.name,
            "success": False,
            "message": f"FAIL {src.name}: {exc}",
            "start": start,
            "end": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed": time.monotonic() - t0,
            "original_size": orig_size,
            "compressed_size": 0,
        }


def _read_file_bytes(args: tuple[str]) -> tuple[str, bytes]:
    (path_str,) = args
    return (path_str, Path(path_str).read_bytes())


def _pylzma_compress_blob(data: bytes) -> bytes:
    if not _HAS_PYLZMA:
        msg = "pylzma is not installed (`pip install pylzma`)."
        raise RuntimeError(msg)
    return pylzma.compress(data, filters=_PYLZMA_FILTERS)


def _pylzma_decompress_blob(data: bytes) -> bytes:
    if not _HAS_PYLZMA:
        msg = "pylzma is not installed."
        raise RuntimeError(msg)
    return pylzma.decompress(data)


def _worker_pylzma_7z(args: tuple[str, bool, Optional[str], bool]) -> FileResult:
    path_str, keep, out_dir_str, _tar_subdirs = args
    src = Path(path_str)
    out_dir = Path(out_dir_str) if out_dir_str else None
    t0 = time.monotonic()
    try:
        if src.is_dir():
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                tar.add(src, arcname=src.name)
            cdata = _pylzma_compress_blob(buf.getvalue())
            dst = (
                (out_dir / f"{src.name}{TAR_SUFFIX}{SEVENZ_SUFFIX}")
                if out_dir
                else src.with_name(f"{src.name}{TAR_SUFFIX}{SEVENZ_SUFFIX}")
            )
            dst.write_bytes(cdata)
            if not keep:
                shutil.rmtree(src)
            return FileResult(
                src,
                True,
                f"→ {dst}",
                original_size=dir_size(src) if src.exists() else 0,
                processed_size=len(cdata),
                duration=time.monotonic() - t0,
                was_tarred=True,
            )
        else:
            data = src.read_bytes()
            cdata = _pylzma_compress_blob(data)
            dst = (out_dir / f"{src.name}{SEVENZ_SUFFIX}") if out_dir else src.with_name(f"{src.name}{SEVENZ_SUFFIX}")
            dst.write_bytes(cdata)
            if not keep:
                src.unlink()
            return FileResult(
                src,
                True,
                f"→ {dst}",
                original_size=len(data),
                processed_size=len(cdata),
                duration=time.monotonic() - t0,
            )
    except Exception as exc:
        return FileResult(src, False, f"Error: {exc}", duration=time.monotonic() - t0)


def _worker_pylzma_un7z(args: tuple[str, bool, Optional[str], bool]) -> FileResult:
    path_str, keep, out_dir_str, _ = args
    src = Path(path_str)
    out_dir = Path(out_dir_str) if out_dir_str else None
    t0 = time.monotonic()
    try:
        if not src.is_file():
            return FileResult(src, False, "not a file")
        data = src.read_bytes()
        raw = _pylzma_decompress_blob(data)
        if src.name.endswith(f"{TAR_SUFFIX}{SEVENZ_SUFFIX}"):
            stem = src.name[: -len(f"{TAR_SUFFIX}{SEVENZ_SUFFIX}")]
            target_dir = (out_dir / stem) if out_dir else src.with_name(stem)
            target_dir.mkdir(parents=True, exist_ok=True)
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r") as tar:
                tar.extractall(path=target_dir, filter="data")
            if not keep:
                src.unlink()
            return FileResult(
                src,
                True,
                f"→ {target_dir}/",
                original_size=len(data),
                processed_size=dir_size(target_dir),
                duration=time.monotonic() - t0,
                was_tarred=True,
            )
        if src.name.endswith(SEVENZ_SUFFIX):
            stem = src.name[: -len(SEVENZ_SUFFIX)]
            dst = (out_dir / stem) if out_dir else src.with_name(stem)
            dst.write_bytes(raw)
            if not keep:
                src.unlink()
            return FileResult(
                src,
                True,
                f"→ {dst}",
                original_size=len(data),
                processed_size=len(raw),
                duration=time.monotonic() - t0,
            )
        return FileResult(src, False, "not a .7z / .tar.7z file")
    except Exception as exc:
        return FileResult(src, False, f"Error: {exc}", duration=time.monotonic() - t0)


def _pylzma_compress_chunk(chunk: bytes) -> bytes:
    return _pylzma_compress_blob(chunk)


def _chunked_compress_file(src: Path, dst: Path, chunk_size: int, workers: int) -> bool:
    data = src.read_bytes()
    total = len(data)
    n_chunks = (total + chunk_size - 1) // chunk_size
    chunks = [data[i * chunk_size : (i + 1) * chunk_size] for i in range(n_chunks)]
    with Pool(processes=workers) as pool:
        compressed = pool.map(_pylzma_compress_chunk, chunks)
    with dst.open("wb") as fh:
        fh.write(n_chunks.to_bytes(4, "big"))
        for blob in compressed:
            fh.write(len(blob).to_bytes(8, "big"))
            fh.write(blob)
    return True


def _chunked_decompress_file(src: Path, dst: Path) -> bool:
    with src.open("rb") as fin, dst.open("wb") as fout:
        header = fin.read(4)
        if len(header) != 4:
            msg = "invalid chunked header"
            raise ValueError(msg)
        n_chunks = int.from_bytes(header, "big")
        for _ in range(n_chunks):
            size_raw = fin.read(8)
            if len(size_raw) != 8:
                msg = "truncated chunk size"
                raise ValueError(msg)
            size = int.from_bytes(size_raw, "big")
            blob = fin.read(size)
            if len(blob) != size:
                msg = "truncated chunk"
                raise ValueError(msg)
            fout.write(_pylzma_decompress_blob(blob))
    return True


def _worker_lzma_chunk_compress(args: tuple[str, int, int, bool]) -> FileResult:
    path_str, chunk_size, workers, remove_orig = args
    src = Path(path_str)
    dst = src.with_suffix(src.suffix + LZMA_SUFFIX)
    t0 = time.monotonic()
    if dst.exists():
        return FileResult(src, False, f"{dst.name} exists — skipped")
    try:
        size = src.stat().st_size
        if size < PYLZMA_MEM_THRESHOLD:
            cdata = _pylzma_compress_blob(src.read_bytes())
            dst.write_bytes(cdata)
        else:
            _chunked_compress_file(src, dst, chunk_size, workers)
        csize = dst.stat().st_size
        if csize == 0:
            dst.unlink()
            return FileResult(src, False, "empty output")
        if csize >= size:
            dst.unlink()
            return FileResult(src, False, "no space saved — discarded")
        if remove_orig:
            src.unlink()
        return FileResult(
            src,
            True,
            f"→ {dst.name}",
            original_size=size,
            processed_size=csize,
            duration=time.monotonic() - t0,
        )
    except Exception as exc:
        with contextlib.suppress(OSError):
            if dst.exists():
                dst.unlink()
        return FileResult(src, False, f"Error: {exc}", duration=time.monotonic() - t0)


def _worker_lzma_chunk_decompress(args: tuple[str, bool]) -> FileResult:
    path_str, remove_orig = args
    src = Path(path_str)
    if not src.name.endswith(LZMA_SUFFIX):
        return FileResult(src, False, "not a .lzma file")
    dst = src.with_suffix("")
    t0 = time.monotonic()
    try:
        data_head = src.read_bytes()[:12]
        is_chunked = False
        if len(data_head) == 12:
            n = int.from_bytes(data_head[:4], "big")
            first = int.from_bytes(data_head[4:12], "big")
            if 0 < n < 10_000_000 and 0 < first <= src.stat().st_size:
                is_chunked = True
        if is_chunked:
            _chunked_decompress_file(src, dst)
        else:
            dst.write_bytes(_pylzma_decompress_blob(src.read_bytes()))
        if remove_orig:
            src.unlink()
        return FileResult(
            src,
            True,
            f"→ {dst.name}",
            original_size=src.stat().st_size if src.exists() else 0,
            processed_size=dst.stat().st_size,
            duration=time.monotonic() - t0,
        )
    except Exception as exc:
        with contextlib.suppress(OSError):
            if dst.exists():
                dst.unlink()
        return FileResult(src, False, f"Error: {exc}", duration=time.monotonic() - t0)


def _print_file_result(root: Path, r: FileResult, index: int = 0, total: int = 0) -> None:
    prefix = ""
    if total:
        pct = index / total * 100
        prefix = f"[{pct:5.1f}%] {index}/{total} "
    mark = "✓" if r.success else "✗"
    try:
        rel = r.path.relative_to(root)
    except ValueError:
        rel = r.path
    extra = ""
    if r.success and r.original_size and r.processed_size:
        ratio = r.processed_size / r.original_size * 100
        extra = f"  ({format_size(r.original_size)} → {format_size(r.processed_size)}, {ratio:.1f}%)"
    print(f"{prefix}{mark} {rel}: {r.message}{extra}")


def _print_summary(results: Sequence[FileResult]) -> None:
    ok = [r for r in results if r.success]
    fail = [r for r in results if not r.success]
    print("─" * 60)
    print(f"Total successful: {len(ok)}")
    print(f"Total failed:     {len(fail)}")
    if ok:
        orig = sum(r.original_size for r in ok)
        proc = sum(r.processed_size for r in ok)
        if orig:
            print(f"Total original size:   {format_size(orig)}")
            print(f"Total processed size:  {format_size(proc)}")
            if proc <= orig:
                print(f"Space saved:           {format_size(orig - proc)} ({(orig - proc) / orig * 100:.1f}%)")
            else:
                print(f"Size increase:         {format_size(proc - orig)} ({(proc - orig) / orig * 100:.1f}%)")


def _tar_top_level_dirs(root: Path, preset: int, workers: int) -> list[dict[str, Any]]:
    subdirs = [
        p
        for p in root.iterdir()
        if p.is_dir() and not p.is_symlink() and not p.name.startswith(".") and p.name not in DEFAULT_SKIP_DIRS
    ]
    if not subdirs:
        return []
    print(f"Taring {len(subdirs)} top-level subdirectories...")
    args = [(str(d), preset, True, True) for d in subdirs]
    with Pool(processes=min(workers, len(subdirs))) as pool:
        results = pool.map(_worker_tar_dir, args)
    for r in results:
        mark = "✅" if r["success"] else "❌"
        print(f"  {mark} {r['message']} ({format_size(r['original_size'])} → {format_size(r['compressed_size'])})")
    return results


def cmd_compress(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.is_dir():
        logger.error(f"{root} is not a directory")
        return 1
    if args.dry_run:
        print("[dry-run] no files will be modified")
    if args.tar_subdirs or args.auto_tar_dirs:
        _tar_top_level_dirs(root, args.preset, args.workers)
    if args.no_skip_compressed:
        skip_exts: set[str] = {XZ_SUFFIX}
    else:
        skip_exts = DEFAULT_SKIP_EXTS
    files = discover_files(
        root,
        mode="compress",
        extensions=args.extensions,
        skip_extensions=skip_exts,
        exclude_patterns=args.exclude,
        recursive=not args.auto_tar_dirs,
    )
    if not files:
        print("No files found to compress")
        return 0
    print(
        f"Compressing {len(files)} file(s) with {'sequential mode' if args.sequential else f'{args.workers} workers'}"
    )
    print(f"Preset: {args.preset}, threads/job: {args.threads}, backend: {args.backend}")
    if args.dry_run:
        for f in files:
            print(f"  [dry-run] would compress {f.relative_to(root)}")
        return 0
    worker_args = [(str(f), args.preset, args.threads, args.backend, not args.keep_orig, False) for f in files]
    results: list[FileResult] = []
    if args.sequential or len(files) == 1:
        for i, wa in enumerate(worker_args, 1):
            r = _worker_compress_xz(wa)
            results.append(r)
            if args.verbose:
                _print_file_result(root, r, i, len(files))
    else:
        with Pool(processes=args.workers) as pool:
            futures = [pool.apply_async(_worker_compress_xz, (wa,)) for wa in worker_args]
            for i, fut in enumerate(futures, 1):
                r = fut.get()
                results.append(r)
                if args.verbose:
                    _print_file_result(root, r, i, len(files))
    if not args.no_size_stats:
        _print_summary(results)
    return 0


def cmd_decompress(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    if not root.is_dir():
        logger.error(f"{root} is not a directory")
        return 1
    files = discover_files(
        root,
        mode="decompress",
        exclude_patterns=args.exclude,
    )
    if not files:
        print("No .xz files found to decompress")
        return 0
    print(f"Decompressing {len(files)} file(s)...")
    worker_args = [(str(f), not args.keep_orig, args.handle_tar_xz) for f in files]
    results: list[FileResult] = []
    if args.sequential or len(files) == 1:
        for i, wa in enumerate(worker_args, 1):
            r = _worker_decompress_xz(wa)
            results.append(r)
            _print_file_result(root, r, i, len(files))
    else:
        with Pool(processes=args.workers) as pool:
            futures = [pool.apply_async(_worker_decompress_xz, (wa,)) for wa in worker_args]
            for i, fut in enumerate(futures, 1):
                r = fut.get()
                results.append(r)
                _print_file_result(root, r, i, len(files))
    _print_summary(results)
    return 0


def cmd_tar_dirs(args: argparse.Namespace) -> int:
    root = Path(args.directory).expanduser().resolve()
    if not root.is_dir():
        logger.error(f"Not a directory: {root}")
        return 1
    subdirs = sorted(
        (p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")),
        key=lambda p: p.name.lower(),
    )
    if not subdirs:
        print(f"No non-hidden top-level subdirectories found in: {root}")
        return 0
    workers = min(args.workers, len(subdirs))
    print(f"Directory:      {root}")
    print(f"Subdirectories: {len(subdirs)}")
    print(f"Workers:        {workers}")
    print(f"xz preset:      {args.preset}")
    print()
    worker_args = [(str(d), args.preset, not args.no_verify, not args.keep_orig) for d in subdirs]
    ok = fail = 0
    with Pool(processes=workers) as pool:
        for r in pool.imap_unordered(_worker_tar_dir, worker_args):
            mark = "✅" if r["success"] else "❌"
            if r["success"]:
                ratio = r["compressed_size"] / r["original_size"] * 100 if r["original_size"] else 0.0
                print(
                    f"{mark} {r['message']:<45} took={r['elapsed']:.1f}s "
                    f"{format_size(r['original_size'])} → "
                    f"{format_size(r['compressed_size'])} ({ratio:.1f}%)"
                )
                ok += 1
            else:
                print(f"{mark} {r['message']}  took={r['elapsed']:.1f}s")
                fail += 1
    print()
    print("Finished")
    print(f"Successful: {ok}")
    print(f"Failed:     {fail}")
    return 0 if fail == 0 else 1


def cmd_archive_cwd(args: argparse.Namespace) -> int:
    cwd = Path.cwd().resolve()
    parent = cwd.parent
    archive = parent / f"{cwd.name}{TAR_XZ_SUFFIX}"
    print(f"Current directory: {cwd}")
    print(f"Target archive:    {archive}")
    if archive.exists():
        logger.error(f"Archive already exists: {archive}")
        return 1
    files: list[Path] = []
    for p in cwd.rglob("*"):
        if p.is_file() and not p.is_symlink():
            files.append(p)
        elif p.is_symlink() and not p.exists():
            logger.warning(f"Skipping broken symlink: {p}")
    if not files:
        logger.error(f"No files found in {cwd}")
        return 1
    print(f"Reading {len(files)} file(s) with {args.workers} worker(s)...")
    with Pool(processes=args.workers) as pool:
        payload = pool.map(_read_file_bytes, [(str(f),) for f in files])
    print(f"Writing archive: {archive}")
    try:
        with tarfile.open(name=str(archive), mode="w:xz", preset=6, format=tarfile.PAX_FORMAT) as tar:
            for path_str, data in payload:
                src = Path(path_str)
                try:
                    rel = src.relative_to(cwd)
                except ValueError:
                    rel = Path(src.name)
                arcname = Path(cwd.name) / rel
                info = tarfile.TarInfo(name=str(arcname))
                info.size = len(data)
                info.mtime = src.stat().st_mtime
                tar.addfile(info, io.BytesIO(data))
    except Exception as exc:
        logger.exception(f"Writing archive failed: {exc}")
        with contextlib.suppress(OSError):
            archive.unlink()
        return 1
    try:
        with tarfile.open(archive, "r:xz") as tar:
            names = tar.getnames()
    except Exception as exc:
        logger.error(f"Verification failed: {exc}")
        with contextlib.suppress(OSError):
            archive.unlink()
        return 1
    prefix = f"{cwd.name}/"
    bad = [n for n in names if not n.startswith(prefix)]
    if bad:
        logger.error(f"Archive contains members outside '{prefix}': {bad[:5]}")
        archive.unlink()
        return 1
    if len(names) != len(files):
        logger.error(f"Member count mismatch: expected {len(files)}, got {len(names)}")
        archive.unlink()
        return 1
    os.chdir(parent)
    print(f"Removing original directory: {cwd}")
    shutil.rmtree(cwd)
    logger.success(f"Done. Archive: {archive}")
    return 0


def _collect_7z_targets(
    paths: Sequence[str],
    mode: str,
    cwd: Path,
    output_dir: Optional[Path],
) -> list[Path]:
    targets: list[Path] = []

    def add_compress_tree(base: Path) -> None:
        for p in base.rglob("*"):
            if p.is_file() and not p.name.endswith(SEVENZ_SUFFIX):
                targets.append(p.resolve())

    def add_decompress_tree(base: Path) -> None:
        for p in base.rglob(f"*{SEVENZ_SUFFIX}"):
            if p.is_file():
                targets.append(p.resolve())

    if not paths:
        if mode == "compress":
            add_compress_tree(cwd)
        else:
            add_decompress_tree(cwd)
    else:
        for raw in paths:
            p = Path(raw)
            if not p.exists():
                logger.warning(f"{p} does not exist, skipping")
                continue
            rp = p.resolve()
            if rp == cwd and p.is_dir():
                if mode == "compress":
                    add_compress_tree(cwd)
                else:
                    add_decompress_tree(cwd)
            elif mode == "decompress" and p.is_dir():
                add_decompress_tree(rp)
            else:
                targets.append(rp)
    if output_dir is not None:
        out_res = output_dir.resolve()
        targets = [t for t in targets if out_res not in t.parents and t != out_res]
    seen: set[Path] = set()
    ordered: list[Path] = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            ordered.append(t)
    return ordered


def cmd_7z(args: argparse.Namespace) -> int:
    if not _HAS_PYLZMA:
        logger.error("pylzma is required for the `7z` subcommand. Install it with `pip install pylzma`.")
        return 1
    mode = "decompress" if args.decompress else "compress"
    out_dir: Optional[Path] = None
    if args.output_dir:
        out_dir = Path(args.output_dir).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
    targets = _collect_7z_targets(args.paths, mode, Path.cwd(), out_dir)
    if not targets:
        print(f"No items found to {mode}")
        return 0
    print(f"{mode.capitalize()}ing {len(targets)} item(s) with {args.workers} workers...")
    if out_dir:
        print(f"Output directory: {out_dir}")
    worker = _worker_pylzma_un7z if mode == "decompress" else _worker_pylzma_7z
    worker_args = [(str(t), args.keep, str(out_dir) if out_dir else None, args.tar_subdirs) for t in targets]
    with Pool(processes=args.workers) as pool:
        for r in pool.imap_unordered(worker, worker_args):
            mark = "✓" if r.success else "✗"
            print(f"  {mark} {r.path}: {r.message}")
    return 0


def cmd_lzma_chunk(args: argparse.Namespace) -> int:
    if not _HAS_PYLZMA:
        logger.error("pylzma is required for the `lzma-chunk` subcommand.")
        return 1
    cwd = Path.cwd()
    if args.decompress:
        files = [p for p in cwd.glob(f"*{LZMA_SUFFIX}") if p.is_file() and not p.is_symlink()]
        if not files:
            print("No .lzma files to decompress")
            return 0
        print(f"Decompressing {len(files)} archive(s)...")
        worker_args = [(str(f), not args.keep_orig) for f in files]
        worker = _worker_lzma_chunk_decompress
    else:
        files = [
            p
            for p in cwd.glob("*")
            if p.is_file()
            and not p.is_symlink()
            and not any(p.name.endswith(s) for s in (LZMA_SUFFIX, XZ_SUFFIX, SEVENZ_SUFFIX, ".gz", ".bz2", ".zip"))
            and p.stat().st_size >= 1024
        ]
        if not files:
            print("No files to compress")
            return 0
        print(f"Compressing {len(files)} file(s) with chunk size {format_size(args.chunk_size)}...")
        worker_args = [(str(f), args.chunk_size, args.workers, not args.keep_orig) for f in files]
        worker = _worker_lzma_chunk_compress
    ok = 0
    total_orig = total_new = 0
    with Pool(processes=args.workers) as pool:
        for r in pool.imap_unordered(worker, worker_args):
            mark = "✓" if r.success else "✗"
            extra = ""
            if r.success and r.original_size and r.processed_size:
                pct = (1 - r.processed_size / r.original_size) * 100
                extra = f" ({format_size(r.original_size)} → {format_size(r.processed_size)}, {pct:.1f}% saved)"
            print(f"  {mark} {r.path.name}: {r.message}{extra}")
            if r.success:
                ok += 1
                total_orig += r.original_size
                total_new += r.processed_size
    print("─" * 60)
    print(f"Succeeded: {ok} / {len(files)}")
    if ok and total_orig:
        saved = total_orig - total_new
        pct = saved / total_orig * 100
        print(f"Total original:   {format_size(total_orig)}")
        print(f"Total processed:  {format_size(total_new)}")
        print(f"Space saved:      {format_size(abs(saved))} ({pct:.1f}%)")
    return 0


def _add_common_xz_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory to process (default: current directory)",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Parallel workers (default: {DEFAULT_WORKERS})",
    )
    p.add_argument(
        "--sequential",
        action="store_true",
        help="Disable multiprocessing (run in-process)",
    )
    p.add_argument("--keep-orig", action="store_true", help="Keep original files after processing")
    p.add_argument(
        "--exclude",
        nargs="+",
        default=[],
        metavar="PAT",
        help="Skip paths containing any of these substrings",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xz_tool.py",
        description="Unified compression / decompression CLI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  xz_tool.py compress                              "
            "# recursive .xz of cwd\n"
            "  xz_tool.py compress /data --preset 6 --threads 8\n"
            "  xz_tool.py compress --tar-subdirs --verbose\n"
            "  xz_tool.py compress --auto-tar-dirs --chunk-size 1048576\n"
            "  xz_tool.py decompress --handle-tar-xz /data/archives\n"
            "  xz_tool.py tar-dirs /data --workers 4\n"
            "  xz_tool.py archive-cwd\n"
            "  xz_tool.py 7z -k path1 path2 -k\n"
            "  xz_tool.py 7z --output-dir ./compressed\n"
            "  xz_tool.py lzma-chunk -c\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_c = sub.add_parser("compress", help="Recursively compress files to .xz")
    _add_common_xz_opts(p_c)
    p_c.add_argument(
        "--preset",
        type=int,
        default=DEFAULT_PRESET,
        choices=range(10),
        help=f"xz preset 0-9 (default: {DEFAULT_PRESET})",
    )
    p_c.add_argument(
        "--threads",
        type=int,
        default=DEFAULT_THREADS,
        help=f"Threads per compression job (default: {DEFAULT_THREADS})",
    )
    p_c.add_argument(
        "--backend",
        choices=["auto", "lzma_mt", "lzmamt", "lzma"],
        default="auto",
        help="Compression backend (default: auto)",
    )
    p_c.add_argument(
        "-e",
        "--extensions",
        nargs="+",
        help="Only compress files with these extensions",
    )
    p_c.add_argument(
        "--no-skip-compressed",
        action="store_true",
        help="Do not skip already-compressed extensions",
    )
    p_c.add_argument(
        "--tar-subdirs",
        action="store_true",
        help="Tar top-level subdirs first, then compress",
    )
    p_c.add_argument(
        "--auto-tar-dirs",
        action="store_true",
        help="(xzer.py) Same as --tar-subdirs but only top-level files are subsequently compressed",
    )
    p_c.add_argument("--dry-run", action="store_true", help="Show what would be done; modify nothing")
    p_c.add_argument("--verbose", action="store_true", help="Print per-file results")
    p_c.add_argument("--no-size-stats", action="store_true", help="Skip the size summary at the end")
    p_c.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"Chunk size for large files (default: {DEFAULT_CHUNK_SIZE})",
    )
    p_c.set_defaults(func=cmd_compress)
    p_d = sub.add_parser("decompress", help="Recursively decompress .xz files")
    _add_common_xz_opts(p_d)
    p_d.add_argument(
        "--handle-tar-xz",
        action="store_true",
        help="Extract `*.tar.xz` files into directories",
    )
    p_d.set_defaults(func=cmd_decompress)
    p_t = sub.add_parser("tar-dirs", help="Pack each top-level subdirectory into .tar.xz")
    p_t.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory containing the subdirectories (default: cwd)",
    )
    p_t.add_argument(
        "--preset",
        type=int,
        default=DEFAULT_PRESET,
        choices=range(10),
        help=f"xz preset 0-9 (default: {DEFAULT_PRESET})",
    )
    p_t.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Parallel workers (default: {DEFAULT_WORKERS})",
    )
    p_t.add_argument("--keep-orig", action="store_true", help="Keep the source subdirectory")
    p_t.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip archive verification (faster, riskier)",
    )
    p_t.set_defaults(func=cmd_tar_dirs)
    p_a = sub.add_parser("archive-cwd", help="Archive cwd to ../<cwd>.tar.xz and delete it")
    p_a.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Parallel readers (default: {DEFAULT_WORKERS})",
    )
    p_a.set_defaults(func=cmd_archive_cwd)
    p_7 = sub.add_parser("7z", help="pylzma-based .7z / .tar.7z codec")
    p_7.add_argument("paths", nargs="*", help="Files / directories (default: cwd recursively)")
    p_7.add_argument("-d", "--decompress", action="store_true", help="Decompress mode")
    p_7.add_argument("-k", "--keep", action="store_true", help="Keep originals after processing")
    p_7.add_argument(
        "--output-dir",
        default=None,
        help="Write outputs into this directory (compress_pylzma.py behaviour)",
    )
    p_7.add_argument(
        "--tar-subdirs",
        action="store_true",
        help="(Accepted for parity; directories are always tarred)",
    )
    p_7.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Parallel workers (default: {DEFAULT_WORKERS})",
    )
    p_7.set_defaults(func=cmd_7z)
    p_l = sub.add_parser("lzma-chunk", help="pylzma chunked .lzma codec (max compression)")
    p_l.add_argument("-c", "--compress", action="store_true", default=True, help="Compress (default)")
    p_l.add_argument("-d", "--decompress", action="store_true", help="Decompress .lzma files")
    p_l.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Parallel workers (default: {DEFAULT_WORKERS})",
    )
    p_l.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_PYLZMA_CHUNK,
        help=f"Chunk size in bytes (default: {DEFAULT_PYLZMA_CHUNK})",
    )
    p_l.add_argument("--keep-orig", action="store_true", help="Keep original files after processing")
    p_l.set_defaults(func=cmd_lzma_chunk)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        return 130
    except Exception as exc:
        logger.error(f"Unexpected error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
"""

_SOURCES['lz4'] = r"""#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import argparse
import contextlib
import io
import shutil
import sys
import tarfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import lz4.frame

DEFAULT_SKIP_EXT = {".gz", ".br", ".xz", ".zst", ".bz2", ".zip", ".whl", ".lz4", ".7z", ".rar"}
DEFAULT_MIN_SIZE = 1024
DEFAULT_CHUNK_SIZE = 32768
DEFAULT_CHUNK_THRESHOLD = 524288
DEFAULT_WORKERS = 8
DEFAULT_LEVEL = 3
BLOCK_SIZE_MAX = lz4.frame.BLOCKSIZE_MAX


def human_size(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    v = float(n)
    for u in units:
        if v < 1024 or u == units[-1]:
            return f"{v:.2f} {u}"
        v /= 1024
    return f"{v:.2f} {units[-1]}"


def _compress_bytes(data: bytes, level: int, block_size: int, checksum: bool) -> bytes:
    return lz4.frame.compress(
        data,
        compression_level=level,
        block_size=block_size,
        block_linked=True,
        content_checksum=checksum,
    )


def _chunk_worker(args):
    data, level, block_size, checksum = args
    return _compress_bytes(data, level, block_size, checksum)


def compress_file(
    src: Path,
    dst: Path,
    *,
    level: int,
    workers: int,
    chunk_size: int,
    chunk_threshold: int,
    checksum: bool = True,
    block_size: int = BLOCK_SIZE_MAX,
) -> bool:
    try:
        size = src.stat().st_size
    except OSError:
        return False
    if size == 0:
        return False
    try:
        if size < chunk_threshold or workers <= 1:
            dst.write_bytes(_compress_bytes(src.read_bytes(), level, block_size, checksum))
            return True
        n = (size + chunk_size - 1) // chunk_size
        with src.open("rb") as f:
            chunks = [f.read(chunk_size) for _ in range(n)]
        with ProcessPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(_chunk_worker, [(c, level, block_size, checksum) for c in chunks]))
        with dst.open("wb") as o:
            for r in results:
                o.write(r)
        return True
    except Exception as e:
        print(f"  ! Failed to compress {src}: {e}", file=sys.stderr)
        if dst.exists():
            with contextlib.suppress(OSError):
                dst.unlink()
        return False


def tar_compress_folder(folder: Path, *, level: int, checksum: bool, block_size: int, keep: bool) -> tuple[bool, str]:
    dst = folder.parent / (folder.name + ".tar.lz4")
    if dst.exists():
        return False, f"Skipped {folder.name}: {dst.name} already exists"
    try:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            tf.add(str(folder), arcname=folder.name)
        raw = buf.getvalue()
        comp = _compress_bytes(raw, level, block_size, checksum)
        dst.write_bytes(comp)
        if not keep:
            shutil.rmtree(folder)
        return True, f"{folder.name}: {human_size(len(raw))} -> {human_size(len(comp))}"
    except Exception as e:
        if dst.exists():
            with contextlib.suppress(OSError):
                dst.unlink()
        return False, f"Error compressing {folder.name}: {e}"


def tar_decompress(archive: Path) -> tuple[bool, str]:
    name = archive.name
    if not name.endswith(".tar.lz4"):
        return False, f"Skipped {name}: not a .tar.lz4 file"
    stem = name[: -len(".tar.lz4")]
    target = archive.parent / stem
    if target.exists():
        return False, f"Skipped {name}: {target.name}/ already exists"
    try:
        data = lz4.frame.decompress(archive.read_bytes())
        with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tf:
            try:
                tf.extractall(path=str(archive.parent), filter="data")
            except TypeError:
                tf.extractall(path=str(archive.parent))
        archive.unlink()
        return True, f"{name} -> {target.name}/"
    except Exception as e:
        return False, f"Failed to extract {name}: {e}"


def plain_decompress(archive: Path) -> tuple[bool, str]:
    target = archive.with_suffix("")
    try:
        data = archive.read_bytes()
        if not data:
            return False, f"Skipped {archive.name}: empty file"
        out = lz4.frame.decompress(data)
        target.write_bytes(out)
        archive.unlink()
        return True, f"{archive.name}: {human_size(len(data))} -> {human_size(len(out))}"
    except Exception as e:
        return False, f"Failed to decompress {archive.name}: {e}"


def _should_skip_file(p: Path, skip_ext: set, min_size: int) -> bool:
    if p.is_symlink() or not p.is_file():
        return True
    if p.suffix.lower() in skip_ext:
        return True
    if ".tar." in p.name:
        return True
    try:
        return p.stat().st_size < min_size
    except OSError:
        return True


def cmd_compress(args) -> int:
    skip_ext = {e.lower() for e in (args.skip_ext or DEFAULT_SKIP_EXT)}
    checksum = not args.no_checksum
    block_size = BLOCK_SIZE_MAX
    stats = {"ok": 0, "skip": 0, "err": 0, "before": 0, "after": 0}

    def _do_file(f: Path) -> None:
        if _should_skip_file(f, skip_ext, args.min_size):
            return
        dst = f.parent / (f.name + ".lz4")
        if dst.exists():
            print(f"  - Skipped {f.name}: {dst.name} already exists")
            stats["skip"] += 1
            return
        try:
            orig = f.stat().st_size
        except OSError:
            return
        if not compress_file(
            f,
            dst,
            level=args.level,
            workers=args.workers,
            chunk_size=args.chunk_size,
            chunk_threshold=args.chunk_threshold,
            checksum=checksum,
            block_size=block_size,
        ):
            stats["err"] += 1
            return
        new = dst.stat().st_size
        if new >= orig:
            dst.unlink()
            print(f"  - Skipped {f.name}: no space saved")
            stats["skip"] += 1
            return
        if not args.keep:
            f.unlink()
        pct = (orig - new) / orig * 100
        print(f"  + {f.name}: {human_size(orig)} -> {human_size(new)} ({pct:.1f}% saved)")
        stats["ok"] += 1
        stats["before"] += orig
        stats["after"] += new

    def _do_folder(d: Path) -> None:
        ok, msg = tar_compress_folder(d, level=args.level, checksum=checksum, block_size=block_size, keep=args.keep)
        if ok:
            print(f"  + {msg}")
            stats["ok"] += 1
        else:
            print(f"  - {msg}")
            stats["skip"] += 1

    for root_str in args.paths:
        root = Path(root_str).resolve()
        if not root.exists():
            print(f"Skipped {root}: not found", file=sys.stderr)
            continue
        if root.is_file():
            if args.mode != "folders":
                _do_file(root)
            continue
        if not root.is_dir():
            continue
        if args.mode == "folders":
            for d in sorted(root.iterdir()):
                if d.is_dir() and not d.is_symlink():
                    _do_folder(d)
        elif args.mode == "files":
            it = root.rglob("*") if args.recursive else root.iterdir()
            for f in sorted(p for p in it if p.is_file() and not p.is_symlink()):
                _do_file(f)
        else:
            for p in sorted(root.iterdir()):
                if p.is_symlink():
                    continue
                if p.is_dir():
                    _do_folder(p)
                elif p.is_file():
                    _do_file(p)
    print(f"\n{'=' * 40}")
    print(f"Compressed: {stats['ok']}  skipped: {stats['skip']}  errors: {stats['err']}")
    if stats["before"]:
        saved = stats["before"] - stats["after"]
        print(
            f"Total: {human_size(stats['before'])} -> {human_size(stats['after'])} "
            f"({saved / stats['before'] * 100:.1f}% saved, {human_size(saved)} freed)"
        )
    return 0 if stats["err"] == 0 else 1


def cmd_decompress(args) -> int:
    stats = {"ok": 0, "skip": 0, "err": 0}
    for root_str in args.paths:
        root = Path(root_str).resolve()
        if not root.exists():
            print(f"Skipped {root}: not found", file=sys.stderr)
            continue
        if root.is_file():
            candidates = [root]
        else:
            it = root.rglob("*.lz4") if args.recursive else root.glob("*.lz4")
            candidates = sorted(p for p in it if p.is_file() and not p.is_symlink())
        for a in candidates:
            if not a.name.endswith(".lz4"):
                continue
            if a.name.endswith(".tar.lz4"):
                ok, msg = tar_decompress(a)
            else:
                ok, msg = plain_decompress(a)
            if ok:
                print(f"  + {msg}")
                stats["ok"] += 1
            else:
                print(f"  - {msg}")
                stats["err"] += 1
    print(f"\n{'=' * 40}")
    print(f"Decompressed: {stats['ok']}  errors: {stats['err']}")
    return 0 if stats["err"] == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="merged.py",
        description="Unified LZ4 compression tool (consolidates lz5.py, lzer.py, lzr.py).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("compress", help="Compress files and/or folders")
    c.add_argument("paths", nargs="*", default=["."], help="Paths to process (default: current directory)")
    c.add_argument(
        "--mode",
        choices=["auto", "files", "folders"],
        default="auto",
        help="auto (lzr): files -> .lz4, folders -> .tar.lz4. "
        "files (lzer): compress files only. "
        "folders (lz5): compress each subfolder as .tar.lz4. "
        "Default: auto",
    )
    c.add_argument("--level", type=int, default=DEFAULT_LEVEL, help=f"LZ4 compression level (default: {DEFAULT_LEVEL})")
    c.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Workers for chunked parallel compression (default: {DEFAULT_WORKERS})",
    )
    c.add_argument(
        "--min-size",
        type=int,
        default=DEFAULT_MIN_SIZE,
        help=f"Minimum file size in bytes to compress (default: {DEFAULT_MIN_SIZE})",
    )
    c.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"Chunk size in bytes for parallel compression (default: {DEFAULT_CHUNK_SIZE})",
    )
    c.add_argument(
        "--chunk-threshold",
        type=int,
        default=DEFAULT_CHUNK_THRESHOLD,
        help=f"File size above which chunked parallel compression is used (default: {DEFAULT_CHUNK_THRESHOLD})",
    )
    c.add_argument("--recursive", action="store_true", help="Walk directories recursively (mode=files)")
    c.add_argument("--keep", action="store_true", help="Keep original files/folders after successful compression")
    c.add_argument("--skip-ext", nargs="*", default=None, help="File extensions to skip (default: archive formats)")
    c.add_argument("--no-checksum", action="store_true", help="Disable LZ4 content checksum")
    c.set_defaults(func=cmd_compress)
    d = sub.add_parser("decompress", help="Decompress .lz4 and .tar.lz4 files")
    d.add_argument("paths", nargs="*", default=["."], help="Paths to process (default: current directory)")
    d.add_argument("--recursive", action="store_true", help="Walk directories recursively")
    d.set_defaults(func=cmd_decompress)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
"""

_SOURCES['gzbz'] = r"""#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import argparse
import bz2
import contextlib
import gzip
import mmap
import shutil
import tarfile
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Optional, Self, Sequence

from loguru import logger

DEFAULT_WORKERS: int = 8
DEFAULT_CHUNK_SIZE: int = 524_288
DEFAULT_GZIPER_CHUNK: int = 262_144
DEFAULT_MEM_THRESHOLD: int = 32_768
DEFAULT_MIN_FILE_SIZE: int = 1_024
DEFAULT_BZ2_LEVEL: int = 9
DEFAULT_GZ_LEVEL: int = 9
DEFAULT_GZIPER_LEVEL: int = 9
EXCLUDE_BZ2: frozenset[str] = frozenset({".bz2", ".xz", ".gz", ".br", ".zst", ".7z", ".zip", ".rar"})
EXCLUDE_GZ: frozenset[str] = frozenset({".gz", ".bz2", ".xz", ".br", ".zst", ".7z", ".zip", ".rar"})
EXCLUDE_GZIPER: frozenset[str] = frozenset({".gz", ".zip", ".bz2", ".xz", ".7z", ".rar", ".tar"})


def human_size(n: float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    v = float(n)
    for u in units:
        if v < 1024.0 or u == units[-1]:
            return f"{int(v)} B" if u == "B" else f"{v:.1f} {u}"
        v /= 1024.0
    return f"{v:.1f} TB"


def human_time(seconds: int) -> str:
    return str(timedelta(seconds=seconds))


def compress_bytes(data: bytes, algo: str, level: int) -> bytes:
    if algo == "bz2":
        return bz2.compress(data, compresslevel=level)
    if algo == "gz":
        return gzip.compress(data, compresslevel=level)
    msg = f"unknown algorithm: {algo!r}"
    raise ValueError(msg)


def decompress_bytes(data: bytes, algo: str) -> bytes:
    if algo == "bz2":
        return bz2.decompress(data)
    if algo == "gz":
        return gzip.decompress(data)
    msg = f"unknown algorithm: {algo!r}"
    raise ValueError(msg)


def _worker_compress_chunk(args: tuple[int, bytes, str, int]) -> tuple[int, bytes]:
    idx, data, algo, level = args
    return idx, compress_bytes(data, algo, level)


def _worker_gzip_file(args: tuple[str, int, int]) -> tuple[str, bool, int, int, str]:
    path_str, chunk_size, level = args
    src = Path(path_str)
    dst = Path(str(src) + ".gz")
    try:
        size = src.stat().st_size
        with open(src, "rb") as fi, gzip.open(dst, "wb", compresslevel=level) as fo:
            shutil.copyfileobj(fi, fo, chunk_size)
        csize = dst.stat().st_size
        src.unlink()
        return (path_str, True, size, csize, "")
    except Exception as exc:
        if dst.exists():
            with contextlib.suppress(OSError):
                dst.unlink()
        return (path_str, False, 0, 0, str(exc))


@dataclass
class Config:
    algo: str
    level: int
    workers: int = DEFAULT_WORKERS
    chunk_size: int = DEFAULT_CHUNK_SIZE
    mem_threshold: int = DEFAULT_MEM_THRESHOLD
    min_file_size: int = DEFAULT_MIN_FILE_SIZE

    @property
    def file_ext(self) -> str:
        return ".bz2" if self.algo == "bz2" else ".gz"

    @property
    def tar_ext(self) -> str:
        return ".tar.bz2" if self.algo == "bz2" else ".tar.gz"

    @property
    def exclude_ext(self) -> frozenset[str]:
        return EXCLUDE_BZ2 if self.algo == "bz2" else EXCLUDE_GZ


class Tool:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._pool: Optional[ProcessPoolExecutor] = None

    def _get_pool(self) -> ProcessPoolExecutor:
        if self._pool is None:
            self._pool = ProcessPoolExecutor(max_workers=self.cfg.workers)
        return self._pool

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _compress_small(self, src: Path, dst: Path) -> bool:
        try:
            data = src.read_bytes()
            if not data:
                return False
            dst.write_bytes(compress_bytes(data, self.cfg.algo, self.cfg.level))
            return True
        except (OSError, MemoryError) as exc:
            logger.error(f"Memory compression failed for {src.name}: {exc}")
            return False

    def _compress_chunked(self, src: Path, dst: Path, size: int) -> bool:
        chunk = self.cfg.chunk_size
        try:
            n = (size + chunk - 1) // chunk
            with (
                dst.open("wb", buffering=1 << 20) as out,
                src.open("rb") as fh,
                mmap.mmap(fh.fileno(), length=0, access=mmap.ACCESS_READ) as mm,
            ):
                slices = [(i, mm[i * chunk : min((i + 1) * chunk, size)]) for i in range(n)]
            pool = self._get_pool()
            futures = [
                pool.submit(_worker_compress_chunk, (idx, data, self.cfg.algo, self.cfg.level)) for idx, data in slices
            ]
            outputs: list[Optional[bytes]] = [None] * n
            for fut in futures:
                try:
                    idx, blob = fut.result()
                    outputs[idx] = blob
                except Exception as exc:
                    logger.error(f"Chunk compression failed: {exc}")
                    return False
            with dst.open("wb", buffering=1 << 20) as out:
                for blob in outputs:
                    if blob is None:
                        return False
                    out.write(blob)
            return True
        except (OSError, MemoryError) as exc:
            logger.error(f"Chunked compression failed for {src.name}: {exc}")
            return False

    def _compress_file(self, src: Path, dst: Path, size: int) -> bool:
        if size < self.cfg.mem_threshold:
            return self._compress_small(src, dst)
        return self._compress_chunked(src, dst, size)

    @staticmethod
    def _create_tar(src_dir: Path, tar_path: Path) -> bool:
        try:
            with tarfile.open(tar_path, "w") as tf:
                for p in src_dir.rglob("*"):
                    if p.is_file():
                        arcname = p.relative_to(src_dir.parent)
                        tf.add(p, arcname=str(arcname))
            return True
        except (OSError, tarfile.TarError) as exc:
            logger.error(f"  Failed to create tar archive: {exc}")
            return False

    @staticmethod
    def _extract_tar(tar_path: Path, dest: Path) -> bool:
        try:
            dest.mkdir(parents=True, exist_ok=True)
            with tarfile.open(tar_path, "r") as tf:
                tf.extractall(dest)
            return True
        except (OSError, tarfile.TarError) as exc:
            logger.error(f"  Failed to extract tar archive: {exc}")
            return False

    def compress_dir(self, src_dir: Path) -> bool:
        tar_path = src_dir.with_name(src_dir.name + ".tar")
        out_path = src_dir.with_name(src_dir.name + self.cfg.tar_ext)
        try:
            print("  Creating tar archive...")
            if not self._create_tar(src_dir, tar_path) or not tar_path.exists():
                logger.error("  Failed to create tar archive")
                return False
            print(f"  Compressing tar archive with {self.cfg.algo} (level {self.cfg.level})...")
            size = tar_path.stat().st_size
            if not self._compress_file(tar_path, out_path, size):
                return False
            if not out_path.exists():
                return False
            csize = out_path.stat().st_size
            if csize == 0:
                logger.warning(f"Compressed archive empty for {src_dir.name}")
                out_path.unlink()
                return False
            if csize < size:
                tar_path.unlink()
                saved_pct = (size - csize) / size * 100
                print(f"  ✓ Compressed archive: {saved_pct:.1f}% saved ({human_size(size)} → {human_size(csize)})")
                shutil.rmtree(src_dir)
                return True
            print("  ✗ Archive compression didn't save space, keeping .tar")
            out_path.unlink()
            return False
        except (OSError, MemoryError, tarfile.TarError) as exc:
            logger.error(f"  ✗ Failed to compress tar archive: {exc}")
            if tar_path.exists():
                tar_path.unlink()
            if out_path.exists():
                out_path.unlink()
            return False

    def compress_single_file(self, src: Path) -> tuple[bool, int, int]:
        dst = Path(str(src) + self.cfg.file_ext)
        if dst.exists():
            print(f"Skipping {src.name} - output already exists")
            return (False, 0, 0)
        try:
            size = src.stat().st_size
            if not size:
                return (False, 0, 0)
            if not self._compress_file(src, dst, size) or not dst.exists():
                return (False, 0, 0)
            csize = dst.stat().st_size
            if csize == 0:
                logger.warning(f"Compressed file empty for {src.name}")
                dst.unlink()
                return (False, 0, 0)
            if csize < size:
                src.unlink()
                saved_pct = (size - csize) / size * 100
                print(f"  ✓ {src.name}: {saved_pct:.1f}% saved ({human_size(size)} → {human_size(csize)})")
                return (True, size, csize)
            print(f"  ✗ {src.name}: No space saved, removing compressed file")
            dst.unlink()
            return (False, 0, 0)
        except (OSError, PermissionError, MemoryError) as exc:
            logger.error(f"  ✗ Failed to compress {src.name}: {exc}")
            return (False, 0, 0)

    def decompress_single_file(self, src: Path) -> bool:
        if src.suffix != self.cfg.file_ext:
            return False
        dst = src.with_suffix("")
        try:
            data = src.read_bytes()
            if not data:
                return False
            dst.write_bytes(decompress_bytes(data, self.cfg.algo))
            print(f"  ✓ Decompressed {src.name}: {human_size(src.stat().st_size)} → {human_size(dst.stat().st_size)}")
            src.unlink()
            return True
        except (OSError, EOFError, ValueError) as exc:
            logger.error(f"  ✗ Failed to decompress {src.name}: {exc}")
            return False

    def decompress_archive(self, archive: Path) -> bool:
        tar_path = archive.with_suffix("")
        dest = archive.parent
        try:
            print(f"\n  Decompressing {archive.name}...")
            data = archive.read_bytes()
            tar_path.write_bytes(decompress_bytes(data, self.cfg.algo))
            print(f"    Extracting tar to {dest}/...")
            if self._extract_tar(tar_path, dest):
                tar_path.unlink()
                archive.unlink()
                print(f"  ✓ Extracted {archive.name} to {dest}/")
                return True
            logger.error(f"  ✗ Failed to extract {archive.name}")
            return False
        except (OSError, EOFError, ValueError, tarfile.TarError) as exc:
            logger.error(f"  ✗ Failed to decompress {archive.name}: {exc}")
            if tar_path.exists():
                tar_path.unlink()
            return False

    def collect_dirs(self, root: Path) -> list[Path]:
        return [p for p in root.glob("*") if p.is_dir() and not p.is_symlink()]

    def collect_files(self, root: Path) -> list[Path]:
        out: list[Path] = []
        for p in root.glob("*"):
            if not p.is_file() or p.is_symlink():
                continue
            if p.suffix in self.cfg.exclude_ext:
                continue
            try:
                if p.stat().st_size >= self.cfg.min_file_size:
                    out.append(p)
            except (OSError, PermissionError):
                pass
        return out

    def collect_archives(self, root: Path) -> list[Path]:
        return [p for p in root.glob(f"*{self.cfg.tar_ext}") if p.is_file() and not p.is_symlink()]

    def collect_compressed(self, root: Path) -> list[Path]:
        ext = self.cfg.file_ext
        return [
            p
            for p in root.glob(f"*{ext}")
            if p.is_file() and not p.is_symlink() and not p.name.endswith(self.cfg.tar_ext)
        ]

    def compress(self, paths: Sequence[Path]) -> None:
        cwd = Path.cwd()
        if paths:
            dirs = [p for p in paths if p.is_dir()]
            files = [p for p in paths if p.is_file()]
        else:
            dirs = self.collect_dirs(cwd)
            files = self.collect_files(cwd)
        if dirs:
            print(f"\n📁 Compressing {len(dirs)} directories...")
            for d in sorted(dirs):
                try:
                    rel = d.relative_to(cwd)
                except ValueError:
                    rel = d
                print(f"\n  Processing {rel}...")
                if self.compress_dir(d):
                    print(f"  ✓ Successfully compressed {rel} to {d.name}{self.cfg.tar_ext}")
                else:
                    logger.error(f"  ✗ Failed to compress {rel}")
        if not files:
            print("\n📄 No files to compress")
            return
        print(f"\n📄 Compressing {len(files)} files with {self.cfg.algo} max compression...")
        orig = comp = ok = 0
        for i, f in enumerate(sorted(files), 1):
            print(f"\n[{i}/{len(files)}] {f.name}")
            success, o, c = self.compress_single_file(f)
            if success:
                ok += 1
                orig += o
                comp += c
        if ok > 0:
            saved = orig - comp
            pct = saved / orig * 100 if orig else 0.0
            print(f"\n{'=' * 40}")
            print(f"✅ Compressed {ok}/{len(files)} files")
            print(f"📊 Original size: {human_size(orig)}")
            print(f"📦 Compressed size: {human_size(comp)}")
            print(f"💾 Space saved: {human_size(saved)} ({pct:.1f}%)")
            print(f"{'=' * 40}")
        elif files:
            logger.error("\n❌ No files were successfully compressed")

    def decompress(self, paths: Sequence[Path]) -> None:
        cwd = Path.cwd()
        if paths:
            archives = [p for p in paths if p.is_file() and p.name.endswith(self.cfg.tar_ext)]
        else:
            archives = self.collect_archives(cwd)
        if archives:
            print(f"\n📦 Decompressing {len(archives)} archives...")
            for a in sorted(archives):
                self.decompress_archive(a)
        if paths:
            singles = [
                p
                for p in paths
                if p.is_file() and p.suffix == self.cfg.file_ext and not p.name.endswith(self.cfg.tar_ext)
            ]
        else:
            singles = self.collect_compressed(cwd)
        if not singles:
            print(f"\n📄 No {self.cfg.file_ext} files to decompress")
            return
        print(f"\n📄 Decompressing {len(singles)} {self.cfg.algo} files...")
        total_in = total_out = ok = 0
        for i, f in enumerate(sorted(singles), 1):
            print(f"\n[{i}/{len(singles)}] {f.name}")
            with contextlib.suppress(OSError):
                total_in += f.stat().st_size
            if self.decompress_single_file(f):
                ok += 1
                dst = f.with_suffix("")
                if dst.exists():
                    total_out += dst.stat().st_size
        if ok > 0:
            print(f"\n{'=' * 40}")
            print(f"✅ Decompressed {ok}/{len(singles)} files")
            print(f"📦 Compressed size: {human_size(total_in)}")
            print(f"📊 Decompressed size: {human_size(total_out)}")
            print(f"{'=' * 40}")
        elif singles:
            logger.error("\n❌ No files were successfully decompressed")


def run_gzip_files(
    directories: Sequence[Path],
    excludes: Sequence[str],
    workers: int,
    chunk_size: int,
    level: int,
) -> int:
    exclude_set: set[str] = set(EXCLUDE_GZIPER)
    for e in excludes:
        if not e.startswith("."):
            e = "." + e
        exclude_set.add(e)
    resolved_dirs = [d.resolve() for d in directories]
    print("=" * 40)
    print("🔍 GZIP Compression Tool (Maximum Compression - Level 9)".center(70))
    print("-" * 40)
    print("📂 Processing directories:")
    for d in resolved_dirs:
        print("   •", d)
    print("🚫 Excluding extensions:", ",".join(sorted(exclude_set)))
    files: list[Path] = []
    for d in resolved_dirs:
        if not d.exists():
            logger.warning(f"Directory '{d}' does not exist, skipping...")
            continue
        for p in d.rglob("*"):
            if p.is_file() and p.suffix not in exclude_set:
                files.append(p)
    if not files:
        logger.success("✅ No files found to compress!")
        return 0
    print("📊 Found", len(files), "file(s) to compress")
    print("-" * 40)
    print(f"{'File':<50} {'Original':>10} {'Compressed':>10} {'Ratio':>8} {'Status':>10}")
    print("-" * 40)
    total_files = ok = fail = 0
    total_orig = total_comp = 0
    start = time.time()
    cwd = Path.cwd()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_worker_gzip_file, (str(f), chunk_size, level)) for f in files]
        for fut in futures:
            path_str, success, o, c, err = fut.result()
            total_files += 1
            try:
                rel = str(Path(path_str).relative_to(cwd))
            except ValueError:
                rel = path_str
            display = ("..." + rel[-44:]) if len(rel) > 47 else rel
            if success:
                ok += 1
                total_orig += o
                total_comp += c
                ratio = f"{(1 - c / o) * 100:.1f}%" if o else "N/A"
                print(f"{display:<50} {human_size(o):>10} {human_size(c):>10} {ratio:>8} {'✅':>10}")
            else:
                fail += 1
                print(f"{display:<50} {'N/A':>10} {'N/A':>10} {'N/A':>8} {'❌':>10}")
                if err:
                    logger.warning(f"   ⚠ Error: {err}")
    elapsed = time.time() - start
    print("=" * 40)
    print("📊 COMPRESSION SUMMARY".center(70))
    print("-" * 40)
    print(f"  Total files processed: {total_files}")
    print(f"  Successfully compressed: {ok} ✅")
    print(f"  Failed compressions: {fail} ❌")
    print(f"  Original total size: {human_size(total_orig)}")
    print(f"  Compressed total size: {human_size(total_comp)}")
    if total_orig > 0:
        ratio = (1 - total_comp / total_orig) * 100
        saved = total_orig - total_comp
        print(f"  Overall compression ratio: {ratio:.1f}%")
        print(f"  Space saved: {human_size(saved)}")
    print(f"  Time elapsed: {human_time(int(elapsed))}")
    print("-" * 40)
    return 0


def _add_pool_options(sp: argparse.ArgumentParser, algo_default_level: int, chunk_default: int) -> None:
    sp.add_argument("-d", "--decompress", action="store_true", help="Decompress instead of compress")
    sp.add_argument(
        "-c",
        "--compress",
        action="store_true",
        help="Explicitly select compression (default)",
    )
    sp.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Files or directories to process (default: current working directory)",
    )
    sp.add_argument(
        "-w",
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Parallel worker processes (default: {DEFAULT_WORKERS})",
    )
    sp.add_argument(
        "-l",
        "--level",
        type=int,
        default=algo_default_level,
        help=f"Compression level (default: {algo_default_level})",
    )
    sp.add_argument(
        "--chunk-size",
        type=int,
        default=chunk_default,
        help=f"Chunk size for parallel compression in bytes (default: {chunk_default})",
    )
    sp.add_argument(
        "--mem-threshold",
        type=int,
        default=DEFAULT_MEM_THRESHOLD,
        help=f"Files smaller than this are compressed in memory (default: {DEFAULT_MEM_THRESHOLD})",
    )
    sp.add_argument(
        "--min-file-size",
        type=int,
        default=DEFAULT_MIN_FILE_SIZE,
        help=f"Ignore files smaller than this many bytes (default: {DEFAULT_MIN_FILE_SIZE})",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="compress_tool",
        description="Unified bz2/gz compression & decompression tool.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Mapping from the original scripts:\n"
            "  bzr.py     -> compress_tool.py bz2         [paths...]\n"
            "  bzr.py -d  -> compress_tool.py bz2 -d      [paths...]\n"
            "  gzr.py     -> compress_tool.py gz          [paths...]\n"
            "  gzr.py -d  -> compress_tool.py gz  -d      [paths...]\n"
            "  gziper.py  -> compress_tool.py gzip-files  [dirs...]\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=False)
    bz2_p = sub.add_parser("bz2", help="bzip2 compression / decompression (bzr.py)")
    _add_pool_options(bz2_p, DEFAULT_BZ2_LEVEL, DEFAULT_CHUNK_SIZE)
    gz_p = sub.add_parser("gz", help="gzip compression / decompression with tar support (gzr.py)")
    _add_pool_options(gz_p, DEFAULT_GZ_LEVEL, DEFAULT_CHUNK_SIZE)
    gf_p = sub.add_parser("gzip-files", help="recursive gzip on files only (gziper.py)")
    gf_p.add_argument(
        "directories",
        nargs="*",
        type=Path,
        help="Directories to walk recursively (default: '.')",
    )
    gf_p.add_argument(
        "-e",
        "--exclude",
        nargs="+",
        default=[],
        help="Additional file extensions to exclude (e.g. .pdf .jpg)",
    )
    gf_p.add_argument(
        "-w",
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Parallel worker processes (default: {DEFAULT_WORKERS})",
    )
    gf_p.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_GZIPER_CHUNK,
        help=f"Streaming copy buffer size in bytes (default: {DEFAULT_GZIPER_CHUNK})",
    )
    gf_p.add_argument(
        "-l",
        "--level",
        type=int,
        default=DEFAULT_GZIPER_LEVEL,
        help=f"gzip compression level (default: {DEFAULT_GZIPER_LEVEL})",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        if args.command == "gzip-files":
            return run_gzip_files(
                directories=args.directories or [Path()],
                excludes=args.exclude,
                workers=args.workers,
                chunk_size=args.chunk_size,
                level=args.level,
            )
        algo = "bz2" if args.command == "bz2" else "gz"
        cfg = Config(
            algo=algo,
            level=args.level,
            workers=args.workers,
            chunk_size=args.chunk_size,
            mem_threshold=args.mem_threshold,
            min_file_size=args.min_file_size,
        )
        paths = [p.resolve() for p in (args.paths or [])]
        with Tool(cfg) as tool:
            if args.decompress:
                tool.decompress(paths)
            else:
                tool.compress(paths)
        return 0
    except KeyboardInterrupt:
        logger.warning("\n\n⚠️  Interrupted by user")
        return 1
    except Exception as exc:
        logger.error(f"\n❌ Unexpected error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
"""

_SOURCES['text'] = r"""#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import argparse
import bz2
import hashlib
import importlib.util
import json
import lzma
import multiprocessing as mp
import os
import queue
import shutil
import struct
import sys
import tarfile
import tempfile
import time
import traceback
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ProcessPoolExecutor, wait
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from multiprocessing.queues import Queue as MPQueue
from pathlib import Path
from typing import Final, TypeAlias

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
)
from rich.table import Table
from rich.text import Text

CHUNK: Final[int] = 1 << 20
REPORT_INTERVAL: Final[float] = 0.1
POLL_INTERVAL: Final[float] = 0.05
EXTREME: Final[int] = lzma.PRESET_EXTREME
PPMD_MAGIC: Final[bytes] = b"PPMD"
PPMD_HEADER: Final[struct.Struct] = struct.Struct(">4sBIQ")
ORDER: Final[list[str]] = ["brotli", "pyppmd", "zstd", "xz", "py7zr", "bz2"]
SUFFIX: Final[dict[str, str]] = {
    "zstd": ".zst",
    "brotli": ".br",
    "py7zr": ".7z",
    "bz2": ".bz2",
    "xz": ".xz",
    "pyppmd": ".ppmd",
}
MODULE: Final[dict[str, str]] = {
    "zstd": "zstandard",
    "brotli": "brotli",
    "py7zr": "py7zr",
    "bz2": "bz2",
    "xz": "lzma",
    "pyppmd": "pyppmd",
}
LEVELS: Final[dict[str, dict[str, int]]] = {
    "max": {
        "zstd": 22,
        "brotli": 11,
        "xz": 9 | EXTREME,
        "bz2": 9,
        "py7zr": 9 | EXTREME,
        "pyppmd": 16,
    },
    "balanced": {
        "zstd": 19,
        "brotli": 9,
        "xz": 6,
        "bz2": 9,
        "py7zr": 6,
        "pyppmd": 8,
    },
    "fast": {
        "zstd": 3,
        "brotli": 5,
        "xz": 3,
        "bz2": 6,
        "py7zr": 3,
        "pyppmd": 6,
    },
}
Message: TypeAlias = tuple[str, int, int | None, str]
_queue: MPQueue[Message] | None = None


class CliError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Codec:
    feed: Callable[[bytes], bytes]
    finish: Callable[[], bytes]
    header: bytes = b""
    skip: int = 0


@dataclass(frozen=True, slots=True)
class Job:
    name: str
    src: Path
    dst: Path
    size: int
    level: int
    digest: str | None


@dataclass(slots=True)
class Result:
    name: str
    path: str
    size: int
    seconds: float
    verified: bool | None
    error: str | None


def _init_worker(q: MPQueue[Message]) -> None:
    global _queue
    _queue = q


class Reporter:
    def __init__(self, name: str) -> None:
        self.name = name
        self._last = 0.0

    def __call__(
        self,
        done: int,
        total: int | None,
        status: str = "running",
        force: bool = False,
    ) -> None:
        now = time.perf_counter()
        if force or now - self._last >= REPORT_INTERVAL:
            self._last = now
            if _queue is not None:
                _queue.put((self.name, done, total, status))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def part_path(dst: Path) -> Path:
    return dst.with_name(dst.name + ".part")


def zstd_encoder(level: int, size: int) -> Codec:
    import zstandard as zstd

    cctx: zstd.ZstdCompressor | None = None
    if level >= 20:
        try:
            params = zstd.ZstdCompressionParameters.from_level(level, window_log=27, enable_ldm=1)
            cctx = zstd.ZstdCompressor(compression_params=params)
        except (TypeError, ValueError, zstd.ZstdError):
            cctx = None
    if cctx is None:
        cctx = zstd.ZstdCompressor(level=level)
    obj = cctx.compressobj(size=size)
    return Codec(obj.compress, obj.flush)


def zstd_decoder(path: Path) -> Codec:
    import zstandard as zstd

    obj = zstd.ZstdDecompressor(max_window_size=1 << 31).decompressobj()
    return Codec(obj.decompress, lambda: b"")


def brotli_encoder(level: int, size: int) -> Codec:
    import brotli

    comp = brotli.Compressor(quality=level, lgwin=24, mode=brotli.MODE_TEXT)
    return Codec(comp.process, comp.finish)


def brotli_decoder(path: Path) -> Codec:
    import brotli

    dec = brotli.Decompressor()
    return Codec(dec.process, lambda: b"")


def bz2_encoder(level: int, size: int) -> Codec:
    comp = bz2.BZ2Compressor(level)
    return Codec(comp.compress, comp.flush)


def bz2_decoder(path: Path) -> Codec:
    dec = bz2.BZ2Decompressor()
    return Codec(dec.decompress, lambda: b"")


def xz_encoder(level: int, size: int) -> Codec:
    comp = lzma.LZMACompressor(format=lzma.FORMAT_XZ, check=lzma.CHECK_CRC64, preset=level)
    return Codec(comp.compress, comp.flush)


def xz_decoder(path: Path) -> Codec:
    dec = lzma.LZMADecompressor()
    return Codec(dec.decompress, lambda: b"")


def ppmd_encoder(level: int, size: int) -> Codec:
    import pyppmd

    mem = min(max(16 << 20, size), 256 << 20)
    enc = pyppmd.Ppmd7Encoder(level, mem)
    header = PPMD_HEADER.pack(PPMD_MAGIC, level, mem, size)
    return Codec(enc.encode, lambda: enc.flush(endmark=True), header)


def ppmd_decoder(path: Path) -> Codec:
    import pyppmd

    with path.open("rb") as f:
        magic, order, mem, _ = PPMD_HEADER.unpack(f.read(PPMD_HEADER.size))
    if magic != PPMD_MAGIC:
        raise ValueError("invalid ppmd header")
    dec = pyppmd.Ppmd7Decoder(order, mem)
    return Codec(dec.decode, dec.flush, skip=PPMD_HEADER.size)


ENCODERS: Final[dict[str, Callable[[int, int], Codec]]] = {
    "zstd": zstd_encoder,
    "brotli": brotli_encoder,
    "bz2": bz2_encoder,
    "xz": xz_encoder,
    "pyppmd": ppmd_encoder,
}
DECODERS: Final[dict[str, Callable[[Path], Codec]]] = {
    "zstd": zstd_decoder,
    "brotli": brotli_decoder,
    "bz2": bz2_decoder,
    "xz": xz_decoder,
    "pyppmd": ppmd_decoder,
}


def stream_compress(src: Path, dst: Path, codec: Codec, total: int, report: Reporter) -> None:
    done = 0
    with src.open("rb") as fi, dst.open("wb") as fo:
        fo.write(codec.header)
        while chunk := fi.read(CHUNK):
            fo.write(codec.feed(chunk))
            done += len(chunk)
            report(done, total)
        fo.write(codec.finish())
    report(total, total, force=True)


def compress_7z(src: Path, dst: Path, level: int, report: Reporter) -> None:
    import py7zr

    filters = [{"id": py7zr.FILTER_LZMA2, "preset": level}]
    report(0, None, force=True)
    with py7zr.SevenZipFile(dst, "w", filters=filters) as z:
        z.write(src, arcname=src.name)


def verify_output(job: Job, path: Path, report: Reporter) -> bool:
    expected = job.digest
    if expected is None:
        raise ValueError("no digest available for verification")
    if job.name == "py7zr":
        import py7zr

        report(0, job.size, "verifying", force=True)
        with tempfile.TemporaryDirectory(dir=path.parent) as tmp:
            with py7zr.SevenZipFile(path) as z:
                z.extractall(path=tmp)
            return sha256_file(Path(tmp) / job.src.name) == expected
    codec = DECODERS[job.name](path)
    h = hashlib.sha256()
    done = 0
    with path.open("rb") as f:
        f.seek(codec.skip)
        while chunk := f.read(CHUNK):
            out = codec.feed(chunk)
            h.update(out)
            done += len(out)
            report(done, job.size, f"verifying {done * 100 // max(job.size, 1)}%")
        h.update(codec.finish())
    return h.hexdigest() == expected


def run_job(job: Job) -> Result:
    report = Reporter(job.name)
    part = part_path(job.dst)
    verified: bool | None = None
    start = time.perf_counter()
    elapsed = 0.0
    try:
        report(0, job.size, force=True)
        if job.name == "py7zr":
            compress_7z(job.src, part, job.level, report)
        else:
            codec = ENCODERS[job.name](job.level, job.size)
            stream_compress(job.src, part, codec, job.size, report)
        elapsed = time.perf_counter() - start
        if job.digest is not None:
            verified = False
            verified = verify_output(job, part, report)
            if not verified:
                raise ValueError("verification failed: checksum mismatch")
        os.replace(part, job.dst)
        return Result(job.name, str(job.dst), job.dst.stat().st_size, elapsed, verified, None)
    except Exception:
        part.unlink(missing_ok=True)
        return Result(
            job.name,
            str(job.dst),
            0,
            elapsed or time.perf_counter() - start,
            verified,
            traceback.format_exc(),
        )


def drain(
    q: MPQueue[Message],
    progress: Progress,
    tasks: dict[str, TaskID],
    started: set[str],
    finished: set[str],
) -> None:
    while True:
        try:
            name, done, total, status = q.get_nowait()
        except queue.Empty:
            return
        if name in finished:
            continue
        tid = tasks[name]
        if name not in started:
            progress.start_task(tid)
            started.add(name)
        if status == "running":
            progress.update(tid, completed=done, total=total, status="[cyan]running[/]")
        else:
            progress.update(tid, status=f"[yellow]{status}[/]")


def run_jobs(jobs: list[Job], workers: int, progress: Progress) -> list[Result]:
    ctx = mp.get_context("spawn")
    q: MPQueue[Message] = ctx.Queue()
    tasks: dict[str, TaskID] = {
        j.name: progress.add_task(
            j.name,
            total=None if j.name == "py7zr" else j.size,
            start=False,
            status="[dim]queued[/]",
        )
        for j in jobs
    }
    started: set[str] = set()
    finished: set[str] = set()
    results: dict[str, Result] = {}
    executor = ProcessPoolExecutor(
        max_workers=workers,
        mp_context=ctx,
        initializer=_init_worker,
        initargs=(q,),
    )
    pending: dict[Future[Result], Job] = {executor.submit(run_job, j): j for j in jobs}
    try:
        while pending:
            drain(q, progress, tasks, started, finished)
            done, _ = wait(list(pending), timeout=POLL_INTERVAL)
            for fut in done:
                job = pending.pop(fut)
                try:
                    res = fut.result()
                except Exception:
                    res = Result(job.name, str(job.dst), 0, 0.0, None, traceback.format_exc())
                results[job.name] = res
                finished.add(job.name)
                tid = tasks[job.name]
                if job.name not in started:
                    progress.start_task(tid)
                    started.add(job.name)
                if res.error:
                    progress.update(tid, status="[red]failed[/]")
                else:
                    tag = " + verified" if res.verified else ""
                    progress.update(
                        tid,
                        completed=job.size,
                        total=job.size,
                        status=f"[green]done{tag}[/]",
                    )
                progress.stop_task(tid)
    except KeyboardInterrupt:
        executor.shutdown(wait=False, cancel_futures=True)
        for job in jobs:
            if job.name not in finished:
                part_path(job.dst).unlink(missing_ok=True)
        raise
    executor.shutdown(wait=True)
    return [results[j.name] for j in jobs]


def ensure_space(directory: Path, needed: int) -> None:
    free = shutil.disk_usage(directory).free
    if free < needed + (needed >> 5) + (1 << 20):
        raise CliError(f"not enough free space in {directory}: need ~{needed / 1e6:,.0f} MB, have {free / 1e6:,.0f} MB")


def build_tar(folder: Path, dest: Path, root_name: str, console: Console) -> list[str]:
    entries = sorted(folder.rglob("*"))
    total = 0
    for p in entries:
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    ensure_space(dest.parent, total)
    skipped: list[str] = []
    columns = (
        TextColumn("[bold]tar[/]"),
        BarColumn(),
        TaskProgressColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeElapsedColumn(),
    )
    with Progress(*columns, console=console) as progress:
        task = progress.add_task("tar", total=total)
        with tarfile.open(dest, "w") as tar:
            tar.add(folder, arcname=root_name, recursive=False)
            for p in entries:
                if p == dest:
                    continue
                try:
                    arc = str(Path(root_name) / p.relative_to(folder))
                    tar.add(p, arcname=arc, recursive=False)
                    if p.is_file() and not p.is_symlink():
                        progress.advance(task, p.stat().st_size)
                except OSError as exc:
                    skipped.append(f"{p}: {exc}")
    return skipped


def resolve_algos(requested: Sequence[str], console: Console) -> list[str]:
    wanted = [a for a in ORDER if a in set(requested)]
    available: list[str] = []
    for name in wanted:
        if importlib.util.find_spec(MODULE[name]) is None:
            console.print(f"[yellow]skipping {name}: module '{MODULE[name]}' is not installed[/]")
        else:
            available.append(name)
    if not available:
        raise CliError("no usable algorithms")
    return available


def print_report(console: Console, results: list[Result], orig: int, wall: float) -> None:
    ok = sorted((r for r in results if not r.error), key=lambda r: r.size)
    bad = [r for r in results if r.error]
    fastest = min(ok, key=lambda r: r.seconds).name if ok else None
    table = Table(header_style="bold", show_lines=False)
    table.add_column("#", justify="right")
    table.add_column("algorithm")
    table.add_column("size", justify="right")
    table.add_column("ratio", justify="right")
    table.add_column("saved", justify="right")
    table.add_column("time", justify="right")
    table.add_column("speed", justify="right")
    table.add_column("verified", justify="center")
    table.add_column("output", overflow="fold")
    for rank, r in enumerate(ok, 1):
        ratio = orig / r.size if r.size else 0.0
        saved = (1 - r.size / orig) * 100 if orig else 0.0
        speed = orig / r.seconds / 1e6 if r.seconds else 0.0
        size_cell = f"{r.size:,}"
        time_cell = f"{r.seconds:.1f}s"
        if rank == 1:
            size_cell = f"[bold green]{size_cell}[/]"
        if r.name == fastest:
            time_cell = f"[bold green]{time_cell}[/]"
        mark = "-" if r.verified is None else ("[green]yes[/]" if r.verified else "[red]no[/]")
        table.add_row(
            str(rank),
            r.name,
            size_cell,
            f"{ratio:.2f}x",
            f"{saved:.1f}%",
            time_cell,
            f"{speed:.1f} MB/s",
            mark,
            r.path,
        )
    for r in bad:
        table.add_row("-", r.name, "[red]ERROR[/]", "", "", f"{r.seconds:.1f}s", "", "", "")
    console.print(table)
    for r in bad:
        console.print(Panel(Text(r.error or ""), title=r.name, border_style="red"))
    console.print(f"Wall time: {wall:.1f}s")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="compress_txt.py")
    p.add_argument("path", type=Path)
    p.add_argument("-o", "--out-dir", type=Path)
    p.add_argument("-p", "--preset", choices=list(LEVELS), default="max")
    p.add_argument("-a", "--algos", nargs="+", choices=ORDER, default=ORDER, metavar="ALGO")
    p.add_argument("-w", "--workers", type=int, default=min(4, os.cpu_count() or 1))
    p.add_argument("--verify", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--keep-tar", action="store_true")
    p.add_argument("--tmp-dir", type=Path)
    p.add_argument("--json", type=Path, dest="json_path")
    return p.parse_args(argv)


def run(args: argparse.Namespace, console: Console) -> int:
    src: Path = args.path.expanduser().resolve()
    if not src.exists():
        raise CliError(f"not found: {src}")
    if not (src.is_file() or src.is_dir()):
        raise CliError(f"unsupported path type: {src}")
    if args.workers < 1:
        raise CliError("--workers must be >= 1")
    out_dir: Path = args.out_dir.expanduser().resolve() if args.out_dir else src.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    names = resolve_algos(args.algos, console)
    base = src.name or "root"
    out_base = f"{base}.tar" if src.is_dir() else base
    outputs = {n: out_dir / f"{out_base}{SUFFIX[n]}" for n in names}
    clashes = [str(p) for p in outputs.values() if p.exists()]
    if clashes and not args.force:
        raise CliError("outputs already exist (use --force):\n  " + "\n  ".join(clashes))
    with ExitStack() as stack:
        if src.is_dir():
            if args.keep_tar:
                input_path = out_dir / out_base
            else:
                tmp_root = args.tmp_dir.expanduser().resolve() if args.tmp_dir else None
                tmp = stack.enter_context(tempfile.TemporaryDirectory(dir=tmp_root, prefix="compress_"))
                input_path = Path(tmp) / out_base
            console.print(f"Archiving folder: {src}")
            skipped = build_tar(src, input_path, base, console)
            for line in skipped:
                console.print(f"[yellow]skipped {line}[/]")
        else:
            input_path = src
        orig = input_path.stat().st_size
        console.print(f"Input   : {input_path}")
        console.print(f"Size    : {orig:,} bytes")
        console.print(
            f"Preset  : {args.preset}   Algorithms: {', '.join(names)}   Workers: {min(args.workers, len(names))}"
        )
        digest: str | None = None
        if args.verify:
            with console.status("Hashing source for verification..."):
                digest = sha256_file(input_path)
        levels = LEVELS[args.preset]
        jobs = [Job(n, input_path, outputs[n], orig, levels[n], digest) for n in names]
        columns = (
            TextColumn("[bold]{task.description:<8}"),
            BarColumn(),
            TaskProgressColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeElapsedColumn(),
            TextColumn("{task.fields[status]}"),
        )
        t0 = time.perf_counter()
        with Progress(*columns, console=console, refresh_per_second=10) as progress:
            results = run_jobs(jobs, min(args.workers, len(jobs)), progress)
        wall = time.perf_counter() - t0
    console.print()
    print_report(console, results, orig, wall)
    if args.json_path:
        payload = {
            "input": str(src),
            "size": orig,
            "preset": args.preset,
            "wall_seconds": wall,
            "results": [asdict(r) for r in results],
        }
        args.json_path.write_text(json.dumps(payload, indent=2))
    return 0 if all(r.error is None for r in results) else 2


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    console = Console()
    try:
        return run(args, console)
    except CliError as exc:
        console.print(f"[red]error:[/] {exc}")
        return 1
    except KeyboardInterrupt:
        console.print("[yellow]interrupted[/]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
"""

_SOURCES['subdirs'] = r"""#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import argparse
import bz2
import fnmatch
import gzip
import lzma
import multiprocessing as mp
import os
import re
import shutil
import stat
import struct
import sys
import tarfile
import tempfile
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from typing import BinaryIO, Callable, Final, Literal, TypeVar, cast

Compressor = Literal["xz", "gz", "bz2", "zstd", "lz4", "snappy"]
Method = Literal["mp-map", "mp-imap", "process-pool", "thread-pool"]
Status = Literal["ok", "skipped", "error"]
Result = tuple[Path, Status, str]
CompressTask = tuple[Path, Compressor, bool, bool, int | None, bool]
DecompressTask = tuple[Path, bool]

T = TypeVar("T")
R = TypeVar("R")

COMPRESSORS: Final[tuple[Compressor, ...]] = ("xz", "gz", "bz2", "zstd", "lz4", "snappy")
EXTENSIONS: Final[dict[Compressor, str]] = {
    "xz": ".tar.xz",
    "gz": ".tar.gz",
    "bz2": ".tar.bz2",
    "zstd": ".tar.zst",
    "lz4": ".tar.lz4",
    "snappy": ".tar.snappy",
}
LEVEL_RANGES: Final[dict[Compressor, tuple[int, int]]] = {
    "xz": (0, 9),
    "gz": (0, 9),
    "bz2": (1, 9),
    "zstd": (-5, 22),
    "lz4": (0, 16),
}
DEFAULT_LEVELS: Final[dict[Compressor, int]] = {
    "xz": 9,
    "gz": 9,
    "bz2": 9,
    "zstd": 20,
    "lz4": 16,
}
SNAPPY_MAGIC: Final[bytes] = b"TARDIRS-SNAPPY\x01"
SNAPPY_CHUNK_SIZE: Final[int] = 1024 * 1024
SNAPPY_MAX_BLOCK_SIZE: Final[int] = 2 * SNAPPY_CHUNK_SIZE


def parse_size(value: str) -> int:
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB|KIB|MIB|GIB|TIB)?\s*",
        value,
        flags=re.IGNORECASE,
    )
    if match is None:
        raise argparse.ArgumentTypeError("Expected a size such as 1024, 500KB, 1.5MB, or 2GiB")

    units: dict[str, int] = {
        "B": 1,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
        "KIB": 1024,
        "MIB": 1024**2,
        "GIB": 1024**3,
        "TIB": 1024**4,
    }
    return int(Decimal(match.group(1)) * units[(match.group(2) or "B").upper()])


def format_size(size: int) -> str:
    amount = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    raise AssertionError("Unreachable")


def directory_size(directory: Path) -> int:
    total = 0
    pending: list[Path] = [directory]

    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size

    return total


def archive_format(archive: Path) -> tuple[Compressor, str]:
    for compressor, extension in EXTENSIONS.items():
        if archive.name.endswith(extension):
            name = archive.name[: -len(extension)]
            if name in ("", ".", ".."):
                raise ValueError(f"Invalid archive name: {archive.name}")
            return compressor, name
    raise ValueError(f"Unsupported archive format: {archive}")


def check_dependencies(compressor: Compressor) -> None:
    if compressor == "zstd":
        try:
            from compression import zstd
        except ImportError:
            import zstandard
    elif compressor == "lz4":
        import lz4.frame
    elif compressor == "snappy":
        from snapy import compress, decompress


def _open_writer(
    path: Path,
    compressor: Compressor,
    level: int | None,
    extreme: bool,
) -> BinaryIO:
    selected_level = DEFAULT_LEVELS.get(compressor, 0) if level is None else level

    if compressor == "xz":
        preset = selected_level | (lzma.PRESET_EXTREME if extreme else 0)
        return cast(BinaryIO, lzma.open(path, "wb", preset=preset))
    if compressor == "gz":
        return cast(BinaryIO, gzip.open(path, "wb", compresslevel=selected_level))
    if compressor == "bz2":
        return cast(BinaryIO, bz2.open(path, "wb", compresslevel=selected_level))
    if compressor == "zstd":
        try:
            from compression import zstd
        except ImportError:
            import zstandard

            return cast(
                BinaryIO,
                zstandard.open(str(path), "wb", level=selected_level),
            )
        return cast(BinaryIO, zstd.open(path, "wb", level=selected_level))
    if compressor == "lz4":
        import lz4.frame

        return cast(
            BinaryIO,
            lz4.frame.open(
                path,
                "wb",
                compression_level=selected_level,
                block_size=lz4.frame.BLOCKSIZE_MAX4MB,
                block_linked=True,
                content_checksum=True,
            ),
        )
    raise ValueError(f"Unsupported streaming compressor: {compressor}")


def _open_reader(archive: Path, compressor: Compressor) -> BinaryIO:
    if compressor == "xz":
        return cast(BinaryIO, lzma.open(archive, "rb"))
    if compressor == "gz":
        return cast(BinaryIO, gzip.open(archive, "rb"))
    if compressor == "bz2":
        return cast(BinaryIO, bz2.open(archive, "rb"))
    if compressor == "zstd":
        try:
            from compression import zstd
        except ImportError:
            import zstandard

            return cast(BinaryIO, zstandard.open(str(archive), "rb"))
        return cast(BinaryIO, zstd.open(archive, "rb"))
    if compressor == "lz4":
        import lz4.frame

        return cast(BinaryIO, lz4.frame.open(archive, "rb"))
    raise ValueError(f"Unsupported streaming compressor: {compressor}")


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    output = bytearray()
    while len(output) < size:
        chunk = stream.read(size - len(output))
        if not chunk:
            raise ValueError("Truncated Snappy archive")
        output.extend(chunk)
    return bytes(output)


def _write_snappy(source: BinaryIO, destination: BinaryIO) -> None:
    from snapy import compress

    destination.write(SNAPPY_MAGIC)
    while block := source.read(SNAPPY_CHUNK_SIZE):
        encoded = bytes(compress(block))
        if not encoded or len(encoded) > SNAPPY_MAX_BLOCK_SIZE:
            raise ValueError("Invalid compressed Snappy block size")
        destination.write(struct.pack(">II", len(encoded), len(block)))
        destination.write(encoded)
    destination.write(struct.pack(">II", 0, 0))


def _read_snappy(source: BinaryIO, destination: BinaryIO) -> None:
    from snapy import decompress

    if _read_exact(source, len(SNAPPY_MAGIC)) != SNAPPY_MAGIC:
        raise ValueError("Not a tardirs chunked Snappy archive")

    while True:
        compressed_size, original_size = struct.unpack(">II", _read_exact(source, 8))
        if compressed_size == 0 and original_size == 0:
            if source.read(1):
                raise ValueError("Unexpected trailing Snappy data")
            return
        if not 0 < compressed_size <= SNAPPY_MAX_BLOCK_SIZE:
            raise ValueError("Invalid compressed Snappy block size")
        if not 0 < original_size <= SNAPPY_CHUNK_SIZE:
            raise ValueError("Invalid uncompressed Snappy block size")

        block = bytes(decompress(_read_exact(source, compressed_size)))
        if len(block) != original_size:
            raise ValueError("Snappy block length mismatch")
        destination.write(block)


def _compress_one(task: CompressTask) -> Result:
    directory, compressor, force, keep, level, extreme = task
    archive = directory.with_name(directory.name + EXTENSIONS[compressor])

    try:
        if os.path.lexists(archive) and not force:
            return archive, "skipped", "Archive already exists"

        with tempfile.TemporaryDirectory(prefix=".tardirs-", dir=directory.parent) as temporary:
            temporary_root = Path(temporary)
            output = temporary_root / archive.name

            if compressor == "snappy":
                with tempfile.TemporaryFile(mode="w+b", dir=temporary_root) as raw:
                    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
                        tar.add(directory, arcname=directory.name)
                    raw.seek(0)
                    with output.open("wb") as destination:
                        _write_snappy(raw, destination)
            else:
                with _open_writer(output, compressor, level, extreme) as writer:
                    with tarfile.open(fileobj=writer, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                        tar.add(directory, arcname=directory.name)

            if force:
                os.replace(output, archive)
            else:
                try:
                    os.link(output, archive)
                except FileExistsError:
                    return archive, "skipped", "Archive created by another task"

        if not keep:
            shutil.rmtree(directory)
        return archive, "ok", "Kept source" if keep else "Removed source"
    except Exception as exc:
        return archive, "error", str(exc)


def _extract_tar(reader: BinaryIO, staging: Path, expected_name: str) -> None:
    def safe_filter(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo | None:
        filtered = tarfile.data_filter(member, destination)
        if filtered is None:
            return None

        parts = Path(filtered.name).parts
        if not parts or parts[0] != expected_name or ".." in parts:
            raise ValueError(f"Archive member is outside {expected_name!r}: {filtered.name}")
        if filtered.issym() or filtered.islnk():
            raise ValueError(f"Archive links are not allowed: {filtered.name}")
        if not (filtered.isfile() or filtered.isdir()):
            raise ValueError(f"Unsupported archive member: {filtered.name}")
        return filtered

    with tarfile.open(fileobj=reader, mode="r|") as tar:
        tar.extractall(path=staging, filter=safe_filter)


def _decompress_one(task: DecompressTask) -> Result:
    archive, keep = task
    try:
        compressor, name = archive_format(archive)
        destination = archive.parent / name
        if os.path.lexists(destination):
            return archive, "skipped", f"Destination already exists: {destination}"

        with tempfile.TemporaryDirectory(prefix=".tardirs-", dir=archive.parent) as temporary:
            staging = Path(temporary)
            if compressor == "snappy":
                with tempfile.TemporaryFile(mode="w+b", dir=staging) as raw:
                    with archive.open("rb") as source:
                        _read_snappy(source, raw)
                    raw.seek(0)
                    _extract_tar(raw, staging, name)
            else:
                with _open_reader(archive, compressor) as reader:
                    _extract_tar(reader, staging, name)

            extracted = staging / name
            if not extracted.is_dir() or extracted.is_symlink():
                raise ValueError(f"Archive must contain a directory named {name!r}")

            destination.mkdir()
            try:
                os.replace(extracted, destination)
            except BaseException:
                destination.rmdir()
                raise

        if not keep:
            archive.unlink()
        return destination, "ok", "Kept archive" if keep else "Removed archive"
    except Exception as exc:
        return archive, "error", str(exc)


def parallel_map(
    method: Method,
    func: Callable[[T], R],
    items: list[T],
    max_workers: int,
) -> list[R]:
    if method == "thread-pool":
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            return list(executor.map(func, items))
    if method == "process-pool":
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            return list(executor.map(func, items))
    if method == "mp-map":
        with mp.Pool(processes=max_workers) as pool:
            return pool.map(func, items)
    if method == "mp-imap":
        with mp.Pool(processes=max_workers) as pool:
            return list(pool.imap_unordered(func, items))
    raise ValueError(f"Unknown execution method: {method}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tardirs",
        description="Compress or decompress top-level directories in parallel.",
        epilog=(
            "Requires Python 3.12+. Sizes use decimal KB/MB or binary KiB/MiB. "
            "Size limits are inclusive and apply only when compressing. "
            "Snappy uses a custom chunked container, not standard Snappy framing; "
            "it uses temporary disk space for the uncompressed tar. "
            "Extraction rejects links and special files. "
            "Do not modify source directories during compression."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-C",
        "--directory",
        type=Path,
        default=Path.cwd(),
        help="Target root directory",
    )
    parser.add_argument(
        "-c",
        "--compressor",
        choices=COMPRESSORS,
        default="lz4",
        help="Compression algorithm",
    )
    parser.add_argument(
        "-d",
        "--decompress",
        action="store_true",
        help="Decompress recognized archives",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=4,
        help="Concurrent workers; 0 selects automatically",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Replace existing archives when compressing; never overwrite extracted directories",
    )
    retention = parser.add_mutually_exclusive_group()
    retention.add_argument(
        "-k",
        "--keep",
        dest="keep",
        action="store_true",
        help="Keep source directories or archives",
    )
    retention.add_argument(
        "--remove-source",
        dest="keep",
        action="store_false",
        help="Remove sources after successful completion",
    )
    parser.set_defaults(keep=True)
    parser.add_argument(
        "--min",
        dest="min_size",
        type=parse_size,
        help="Skip directories smaller than this recursive regular-file size",
    )
    parser.add_argument(
        "--max",
        "-max",
        dest="max_size",
        type=parse_size,
        help="Skip directories larger than this recursive regular-file size",
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="Include matching top-level names; repeatable",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="Exclude matching top-level names; repeatable",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Show selected targets without changing files",
    )
    parser.add_argument(
        "--level",
        type=int,
        help="Compression level: xz 0–9, gz 0–9, bz2 1–9, zstd -5–22, lz4 0–16",
    )
    parser.add_argument(
        "--extreme",
        action="store_true",
        help="Enable the slower xz extreme preset",
    )
    parser.add_argument(
        "--mp-method",
        choices=("mp-map", "mp-imap", "process-pool", "thread-pool"),
        default="process-pool",
        help="Parallel execution backend",
    )
    ns = parser.parse_args()

    if sys.version_info < (3, 12):
        parser.error("Python 3.12 or later is required")
    if ns.jobs < 0:
        parser.error("--jobs must be nonnegative")
    if ns.min_size is not None and ns.max_size is not None:
        if ns.min_size > ns.max_size:
            parser.error("--min cannot exceed --max")
    if ns.decompress and (
        ns.min_size is not None or ns.max_size is not None or ns.level is not None or ns.extreme or ns.force
    ):
        parser.error("--min, --max, --level, --extreme, and --force are compression-only")

    compressor = cast(Compressor, ns.compressor)
    if ns.level is not None:
        if compressor not in LEVEL_RANGES:
            parser.error("Snappy does not support --level")
        low, high = LEVEL_RANGES[compressor]
        if not low <= ns.level <= high:
            parser.error(f"--level for {compressor} must be between {low} and {high}")
    if ns.extreme and compressor != "xz":
        parser.error("--extreme requires --compressor xz")

    root = cast(Path, ns.directory).resolve()
    if not root.is_dir():
        parser.error(f"Not a directory: {root}")

    targets: list[Path] = []
    scan_errors = 0
    destinations: set[str] = set()

    try:
        entries = sorted(root.iterdir())
        for path in entries:
            if path.is_symlink():
                continue
            if ns.decompress:
                if not path.is_file() or not path.name.endswith(tuple(EXTENSIONS.values())):
                    continue
            elif not path.is_dir():
                continue

            if ns.include and not any(fnmatch.fnmatchcase(path.name, pattern) for pattern in ns.include):
                continue
            if any(fnmatch.fnmatchcase(path.name, pattern) for pattern in ns.exclude):
                continue

            if ns.decompress:
                _, name = archive_format(path)
                if name in destinations:
                    parser.error(f"Multiple selected archives target {name!r}")
                destinations.add(name)
            elif ns.min_size is not None or ns.max_size is not None:
                try:
                    size = directory_size(path)
                except OSError as exc:
                    print(f"error: {path}: {exc}", file=sys.stderr)
                    scan_errors += 1
                    continue
                if ns.min_size is not None and size < ns.min_size:
                    print(f"skipped: {path}: {format_size(size)} below minimum")
                    continue
                if ns.max_size is not None and size > ns.max_size:
                    print(f"skipped: {path}: {format_size(size)} above maximum")
                    continue

            targets.append(path)
    except OSError as exc:
        parser.error(str(exc))

    if not targets:
        print("No matching targets.")
        raise SystemExit(1 if scan_errors else 0)

    if ns.dry_run:
        for path in targets:
            if ns.decompress:
                _, name = archive_format(path)
                destination = path.parent / name
            else:
                destination = path.with_name(path.name + EXTENSIONS[compressor])
            print(f"{path} -> {destination}")
        raise SystemExit(1 if scan_errors else 0)

    required: set[Compressor] = {archive_format(path)[0] for path in targets} if ns.decompress else {compressor}
    for algorithm in sorted(required):
        try:
            check_dependencies(algorithm)
        except ImportError as exc:
            parser.error(f"Missing dependency for {algorithm}: {exc}")

    workers = min(ns.jobs or (os.cpu_count() or 1), len(targets))
    method = cast(Method, ns.mp_method)
    action = "Decompressing" if ns.decompress else "Compressing"
    print(f"{action} {len(targets)} target(s), {workers} worker(s), {method}")

    results: list[Result]
    if ns.decompress:
        decompress_tasks: list[DecompressTask] = [(path, bool(ns.keep)) for path in targets]
        results = parallel_map(method, _decompress_one, decompress_tasks, workers)
    else:
        compress_tasks: list[CompressTask] = [
            (
                path,
                compressor,
                bool(ns.force),
                bool(ns.keep),
                ns.level,
                bool(ns.extreme),
            )
            for path in targets
        ]
        results = parallel_map(method, _compress_one, compress_tasks, workers)

    counts: dict[Status, int] = {"ok": 0, "skipped": 0, "error": scan_errors}
    for path, status, message in results:
        counts[status] += 1
        stream = sys.stderr if status == "error" else sys.stdout
        print(f"{status}: {path}: {message}", file=stream)

    print(f"Completed: {counts['ok']} succeeded, {counts['skipped']} skipped, {counts['error']} failed")
    raise SystemExit(1 if counts["error"] else 0)


if __name__ == "__main__":
    mp.freeze_support()
    main()
"""

_SOURCES['bench'] = r"""#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import bz2
import gzip
import lzma
import shutil
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Callable, Optional

try:
    import brotli
except ImportError:
    brotli = None
try:
    import zstandard as zstd
except ImportError:
    zstd = None
try:
    import lz4.frame as lz4frame
except ImportError:
    lz4frame = None
try:
    import py7zr
except ImportError:
    py7zr = None
try:
    import pylzma
except ImportError:
    pylzma = None
try:
    import cramjam
except ImportError:
    cramjam = None
try:
    import zopfli.gzip as zopfli_gzip
except ImportError:
    zopfli_gzip = None
try:
    import pyppmd
except ImportError:
    pyppmd = None
try:
    import pyzstd
except ImportError:
    pyzstd = None
try:
    import zipfile_deflate64
except ImportError:
    zipfile_deflate64 = None
try:
    import pyzipper
except ImportError:
    pyzipper = None
Compressor = Callable[[Path, Path, Optional[int]], None]


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


def make_tar(src_dir: Path, tar_path: Path) -> None:
    with tarfile.open(tar_path, "w") as tar:
        tar.add(src_dir, arcname=src_dir.name)


def _read_all(src: Path) -> bytes:
    with open(src, "rb") as f:
        return f.read()


def _write_all(dst: Path, data: bytes) -> None:
    with open(dst, "wb") as f:
        f.write(data)


def compress_gzip(src: Path, dst: Path, level: Optional[int]) -> None:
    lv = 9 if level is None else level
    with open(src, "rb") as fin, gzip.open(dst, "wb", compresslevel=lv) as fout:
        shutil.copyfileobj(fin, fout, 1024 * 1024)


def compress_bz2(src: Path, dst: Path, level: Optional[int]) -> None:
    lv = 9 if level is None else level
    with open(src, "rb") as fin, bz2.open(dst, "wb", compresslevel=lv) as fout:
        shutil.copyfileobj(fin, fout, 1024 * 1024)


def compress_lzma(src: Path, dst: Path, level: Optional[int]) -> None:
    preset = 6 if level is None else level
    with open(src, "rb") as fin, lzma.open(dst, "wb", preset=preset) as fout:
        shutil.copyfileobj(fin, fout, 1024 * 1024)


def compress_zip(src: Path, dst: Path, level: Optional[int]) -> None:
    lv = 9 if level is None else level
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=lv) as zf:
        zf.write(src, arcname=src.name)


def compress_brotli(src: Path, dst: Path, level: Optional[int]) -> None:
    if brotli is None:
        msg = "brotli not installed"
        raise RuntimeError(msg)
    q = 11 if level is None else level
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        comp = brotli.Compressor(quality=q)
        while True:
            chunk = fin.read(1024 * 1024)
            if not chunk:
                break
            fout.write(comp.process(chunk))
        fout.write(comp.finish())


def compress_zstd(src: Path, dst: Path, level: Optional[int]) -> None:
    if zstd is None:
        msg = "zstandard not installed"
        raise RuntimeError(msg)
    lv = 9 if level is None else level
    cctx = zstd.ZstdCompressor(level=lv)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        cctx.copy_stream(fin, fout)


def compress_lz4(src: Path, dst: Path, level: Optional[int]) -> None:
    if lz4frame is None:
        msg = "lz4 not installed"
        raise RuntimeError(msg)
    lv = 9 if level is None else level
    with open(src, "rb") as fin, lz4frame.open(dst, "wb", compression_level=lv) as fout:
        shutil.copyfileobj(fin, fout, 1024 * 1024)


def compress_7z(src: Path, dst: Path, level: Optional[int]) -> None:
    if py7zr is None:
        msg = "py7zr not installed"
        raise RuntimeError(msg)
    preset = 7 if level is None else level
    filters = [{"id": py7zr.FILTER_LZMA2, "preset": preset}]
    with py7zr.SevenZipFile(dst, "w", filters=filters) as zf:
        zf.write(src, arcname=src.name)


def compress_pylzma(src: Path, dst: Path, level: Optional[int]) -> None:
    if pylzma is None:
        msg = "pylzma not installed"
        raise RuntimeError(msg)
    lv = 9 if level is None else level
    data = _read_all(src)
    out = pylzma.compress(data, filters=[{"id": pylzma.FILTER_LZMA1, "preset": lv}])
    _write_all(dst, out)


def compress_snappy(src: Path, dst: Path, _level: Optional[int]) -> None:
    if cramjam is None:
        msg = "cramjam not installed"
        raise RuntimeError(msg)
    data = _read_all(src)
    out = cramjam.snappy.compress(data)
    _write_all(dst, bytes(out))


def compress_zopfli_gzip(src: Path, dst: Path, level: Optional[int]) -> None:
    if zopfli_gzip is None:
        msg = "zopfli not installed"
        raise RuntimeError(msg)
    iters = 15 if level is None else level
    data = _read_all(src)
    out = zopfli_gzip.compress(data, numiterations=iters)
    _write_all(dst, out)


def compress_ppmd(src: Path, dst: Path, level: Optional[int]) -> None:
    if pyppmd is None:
        msg = "pyppmd not installed"
        raise RuntimeError(msg)
    order = 6 if level is None else level
    data = _read_all(src)
    out = pyppmd.compress(data, max_order=order, mem_size=64 * 1024 * 1024)
    _write_all(dst, out)


def compress_pyzstd(src: Path, dst: Path, level: Optional[int]) -> None:
    if pyzstd is None:
        msg = "pyzstd not installed"
        raise RuntimeError(msg)
    lv = 9 if level is None else level
    data = _read_all(src)
    out = pyzstd.compress(data, level_or_option=lv)
    _write_all(dst, out)


def compress_zip_deflate64(src: Path, dst: Path, _level: Optional[int]) -> None:
    if zipfile_deflate64 is None:
        msg = "zipfile-deflate64 not installed"
        raise RuntimeError(msg)
    with zipfile_deflate64.ZipFile(dst, "w") as zf:
        zf.write(src, arcname=src.name)


def compress_zip_lzma(src: Path, dst: Path, _level: Optional[int]) -> None:
    if pyzipper is None:
        msg = "pyzipper not installed"
        raise RuntimeError(msg)
    with pyzipper.AESZipFile(dst, "w", compression=pyzipper.ZIP_LZMA) as zf:
        zf.write(src, arcname=src.name)


def compress_zip_bzip2(src: Path, dst: Path, _level: Optional[int]) -> None:
    if pyzipper is None:
        msg = "pyzipper not installed"
        raise RuntimeError(msg)
    with pyzipper.AESZipFile(dst, "w", compression=pyzipper.ZIP_BZIP2) as zf:
        zf.write(src, arcname=src.name)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <file_or_folder>")
        return 1
    input_path = Path(sys.argv[1]).expanduser().resolve()
    if not input_path.exists():
        print(f"Error: path does not exist: {input_path}")
        return 1
    tmpdir = Path(tempfile.mkdtemp(prefix="compress_bench_"))
    print(f"Working dir: {tmpdir}\n")
    try:
        if input_path.is_dir():
            print(f"Input is a folder -> creating tar of {input_path.name} ...")
            src_file = tmpdir / f"{input_path.name}.tar"
            make_tar(input_path, src_file)
            print(f"Tar created: {src_file} ({human(src_file.stat().st_size)})\n")
        else:
            src_file = input_path
        original_size = src_file.stat().st_size
        print(f"Source file  : {src_file}")
        print(f"Original size: {human(original_size)} ({original_size} bytes)\n")
        print("Testing compressors...")
        print("-" * 72)
        jobs: list[tuple[str, str, Compressor, list[Optional[int]]]] = [
            ("gzip", ".gz", compress_gzip, [1, 6, 9]),
            ("bz2", ".bz2", compress_bz2, [1, 5, 9]),
            ("xz", ".xz", compress_lzma, [0, 3, 6, 9]),
            ("zip", ".zip", compress_zip, [1, 6, 9]),
        ]
        if brotli is not None:
            jobs.append(("brotli", ".br", compress_brotli, [1, 4, 7, 9, 11]))
        if zstd is not None:
            jobs.append(("zstandard", ".zst", compress_zstd, [1, 3, 9, 15, 19, 22]))
        if lz4frame is not None:
            jobs.append(("lz4", ".lz4", compress_lz4, [0, 3, 9, 16]))
        if py7zr is not None:
            jobs.append(("7z", ".7z", compress_7z, [1, 5, 9]))
        if pylzma is not None:
            jobs.append(("pylzma", ".lzma", compress_pylzma, [1, 5, 9]))
        if cramjam is not None:
            jobs.append(("snappy", ".snappy", compress_snappy, [None]))
        if zopfli_gzip is not None:
            jobs.append(("zopfli.gz", ".gz", compress_zopfli_gzip, [5, 15, 30]))
        if pyppmd is not None:
            jobs.append(("ppmd", ".ppmd", compress_ppmd, [4, 6, 8, 16]))
        if pyzstd is not None:
            jobs.append(("pyzstd", ".zst", compress_pyzstd, [1, 3, 9, 15, 19, 22]))
        if zipfile_deflate64 is not None:
            jobs.append(("zip-deflate64", ".zip", compress_zip_deflate64, [None]))
        if pyzipper is not None:
            jobs.append(("zip-lzma", ".zip", compress_zip_lzma, [None]))
            jobs.append(("zip-bzip2", ".zip", compress_zip_bzip2, [None]))
        results: list[tuple[str, Optional[int], int, float, float, Path]] = []
        for name, ext, func, levels in jobs:
            for level in levels:
                tag = "" if level is None else f" level={level}"
                dst = tmpdir / f"{name}_{level if level is not None else 'default'}{ext}"
                try:
                    t0 = time.perf_counter()
                    func(src_file, dst, level)
                    dt = time.perf_counter() - t0
                    size = dst.stat().st_size
                    ratio = size / original_size if original_size else 0.0
                    results.append((name, level, size, ratio, dt, dst))
                    print(f"  {name:<14}{tag:<12} -> {human(size):>12}  ratio={ratio:.4f}  time={dt:.2f}s")
                except Exception as e:
                    print(f"  {name:<14}{tag:<12} -> FAILED ({e.__class__.__name__}: {e})")
        if not results:
            print("\nNo compressor succeeded.")
            return 2
        results.sort(key=lambda r: r[2])
        print("\n" + "=" * 72)
        print("BEST 3 METHODS (smallest first)")
        print("=" * 72)
        for rank, (name, level, size, ratio, dt, _) in enumerate(results[:3], 1):
            tag = "" if level is None else f" (level={level})"
            saved = 100 * (1 - ratio)
            print(f"  #{rank}: {name}{tag}")
            print(f"       size  : {human(size)} ({size} bytes)")
            print(f"       ratio : {ratio:.4f}  (saved {saved:.2f}%)")
            print(f"       time  : {dt:.2f}s")
        best = results[0]
        best_path = best[5]
        dest = Path.cwd() / best_path.name
        i = 1
        while dest.exists():
            dest = Path.cwd() / f"{best_path.stem}_{i}{best_path.suffix}"
            i += 1
        shutil.copy2(best_path, dest)
        print(f"\nBest compressed file copied to: {dest}")
        print("Temp directory will now be removed.")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""

_SOURCES['sniff'] = r'''#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import bz2
import contextlib
import gzip
import lzma
import pickle
import sys
import tarfile
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

try:
    import brotli as _brotli_mod
except ImportError:
    _brotli_mod = None
try:
    import zstandard as _zstd_mod
except ImportError:
    _zstd_mod = None
try:
    import py7zr as _py7zr_mod
except ImportError:
    _py7zr_mod = None
try:
    import cramjam as _cramjam_mod
except ImportError:
    _cramjam_mod = None
try:
    import pylzma as _pylzma_mod
except ImportError:
    _pylzma_mod = None
try:
    import rarfile as _rarfile_mod
except ImportError:
    _rarfile_mod = None
try:
    import libarchive as _libarchive_mod
except ImportError:
    _libarchive_mod = None
try:
    import pycdlib as _pycdlib_mod
except ImportError:
    _pycdlib_mod = None
try:
    import cabarchive as _cabarchive_mod
except ImportError:
    _cabarchive_mod = None
try:
    import acefile as _acefile_mod
except ImportError:
    _acefile_mod = None
try:
    import pyppmd as _pyppmd_mod
except ImportError:
    _pyppmd_mod = None
try:
    import bz3 as _bz3_mod
except ImportError:
    _bz3_mod = None
try:
    import lzo as _lzo_mod
except ImportError:
    _lzo_mod = None


@dataclass
class Detection:
    name: str
    kind: str
    mime: str = "application/octet-stream"
    confidence: str = "medium"


_MAGIC_TABLE: list[tuple[bytes, int, Detection]] = [
    (b"PK\x03\x04", 0, Detection("zip", "archive", "application/zip", "high")),
    (b"PK\x05\x06", 0, Detection("zip (empty)", "archive", "application/zip", "high")),
    (b"PK\x07\x08", 0, Detection("zip (split)", "archive", "application/zip", "high")),
    (
        b"7z\xbc\xaf\x27\x1c",
        0,
        Detection("7z", "archive", "application/x-7z-compressed", "high"),
    ),
    (
        b"Rar!\x1a\x07\x00",
        0,
        Detection("rar4", "archive", "application/vnd.rar", "high"),
    ),
    (
        b"Rar!\x1a\x07\x01\x00",
        0,
        Detection("rar5", "archive", "application/vnd.rar", "high"),
    ),
    (
        b"MSCF",
        0,
        Detection("cab", "archive", "application/vnd.ms-cab-compressed", "high"),
    ),
    (
        b"**ACE**",
        0,
        Detection("ace", "archive", "application/x-ace-compressed", "high"),
    ),
    (b"!<arch>\n", 0, Detection("ar/deb", "archive", "application/x-archive", "high")),
    (b"\xed\xab\xee\xdb", 0, Detection("rpm", "archive", "application/x-rpm", "high")),
    (b"070701", 0, Detection("cpio (newc)", "archive", "application/x-cpio", "high")),
    (b"070702", 0, Detection("cpio (crc)", "archive", "application/x-cpio", "high")),
    (b"070707", 0, Detection("cpio (odc)", "archive", "application/x-cpio", "high")),
    (b"xar!", 0, Detection("xar", "archive", "application/x-xar", "high")),
    (b"ustar", 257, Detection("tar", "archive", "application/x-tar", "high")),
    (
        b"CD001",
        0x8001,
        Detection("iso9660", "archive", "application/x-iso9660-image", "high"),
    ),
    (b"\x1f\x8b", 0, Detection("gzip", "stream", "application/gzip", "high")),
    (
        b"\x1f\x9d",
        0,
        Detection("compress (.Z)", "stream", "application/x-compress", "high"),
    ),
    (b"BZh", 0, Detection("bzip2", "stream", "application/x-bzip2", "high")),
    (b"\xfd7zXZ\x00", 0, Detection("xz", "stream", "application/x-xz", "high")),
    (b"\x28\xb5\x2f\xfd", 0, Detection("zstd", "stream", "application/zstd", "high")),
    (
        b"\x04\x22\x4d\x18",
        0,
        Detection("lz4 frame", "stream", "application/x-lz4", "high"),
    ),
    (
        b"\x02\x21\x4c\x18",
        0,
        Detection("lz4 legacy", "stream", "application/x-lz4", "medium"),
    ),
    (b"BZ3v1", 0, Detection("bzip3", "stream", "application/x-bzip3", "high")),
    (
        b"\xff\x06\x00\x00sNaPpY",
        0,
        Detection("snappy (framed)", "stream", "application/x-snappy-framed", "high"),
    ),
    (
        b"\x80\x04",
        0,
        Detection("pickle proto 4", "serialized", "application/x-python-pickle", "low"),
    ),
    (
        b"\x80\x05",
        0,
        Detection("pickle proto 5", "serialized", "application/x-python-pickle", "low"),
    ),
]


def sniff_magic(data: bytes) -> Detection:
    best: Detection | None = None
    for sig, off, det in _MAGIC_TABLE:
        if len(data) < off + len(sig):
            continue
        if data[off : off + len(sig)] != sig:
            continue
        if best is None or (det.confidence == "high" and best.confidence != "high"):
            best = det
            if det.confidence == "high":
                break
    return best or Detection("unknown", "unknown", "application/octet-stream", "low")


_RECOGNIZED: list[tuple[str, Callable[[Path], None]]] = []


def _register(name: str, extractor: Callable[[Path], None]) -> None:
    _RECOGNIZED.append((name, extractor))


def _safe_member_path(base: Path, member: str) -> Path | None:
    try:
        p = (base / member).resolve()
        p.relative_to(base.resolve())
    except (ValueError, OSError):
        return None
    return p


def try_libarchive(filename: str) -> bool:
    libarchive = _libarchive_mod
    if libarchive is None:
        print("  SKIP: libarchive-c not installed.\n")
        return False
    print("Trying libarchive (catch-all)...")
    try:
        with libarchive.file_reader(filename) as arc:
            entries = list(arc)
        if not entries:
            print("  FAILED: libarchive yielded no entries.\n")
            return False
        first = entries[0].pathname
        fmt = getattr(entries[0], "archive_format", None) or "?"
        print(f"  SUCCESS: libarchive opened {len(entries)} entries [format={fmt}]. First: {first}\n")
        _register("libarchive", extract_libarchive)
        return True
    except Exception as exc:
        print(f"  FAILED: libarchive raised {type(exc).__name__}: {exc}\n")
        return False


def _build_decompressors() -> dict[str, Callable[[bytes], bytes]]:
    methods: dict[str, Callable[[bytes], bytes]] = {
        "zlib": zlib.decompress,
        "raw-deflate": lambda d: zlib.decompress(d, -15),
        "bz2": bz2.decompress,
        "gzip": gzip.decompress,
        "lzma": lzma.decompress,
    }
    brotli = _brotli_mod
    if brotli is not None:
        methods["brotli"] = brotli.decompress
    zstd = _zstd_mod
    if zstd is not None:

        def _zstd_dec(data: bytes) -> bytes:
            return zstd.ZstdDecompressor().decompress(data)

        methods["zstandard"] = _zstd_dec
    cramjam = _cramjam_mod
    if cramjam is not None:
        snappy = cramjam.snappy
        lz4 = cramjam.lz4
        cj_zstd = cramjam.zstd
        cj_brotli = cramjam.brotli
        cj_bzip2 = cramjam.bzip2
        methods["snappy (framed)"] = lambda d: bytes(snappy.decompress(d))
        methods["snappy (raw)"] = lambda d: bytes(snappy.decompress_raw(d))
        methods["lz4 (frame)"] = lambda d: bytes(lz4.decompress(d))
        methods["lz4 (block)"] = lambda d: bytes(lz4.decompress_block(d))
        methods["cramjam-zstd"] = lambda d: bytes(cj_zstd.decompress(d))
        methods["cramjam-brotli"] = lambda d: bytes(cj_brotli.decompress(d))
        methods["cramjam-bzip2"] = lambda d: bytes(cj_bzip2.decompress(d))
    pylzma = _pylzma_mod
    if pylzma is not None:

        def _pylzma_dec(data: bytes) -> bytes:
            out = pylzma.decompress(data)
            return out if isinstance(out, (bytes, bytearray)) else bytes(out)

        methods["pylzma (raw lzma/lzma2)"] = _pylzma_dec
    pyppmd = _pyppmd_mod
    if pyppmd is not None:

        def _ppmd_dec(data: bytes) -> bytes:
            for order, mem in ((6, 16 << 20), (8, 32 << 20), (16, 64 << 20)):
                try:
                    return pyppmd.Ppmd7Decoder(order, mem).decode(data, 64 << 20)
                except Exception:
                    continue
            msg = "no PPMd7 variant matched"
            raise ValueError(msg)

        methods["ppmd7 (raw)"] = _ppmd_dec
    bz3 = _bz3_mod
    if bz3 is not None:
        methods["bzip3"] = bz3.decompress
    lzo = _lzo_mod
    if lzo is not None:
        methods["lzo"] = lambda d: lzo.decompress(d, False, len(d) * 20)
    return methods


def _hint_boost_order(
    methods: dict[str, Callable[[bytes], bytes]],
    hint: Detection,
) -> dict[str, Callable[[bytes], bytes]]:
    if hint.kind == "unknown":
        return methods
    aliases = {
        "gzip": "gzip",
        "bzip2": "bz2",
        "xz": "lzma",
        "zstd": "zstandard",
        "lz4 frame": "lz4",
        "lz4 legacy": "lz4",
        "snappy (framed)": "snappy",
        "bzip3": "bzip3",
        "compress (.Z)": None,
        "pickle proto 4": None,
        "pickle proto 5": None,
    }
    key = aliases.get(hint.name)
    if not key:
        return methods
    preferred = {k: v for k, v in methods.items() if key in k.lower()}
    rest = {k: v for k, v in methods.items() if k not in preferred}
    return {**preferred, **rest}


def try_stream_decompressors(data: bytes, hint: Detection) -> bool:
    methods = _hint_boost_order(_build_decompressors(), hint)
    success = False
    for name, func in methods.items():
        try:
            print(f"Trying {name}...")
            out = func(data)
            if out and len(out) < len(data) * 10:
                print(f"  SUCCESS: {name} decoded {len(out)} bytes.\n")
                _register(
                    name,
                    (lambda p, blob=out, n=name: extract_stream(n, blob, p.name)),
                )
                success = True
            else:
                print(f"  FAILED: {name} produced {len(out or b'')} bytes (suspicious ratio).\n")
        except Exception as exc:
            print(f"  FAILED: {name}: {type(exc).__name__}: {exc}\n")
    return success


def try_tarfile(filename: str) -> bool:
    if not tarfile.is_tarfile(filename):
        return False
    try:
        print("Trying tarfile (stdlib)...")
        with tarfile.open(filename, "r") as tar:
            members = tar.getmembers()
        if not members:
            print("  FAILED: tar is empty.\n")
            return False
        print(f"  SUCCESS: tar with {len(members)} members. First: {members[0].name}\n")
        _register("tar", extract_tar)
        return True
    except Exception as exc:
        print(f"  FAILED: tarfile: {type(exc).__name__}: {exc}\n")
        return False


def try_zipfile(filename: str) -> bool:
    if not zipfile.is_zipfile(filename):
        return False
    try:
        print("Trying zipfile (stdlib)...")
        with zipfile.ZipFile(filename, "r") as zf:
            names = zf.namelist()
        if not names:
            print("  FAILED: zip is empty.\n")
            return False
        print(f"  SUCCESS: zip with {len(names)} files. First: {names[0]}\n")
        _register("zip", extract_zip)
        return True
    except Exception as exc:
        print(f"  FAILED: zipfile: {type(exc).__name__}: {exc}\n")
        return False


def try_py7zr(filename: str) -> bool:
    py7zr = _py7zr_mod
    if py7zr is None:
        return False
    try:
        print("Trying py7zr (7z)...")
        with py7zr.SevenZipFile(filename, mode="r") as z:
            names = z.getnames()
        if not names:
            print("  FAILED: 7z archive is empty.\n")
            return False
        print(f"  SUCCESS: 7z with {len(names)} files. First: {names[0]}\n")
        _register("7z", extract_7z)
        return True
    except Exception as exc:
        print(f"  FAILED: py7zr: {type(exc).__name__}: {exc}\n")
        return False


def try_rarfile(filename: str) -> bool:
    rarfile = _rarfile_mod
    if rarfile is None:
        return False
    if not rarfile.is_rarfile(filename):
        return False
    try:
        print("Trying rarfile...")
        with rarfile.RarFile(filename) as rf:
            names = rf.namelist()
        if not names:
            print("  FAILED: rar is empty.\n")
            return False
        print(f"  SUCCESS: rar with {len(names)} files. First: {names[0]}\n")
        _register("rar", extract_rar)
        return True
    except Exception as exc:
        print(f"  FAILED: rarfile: {type(exc).__name__}: {exc}\n")
        return False


def try_acefile(filename: str) -> bool:
    acefile = _acefile_mod
    if acefile is None:
        return False
    try:
        print("Trying acefile...")
        with acefile.open(filename) as af:
            names = [m.filename for m in af.members()]
        if not names:
            print("  FAILED: ace is empty.\n")
            return False
        print(f"  SUCCESS: ace with {len(names)} files. First: {names[0]}\n")
        _register("ace", extract_ace)
        return True
    except Exception as exc:
        print(f"  FAILED: acefile: {type(exc).__name__}: {exc}\n")
        return False


def try_cabarchive(filename: str) -> bool:
    cabarchive = _cabarchive_mod
    if cabarchive is None:
        return False
    try:
        print("Trying cabarchive...")
        with open(filename, "rb") as fp:
            cf = cabarchive.CabArchive(fp)
            names = [f.filename for f in cf.files]
        if not names:
            print("  FAILED: cab is empty.\n")
            return False
        print(f"  SUCCESS: cab with {len(names)} files. First: {names[0]}\n")
        _register("cab", extract_cab)
        return True
    except Exception as exc:
        print(f"  FAILED: cabarchive: {type(exc).__name__}: {exc}\n")
        return False


def try_pycdlib(filename: str) -> bool:
    pycdlib = _pycdlib_mod
    if pycdlib is None:
        return False
    iso = pycdlib.PyCdlib()
    try:
        print("Trying pycdlib (ISO/UDF)...")
        iso.open(filename)
        count, first = 0, None
        for child in iso.list_children(iso_path="/"):
            if child is None:
                continue
            name = child.file_identifier().decode(errors="replace")
            if name in (".", ".."):
                continue
            if first is None:
                first = name
            count += 1
        if not count:
            print("  FAILED: ISO has no root entries.\n")
            return False
        print(f"  SUCCESS: ISO/UDF with {count} root entries. First: {first}\n")
        _register("iso", extract_iso)
        return True
    except Exception as exc:
        print(f"  FAILED: pycdlib: {type(exc).__name__}: {exc}\n")
        return False
    finally:
        with contextlib.suppress(Exception):
            iso.close()


def try_pickle(data: bytes) -> bool:
    if not (data.startswith((b"\x80\x04", b"\x80\x05", b"c", b"("))):
        return False
    try:
        print("Trying pickle (WARNING: unsafe on untrusted input)...")
        obj = pickle.loads(data)
        print(f"  SUCCESS: pickle decoded → {type(obj).__name__}\n")
        _register("pickle", extract_pickle)
        return True
    except Exception as exc:
        print(f"  FAILED: pickle: {type(exc).__name__}: {exc}\n")
        return False


def extract_tar(src: Path) -> None:
    with tarfile.open(src, "r") as tar:
        for m in tar.getmembers():
            if _safe_member_path(Path.cwd(), m.name) is None:
                print(f"    SKIP (unsafe path): {m.name}")
                continue
            tar.extract(m, path=Path.cwd())


def extract_zip(src: Path) -> None:
    with zipfile.ZipFile(src, "r") as zf:
        for info in zf.infolist():
            if _safe_member_path(Path.cwd(), info.filename) is None:
                print(f"    SKIP (unsafe path): {info.filename}")
                continue
            zf.extract(info, path=Path.cwd())


def extract_7z(src: Path) -> None:
    py7zr = _py7zr_mod
    if py7zr is None:
        return
    with py7zr.SevenZipFile(src, mode="r") as z:
        unsafe = [n for n in z.getnames() if _safe_member_path(Path.cwd(), n) is None]
        if unsafe:
            print(f"    SKIP unsafe members: {unsafe[:3]}{' …' if len(unsafe) > 3 else ''}")
        z.extractall(path=Path.cwd())


def extract_rar(src: Path) -> None:
    rarfile = _rarfile_mod
    if rarfile is None:
        return
    with rarfile.RarFile(src) as rf:
        for info in rf.infolist():
            if _safe_member_path(Path.cwd(), info.filename) is None:
                print(f"    SKIP (unsafe path): {info.filename}")
                continue
            rf.extract(info, path=Path.cwd())


def extract_ace(src: Path) -> None:
    acefile = _acefile_mod
    if acefile is None:
        return
    with acefile.open(src) as af:
        for member in af.members():
            target = _safe_member_path(Path.cwd(), member.filename)
            if target is None:
                print(f"    SKIP (unsafe path): {member.filename}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as out:
                af.extract(member, out)


def extract_cab(src: Path) -> None:
    cabarchive = _cabarchive_mod
    if cabarchive is None:
        return
    with open(src, "rb") as fp:
        cf = cabarchive.CabArchive(fp)
        for f in cf.files:
            target = _safe_member_path(Path.cwd(), f.filename)
            if target is None:
                print(f"    SKIP (unsafe path): {f.filename}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f.read())


def extract_iso(src: Path) -> None:
    pycdlib = _pycdlib_mod
    if pycdlib is None:
        return
    iso = pycdlib.PyCdlib()
    iso.open(str(src))
    try:

        def _walk(path: str) -> None:
            for child in iso.list_children(iso_path=path):
                if child is None:
                    continue
                name = child.file_identifier().decode(errors="replace")
                if name in (".", ".."):
                    continue
                clean = name.split(";", 1)[0]
                child_path = f"{path.rstrip('/')}/{name}"
                target = _safe_member_path(Path.cwd(), clean)
                if target is None:
                    print(f"    SKIP (unsafe path): {clean}")
                    continue
                if child.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    _walk(child_path + "/")
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with open(target, "wb") as out:
                        iso.get_file_from_iso_fp(out, iso_path=child_path)

        _walk("/")
    finally:
        iso.close()


def extract_libarchive(src: Path) -> None:
    libarchive = _libarchive_mod
    if libarchive is None:
        return
    with libarchive.file_reader(str(src)) as arc:
        for entry in arc:
            target = _safe_member_path(Path.cwd(), entry.pathname)
            if target is None:
                print(f"    SKIP (unsafe path): {entry.pathname}")
                continue
            if entry.isdir:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as out:
                out.writelines(entry.get_blocks())


def extract_stream(name: str, blob: bytes, src_name: str) -> None:
    suffixes = {
        "gzip": ".gz",
        "bz2": ".bz2",
        "lzma": ".xz",
        "brotli": ".br",
        "zstandard": ".zst",
        "snappy (framed)": ".sz",
        "snappy (raw)": ".sz",
        "lz4 (frame)": ".lz4",
        "lz4 (block)": ".lz4",
        "bzip3": ".bz3",
        "lzo": ".lzo",
        "raw-deflate": ".zz",
        "zlib": ".zz",
    }
    ext = suffixes.get(name)
    if ext and src_name.endswith(ext):
        stem = src_name[: -len(ext)]
    else:
        stem = src_name + ".out"
    target = Path.cwd() / stem
    target.write_bytes(blob)
    print(f"    WROTE: {target.name} ({len(blob)} bytes)")


def extract_pickle(src: Path) -> None:
    obj = pickle.loads(src.read_bytes())
    out = Path.cwd() / (src.name + ".repr.txt")
    out.write_text(repr(obj), encoding="utf-8")
    print(f"    WROTE: {out.name} (repr of {type(obj).__name__})")


def run_extraction(src: Path) -> None:
    if not _RECOGNIZED:
        print("Nothing recognized — no extraction performed.\n")
        return
    print(f"═══ Extracting to: {Path.cwd()} ═══\n")
    for name, extractor in _RECOGNIZED:
        print(f"→ {name}")
        try:
            extractor(src)
            print(f"  DONE: {name}\n")
        except Exception as exc:
            print(f"  ERROR: {name}: {type(exc).__name__}: {exc}\n")
    _RECOGNIZED.clear()


def process(filename: str) -> bool:
    print(f"═══ Analyzing: {filename} ═══\n")
    _RECOGNIZED.clear()
    try:
        data = Path(filename).read_bytes()
    except FileNotFoundError:
        print(f"Error: file not found: {filename}\n")
        return False
    except Exception as exc:
        print(f"Error reading {filename}: {exc}\n")
        return False
    print(f"File size: {len(data)} bytes\n")
    hint = sniff_magic(data)
    print(f"[STAGE 1] Magic sniff → {hint.name} (kind={hint.kind}, mime={hint.mime}, confidence={hint.confidence})\n")
    success = False
    print("[STAGE 2] libarchive catch-all")
    if hint.kind == "stream":
        print(f"  SKIP: magic says {hint.name} is a raw stream, not a container.\n")
    elif hint.kind == "serialized":
        print("  SKIP: magic says this is a serialized object.\n")
    else:
        if try_libarchive(filename):
            success = True
        success |= try_tarfile(filename)
        success |= try_zipfile(filename)
        success |= try_py7zr(filename)
        success |= try_rarfile(filename)
        success |= try_acefile(filename)
        success |= try_cabarchive(filename)
        success |= try_pycdlib(filename)
    print("[STAGE 3] Byte-stream decompressors")
    if try_stream_decompressors(data, hint):
        success = True
    if hint.kind == "serialized":
        success |= try_pickle(data)
    if success:
        print("✔ At least one format was successfully recognized.\n")
        run_extraction(Path(filename))
    else:
        print("✘ No compression or archive format was recognized.\n")
    return success


def main(argv: list[str] | None = None) -> int:
    """Entry point. *argv* is the argument list without the program name."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] in {"-h", "--help"}:
        print(f"Usage: {Path(sys.argv[0]).name} <filename>\n")
        return 1 if len(args) != 1 else 0
    return 0 if process(args[0]) else 2


if __name__ == "__main__":
    sys.exit(main())
'''

_SOURCES['report'] = r"""#!/data/data/com.termux/files/usr/bin/env python
import argparse
import bz2
import gzip
import json
import lzma
import os
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

try:
    import py7zr
except ImportError:
    py7zr = None
try:
    import pylzma
except ImportError:
    pylzma = None
try:
    import zstandard as zstd
except ImportError:
    zstd = None
try:
    import lz4.frame as lz4_frame
except ImportError:
    lz4_frame = None
try:
    import snappy
except ImportError:
    snappy = None
try:
    import brotli
except ImportError:
    brotli = None


def fsz(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1024.0 or unit == "PB":
            return f"{int(n)} B" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} PB"


ARCHIVE_TYPES: dict[str, str] = {
    ".tar": "TAR Archive (.tar)",
    ".tar.gz": "GZip Tarball (.tar.gz)",
    ".tgz": "GZip Tarball (.tgz)",
    ".tar.xz": "XZ Tarball (.tar.xz)",
    ".txz": "XZ Tarball (.txz)",
    ".tar.bz2": "BZip2 Tarball (.tar.bz2)",
    ".tbz2": "BZip2 Tarball (.tbz2)",
    ".tar.lz4": "LZ4 Tarball (.tar.lz4)",
    ".tar.zst": "Zstandard Tarball (.tar.zst)",
    ".tzst": "Zstandard Tarball (.tzst)",
    ".tar.br": "Brotli Tarball (.tar.br)",
    ".tar.7z": "7-Zip Tarball (.tar.7z)",
    ".tar.bz3": "BZip3 Tarball (.tar.bz3)",
    ".tar.snappy": "Snappy Tarball (.tar.snappy)",
    ".zip": "ZIP Archive (.zip)",
    ".whl": "Python Wheel (.whl)",
    ".7z": "7-Zip Archive (.7z)",
    ".snappy": "Snappy Stream (.snappy)",
    ".zst": "Zstandard Stream (.zst)",
    ".gz": "GZip Stream (.gz)",
    ".bz3": "BZip3 Stream (.bz3)",
    ".bz2": "BZip2 Stream (.bz2)",
    ".xz": "XZ Compressed (.xz)",
    ".br": "Brotli Stream (.br)",
    ".lz4": "LZ4 Frame (.lz4)",
}
_SORTED_EXTS: tuple[str, ...] = tuple(sorted(ARCHIVE_TYPES, key=len, reverse=True))


def detect_archive(path: Path) -> tuple[Optional[str], Optional[str]]:
    name: str = path.name.lower()
    for ext in _SORTED_EXTS:
        if name.endswith(ext):
            return ext, ARCHIVE_TYPES[ext]
    return None, None


def _gzip_stream_size(path: Path) -> int:
    try:
        with open(path, "rb") as f:
            f.seek(-4, os.SEEK_END)
            return int.from_bytes(f.read(4), "little")
    except Exception:
        return int(path.stat().st_size * 2.8)


def _zstd_frame_size(path: Path) -> int:
    if zstd is not None:
        try:
            with open(path, "rb") as f:
                head: bytes = f.read(1024)
            params: Any = (
                zstd.get_frame_parameters(head)
                if hasattr(zstd, "get_frame_parameters")
                else zstd.ZstdDecompressor().frame_parameters(head)
            )
            size: int = getattr(params, "content_size", 0) or getattr(params, "uncompressed_size", 0)
            if size and size > 0:
                return int(size)
        except Exception:
            pass
    return int(path.stat().st_size * 3.2)


def analyze_archive(path: Path) -> dict[str, Any]:
    ext: Optional[str]
    desc: Optional[str]
    ext, desc = detect_archive(path)
    size: int = path.stat().st_size
    extracted: int = 0
    count: int = 0
    integrity: Optional[bool] = None
    err: str = ""
    try:
        if ext in (".zip", ".whl"):
            with zipfile.ZipFile(path, "r") as zf:
                infos: list[zipfile.ZipInfo] = zf.infolist()
                extracted = sum(i.file_size for i in infos)
                count = len(infos)
                integrity = zf.testzip() is None
        elif ext and (ext.startswith(".tar") or ext in (".tgz", ".txz", ".tbz2", ".tzst")):
            mode: str = "r:*"
            if ext in (".tar.gz", ".tgz"):
                mode = "r:gz"
            elif ext in (".tar.bz2", ".tbz2"):
                mode = "r:bz2"
            elif ext in (".tar.xz", ".txz"):
                mode = "r:xz"
            try:
                with tarfile.open(path, mode) as tf:
                    for m in tf:
                        if m.isfile():
                            extracted += m.size
                        count += 1
                    integrity = True
            except Exception as te:
                if zstd is not None and ext in (".tar.zst", ".tzst"):
                    dec: Any = zstd.ZstdDecompressor()
                    with (
                        open(path, "rb") as f,
                        dec.stream_reader(f) as sr,
                        tarfile.open(fileobj=sr, mode="r|*") as tf,
                    ):
                        for m in tf:
                            if m.isfile():
                                extracted += m.size
                            count += 1
                        integrity = True
                else:
                    extracted = int(size * 3.5)
                    integrity, err = False, str(te)
        elif ext == ".7z":
            success: bool = False
            if py7zr is not None:
                try:
                    with py7zr.SevenZipFile(path, "r") as sz:
                        extracted = sz.archive_info().uncompressed
                        count = len(sz.getnames())
                        integrity = True
                        success = True
                except Exception:
                    pass
            if not success and pylzma is not None:
                try:
                    with open(path, "rb") as f:
                        data: bytes = f.read()
                    dec_data: bytes = pylzma.decompress(data)
                    extracted = len(dec_data)
                    count = 1
                    integrity = True
                    success = True
                except Exception as pve:
                    err = str(pve)
            if not success:
                extracted, integrity, err = int(size * 4.1), False, err or "7z extraction modules failed"
        elif ext == ".gz":
            extracted, count, integrity = _gzip_stream_size(path), 1, True
        elif ext == ".zst":
            extracted, count, integrity = _zstd_frame_size(path), 1, True
        elif ext in (
            ".bz2",
            ".xz",
            ".lz4",
            ".br",
            ".snappy",
            ".bz3",
            ".tar.bz3",
            ".tar.snappy",
        ):
            ratios: dict[str, float] = {".bz2": 2.9, ".xz": 3.8, ".lz4": 2.4, ".br": 3.1}
            extracted = int(size * ratios.get(ext, 3.0))
            count, integrity = 1, True
        else:
            extracted, count, integrity = int(size * 2.5), 1, True
    except Exception as e:
        integrity, err, extracted = False, str(e), size
    return {
        "path": str(path),
        "filename": path.name,
        "ext": ext or "unknown",
        "archive_type": desc or "Unknown Archive",
        "compressed_size": size,
        "extracted_size": extracted,
        "file_count": count,
        "ratio": extracted / size if size > 0 else 1.0,
        "integrity": integrity,
        "error": err,
    }


def extract_archive(path: Path, dest_root: Path) -> tuple[bool, str]:
    ext: Optional[str]
    ext, _ = detect_archive(path)
    dest: Path = dest_root / f"{path.name}_extracted"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        if ext in (".zip", ".whl"):
            with zipfile.ZipFile(path, "r") as zf:
                zf.extractall(dest)
            return True, str(dest)
        if ext and (ext.startswith(".tar") or ext in (".tgz", ".txz", ".tbz2", ".tzst")):
            with tarfile.open(path, "r:*") as tf:
                try:
                    tf.extractall(dest, filter="data")
                except TypeError:
                    tf.extractall(dest)
            return True, str(dest)
        if ext == ".7z":
            extracted_ok: bool = False
            if py7zr is not None:
                try:
                    with py7zr.SevenZipFile(path, "r") as sz:
                        sz.extractall(path=dest)
                    extracted_ok = True
                except Exception:
                    pass
            if not extracted_ok and pylzma is not None:
                try:
                    with open(path, "rb") as f:
                        data: bytes = f.read()
                    dec_data: bytes = pylzma.decompress(data)
                    out_file: Path = dest / path.stem
                    out_file.write_bytes(dec_data)
                    extracted_ok = True
                except Exception as pve:
                    return False, str(pve)
            if extracted_ok:
                return True, str(dest)
            return False, "py7zr and pylzma failed to extract"
        if ext == ".gz":
            out: Path = dest / path.stem
            with gzip.open(path, "rb") as src, open(out, "wb") as dst:
                shutil.copyfileobj(src, dst)
            return True, str(dest)
        shutil.copyfile(path, dest / f"{path.name}.decompressed")
        return True, str(dest)
    except Exception as e:
        return False, str(e)


def cmd_scan(
    directory: str,
    auto_extract: bool,
    banner: bool,
    verbose: bool,
    as_json: bool,
) -> int:
    root: Path = Path(directory).resolve()
    if as_json:
        results: list[dict[str, Any]] = [
            analyze_archive(p) for p in root.rglob("*") if p.is_file() and detect_archive(p)[0]
        ]
        print(json.dumps(results, indent=2))
        return 0
    print(f"\x1b[38;5;39mScanning directory recursively:\x1b[0m {root}")
    if banner:
        print(
            f"\x1b[38;5;82m\n    ___                 __     _                  _____\n   / _ | ___________ / /_    (_)_  _____  / ___/______ ____  ___ ____\n  / __ |/ __/ __/ -_) __/   / / _ \\/ ___/ / /__/ __/ _ `/ _ \\/ -_) __/\n /_/ |_/_/  \\__/\\__/\\__/   /_/\\___/_/     \\___/\\__/\\_,_/ .__/\\__/_/\n                                                       /_/\n  [ INTEGRITY VALIDATION & EXTRACTED SIZE SCANNER v1.4.2 ]\x1b[0m"
        )
    results: list[dict[str, Any]] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if not detect_archive(p)[0]:
            continue
        info: dict[str, Any] = analyze_archive(p)
        results.append(info)
        if verbose:
            status: str = "\x1b[32m[PASS]\x1b[0m" if info["integrity"] else "\x1b[31m[FAIL]\x1b[0m"
            print(
                f"->Found:{info['filename']}|Type:{info['archive_type']}"
                f"|Comp:{fsz(info['compressed_size'])}"
                f"->Ext:{fsz(info['extracted_size'])}|{status}"
            )
    print("-" * 40)
    print(
        f"\x1b[1;37m{'FILENAME':<32} {'ARCHIVE TYPE':<26} {'COMPRESSED':<12} {'EXTRACTED':<12} {'INTEGRITY':<10}\x1b[0m"
    )
    print("-" * 40)
    total_comp: int = sum(i["compressed_size"] for i in results)
    total_ext: int = sum(i["extracted_size"] for i in results)
    for info in results:
        st: str
        if info["integrity"] is True:
            st = "\x1b[32mPASSED\x1b[0m"
        elif info["integrity"] is False:
            st = "\x1b[31mFAILED\x1b[0m"
        else:
            st = "\x1b[90mSKIP\x1b[0m"
        print(
            f"{info['filename'][:31]:<32} {info['archive_type'][:25]:<26} "
            f"{fsz(info['compressed_size']):<12} "
            f"{fsz(info['extracted_size']):<12} {st}"
        )
    print("=" * 40)
    print(f"\x1b[1;36mSUMMARY:\x1b[0m Found {len(results)} archive files.")
    print(f"Total Compressed Size : {fsz(total_comp)}")
    print(f"Total Extracted Size  : \x1b[1;32m{fsz(total_ext)}\x1b[0m")
    if total_comp > 0:
        print(f"Overall Expansion      : {total_ext / total_comp:.2f}x ({fsz(total_ext - total_comp)} saved)")
    if auto_extract and results:
        dest_root: Path = root / "extracted_archives"
        print(f"\n\x1b[38;5;214m[-a] Auto-extracting {len(results)} archives into:\x1b[0m {dest_root}")
        for info in results:
            ok: bool
            out: str
            ok, out = extract_archive(Path(info["path"]), dest_root)
            if ok:
                print(f"  \x1b[32m[\u2713]\x1b[0m Extracted {info['filename']} -> {out}")
            else:
                print(f"  \x1b[31m[\u2717]\x1b[0m Failed {info['filename']}: {out}")
    return 0


SKIP_DIRS: frozenset[str] = frozenset({"lazy", ".git", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache"})


def _count_stream(reader: Any) -> int:
    total: int = 0
    while True:
        chunk: bytes = reader.read(1 << 20)
        if not chunk:
            break
        total += len(chunk)
    return total


_SizeHandler = Callable[[Path], tuple[Optional[int], Optional[str]]]


def _h_tar(path: Path) -> tuple[Optional[int], Optional[str]]:
    try:
        with tarfile.open(path, "r:*") as tf:
            sz_sum: int = 0
            for m in tf:
                if m.isfile():
                    sz_sum += m.size
            return sz_sum, None
    except Exception as e:
        return None, str(e)


def _h_zstd(path: Path) -> tuple[Optional[int], Optional[str]]:
    if zstd is None:
        return None, "zstandard not installed"
    try:
        with open(path, "rb") as f:
            head: bytes = f.read(32)
            dec: Any = zstd.ZstdDecompressor()
            params: Any = (
                zstd.get_frame_parameters(head) if hasattr(zstd, "get_frame_parameters") else dec.frame_parameters(head)
            )
            size: int = getattr(params, "content_size", 0) or getattr(params, "uncompressed_size", 0)
            if size:
                return int(size), None
            f.seek(0)
            return _count_stream(dec.stream_reader(f)), None
    except Exception as e:
        return None, str(e)


def _h_xz(path: Path) -> tuple[Optional[int], Optional[str]]:
    try:
        with lzma.open(path, "rb") as f:
            return _count_stream(f), None
    except Exception as e:
        return None, str(e)


def _h_gzip(path: Path) -> tuple[Optional[int], Optional[str]]:
    try:
        with gzip.open(path, "rb") as f:
            return _count_stream(f), None
    except Exception as e:
        return None, str(e)


def _h_bz2(path: Path) -> tuple[Optional[int], Optional[str]]:
    try:
        with bz2.open(path, "rb") as f:
            return _count_stream(f), None
    except Exception as e:
        return None, str(e)


def _h_7z(path: Path) -> tuple[Optional[int], Optional[str]]:
    if py7zr is not None:
        try:
            with py7zr.SevenZipFile(path, "r") as z:
                return (
                    sum(f.uncompressed for f in z.list() if f.uncompressed is not None),
                    None,
                )
        except Exception:
            pass
    if pylzma is not None:
        try:
            with open(path, "rb") as f:
                data: bytes = f.read()
            dec_data: bytes = pylzma.decompress(data)
            return len(dec_data), None
        except Exception as e:
            return None, str(e)
    return None, "py7zr and pylzma not installed or failed"


def _h_zip(path: Path) -> tuple[Optional[int], Optional[str]]:
    try:
        with zipfile.ZipFile(path, "r") as z:
            return sum(i.file_size for i in z.infolist()), None
    except Exception as e:
        return None, str(e)


SIZE_HANDLERS: dict[str, tuple[str, _SizeHandler]] = {
    ".zst": ("zstd", _h_zstd),
    ".xz": ("xz", _h_xz),
    ".gz": ("gzip", _h_gzip),
    ".bz2": ("bzip2", _h_bz2),
    ".7z": ("7zip", _h_7z),
    ".zip": ("zip", _h_zip),
    ".whl": ("wheel", _h_zip),
    ".tar": ("tar", _h_tar),
}


def cmd_sizes(directory: str) -> int:
    root: Path = Path(directory).resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a directory")
        return 1
    print(f"Scanning {root}...\n")
    fmt: str = "{:<30} {:<10} {:>15} {:>15} {:>8}"
    print(fmt.format("File", "Format", "Compressed", "Uncompressed", "Ratio"))
    print("-" * 40)
    count: int = 0
    total_comp: int = 0
    total_uncomp: int = 0
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        handler: Optional[tuple[str, _SizeHandler]] = next(
            ((name, fn) for ext, (name, fn) in SIZE_HANDLERS.items() if p.name.endswith(ext)),
            None,
        )
        if handler is None:
            continue
        name: str
        fn: _SizeHandler
        name, fn = handler
        size: int = p.stat().st_size
        uncomp: Optional[int]
        _err: Optional[str]
        uncomp, _err = fn(p)
        count += 1
        total_comp += size
        if uncomp is not None:
            total_uncomp += uncomp
            ratio: float = uncomp / size if size > 0 else 0.0
            ratio_s: str = f"{ratio:.2f}x"
            size_s: str = fsz(uncomp)
        else:
            ratio_s = "Error"
            size_s = "Error"
        display: str = p.name[:27] + "..." if len(p.name) > 30 else p.name
        print(fmt.format(display, name, fsz(size), size_s, ratio_s))
    print("-" * 40)
    print(f"Total files: {count}")
    print(f"Total compressed: {fsz(total_comp)}")
    print(f"Total uncompressed: {fsz(total_uncomp)}")
    try:
        _, _, free = shutil.disk_usage(root)
        print(f"Free disk space: {fsz(free)}")
        if total_uncomp > free:
            shortfall: int = total_uncomp - free
            print(f"\n\u26a0\ufe0f  WARNING: Not enough space to extract all files! (Shortfall: {fsz(shortfall)})")
    except OSError:
        pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        prog="archive_report.py",
        description=(
            "Unified archive scanner and uncompressed-size reporter (merges xreport.py, xreport2.py, zreport.py)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  archive_report.py scan . -a -v\n"
            "  archive_report.py scan ~/Downloads -j\n"
            "  archive_report.py sizes ~/Downloads\n"
        ),
    )
    sub: argparse._SubParsersAction[Any] = parser.add_subparsers(dest="command")
    sp_scan: argparse.ArgumentParser = sub.add_parser(
        "scan",
        help="Full archive scan with integrity check + auto-extract (≈ xreport.py / xreport2.py).",
    )
    sp_scan.add_argument(
        "directory",
        nargs="?",
        default=".",
        help="Directory to scan (default: current directory).",
    )
    sp_scan.add_argument(
        "-a",
        "--auto-extract-all",
        action="store_true",
        help="Extract every discovered archive into <dir>/extracted_archives.",
    )
    sp_scan.add_argument(
        "-t",
        "--test-integrity",
        action="store_true",
        help="Print the ASCII banner at the start of the scan (preserved from the original scripts).",
    )
    sp_scan.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print a detail line for every archive as it is analysed.",
    )
    sp_scan.add_argument(
        "-j",
        "--json",
        action="store_true",
        help="Emit the scan result as JSON and exit.",
    )
    sp_scan.set_defaults(func=lambda a: cmd_scan(a.directory, a.auto_extract_all, a.test_integrity, a.verbose, a.json))
    sp_sizes: argparse.ArgumentParser = sub.add_parser(
        "sizes",
        help="Report *accurately measured* uncompressed sizes and disk-space headroom (≈ zreport.py).",
    )
    sp_sizes.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Directory to scan (default: current directory).",
    )
    sp_sizes.set_defaults(func=lambda a: cmd_sizes(a.path))
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    argv_list: list[str] = list(sys.argv[1:] if argv is None else argv)
    known: set[str] = {"scan", "sizes", "-h", "--help"}
    if not argv_list or argv_list[0] not in known:
        argv_list = ["scan", *argv_list]
    parser: argparse.ArgumentParser = build_parser()
    args: argparse.Namespace = parser.parse_args(argv_list)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
"""



# --------------------------------------------------------------------------- #
# runtime
# --------------------------------------------------------------------------- #
def _module_name(tool: str) -> str:
    return f"_consolidated_{FOLDER}_{tool}"


def load(tool: str) -> types.ModuleType:
    """Execute the embedded source of *tool* once and return its module."""
    cached = _LOADED.get(tool)
    if cached is not None:
        return cached
    if tool not in TOOLS:
        raise KeyError(tool)
    name = _module_name(tool)
    module = types.ModuleType(name)
    path = None
    if _HERE is not None:
        path = str(_HERE / str(TOOLS[tool]["source"]))
    module.__file__ = path or f"<{FOLDER}.py:{tool}>"
    module.__package__ = name
    module.__dict__["__builtins__"] = __builtins__
    sys.modules[name] = module
    try:
        exec(compile(_SOURCES[tool], module.__file__, "exec"), module.__dict__)
    except ModuleNotFoundError as exc:
        sys.modules.pop(name, None)
        missing = exc.name or "?"
        raise SystemExit(
            f"{FOLDER}.py: tool {tool!r} needs the {missing!r} package.\n"
            f"  install it with: pip install {_pip_name(missing)}"
        ) from None
    except BaseException:
        sys.modules.pop(name, None)
        raise
    _LOADED[tool] = module
    return module


def _pip_name(module: str) -> str:
    aliases = {
        "fitz": "PyMuPDF", "pymupdf": "PyMuPDF", "PyPDF2": "PyPDF2", "pypdf": "pypdf",
        "pdfplumber": "pdfplumber", "pdfminer": "pdfminer.six", "libcst": "libcst",
        "loguru": "loguru", "rich": "rich", "tree_sitter": "tree-sitter",
        "py7zr": "py7zr", "zstandard": "zstandard", "lz4": "lz4", "brotli": "Brotli",
        "cramjam": "cramjam", "snappy": "python-snappy", "pylzma": "pylzma",
        "pyppmd": "pyppmd", "joblib": "joblib", "xxhash": "xxhash",
        "pathspec": "pathspec", "send2trash": "Send2Trash", "watchdog": "watchdog",
        "ssdeep": "ssdeep", "tlsh": "py-tlsh", "rjsmin": "rjsmin", "jsmin": "jsmin",
        "csscompressor": "csscompressor", "tinycss2": "tinycss2", "htmlmin": "htmlmin",
        "luaparse": "luaparse", "readchar": "readchar",
    }
    if module.startswith("tree_sitter_"):
        return "tree-sitter-" + module[len("tree_sitter_"):].replace("_", "-")
    return aliases.get(module, module.replace("_", "-"))


def _entry(module: types.ModuleType):
    for attribute in ("main", "run", "entrypoint", "cli"):
        function = getattr(module, attribute, None)
        if callable(function) and getattr(function, "__module__", None) == module.__name__:
            return function, attribute
    return None, None


def run(tool: str, argv: list[str]) -> int:
    """Run one embedded tool with *argv* as its command line."""
    module = load(tool)
    function, attribute = _entry(module)
    if function is None:
        print(f"{FOLDER}.py: {tool!r} exposes no main()/run()/entrypoint()", file=sys.stderr)
        return 2
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        parameters = ()
    positional = [
        p for p in parameters
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty
    ]
    optional = [
        p for p in parameters
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is not p.empty
    ]
    saved = sys.argv
    sys.argv = [f"{FOLDER}.py {tool}"] + list(argv)
    try:
        if positional:
            if len(positional) > 1:
                print(f"{FOLDER}.py: cannot call {tool}.{attribute}(): it wants {len(positional)} arguments",
                      file=sys.stderr)
                return 2
            result = function(list(argv))
        elif optional:
            result = function(list(argv))
        else:
            result = function()
    except KeyboardInterrupt:
        print(f"{FOLDER}.py: {tool} interrupted", file=sys.stderr)
        return 130
    finally:
        sys.argv = saved
    return result if isinstance(result, int) else 0


def _fatal(statement: ast.stmt) -> bool:
    """True when *statement* aborts the program (raise / sys.exit / SystemExit)."""
    if isinstance(statement, ast.Raise):
        return True
    for node in ast.walk(statement):
        if isinstance(node, ast.Name) and node.id == "SystemExit":
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "exit":
            return True
    return False


def _third_party(tool: str) -> tuple[list[str], list[str]]:
    """Return ``(required, optional)`` third-party modules imported by *tool*.

    Only module level imports are considered, and anything imported inside a
    module level ``try:`` block counts as optional: the tool already handles its
    absence (that is what the guard is for).
    """
    try:
        tree = ast.parse(_SOURCES[tool])
    except SyntaxError:
        return [], []

    required: list[str] = []
    optional: list[str] = []

    def note(name: str, bucket: list[str]) -> None:
        if name in sys.stdlib_module_names or name == "__future__":
            return
        if name not in bucket:
            bucket.append(name)

    def visit(statements: list, bucket: list[str]) -> None:
        for node in statements:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    note(alias.name.split(".")[0], bucket)
            elif isinstance(node, ast.ImportFrom):
                if node.level or not node.module:
                    continue
                note(node.module.split(".")[0], bucket)
            elif isinstance(node, ast.Try):
                # an import guarded by try/except is optional only when the
                # handlers degrade gracefully; raise/sys.exit means it is fatal
                fatal = any(_fatal(stmt) for handler in node.handlers for stmt in handler.body)
                bucket_for_try = required if fatal else optional
                visit(node.body, bucket_for_try)
                visit(node.orelse, bucket_for_try)
                visit(node.finalbody, bucket_for_try)
                for handler in node.handlers:
                    visit(handler.body, optional)
            elif isinstance(node, ast.If):
                visit(node.body, bucket)
                visit(node.orelse, bucket)

    visit(tree.body, required)
    return required, [name for name in optional if name not in required]


def _missing(tool: str) -> list[str]:
    absent: list[str] = []
    for name in _third_party(tool)[0]:
        try:
            if importlib.util.find_spec(name) is None:
                absent.append(name)
        except (ImportError, ValueError):
            absent.append(name)
    return absent


def _optional(tool: str) -> list[str]:
    absent: list[str] = []
    for name in _third_party(tool)[1]:
        try:
            if importlib.util.find_spec(name) is None:
                absent.append(name)
        except (ImportError, ValueError):
            absent.append(name)
    return absent


def _cmd_list() -> int:
    width = max(len(name) for name in TOOLS)
    print(f"{FOLDER}: {len(TOOLS)} tool(s)\n")
    for name, info in TOOLS.items():
        print(f"  {name.ljust(width)}  {info['summary']}")
        print(f"  {' ' * width}  from {info['source']} ({info['lines']} lines)")
    return 0


def _cmd_doctor() -> int:
    problems = 0
    for name in TOOLS:
        absent = _missing(name)
        extras = _optional(name)
        if absent:
            problems += 1
            wanted = ", ".join(dict.fromkeys(_pip_name(item) for item in absent))
            print(f"  MISSING  {name}: {wanted}")
        elif extras:
            wanted = ", ".join(dict.fromkeys(_pip_name(item) for item in extras))
            print(f"  ok       {name} (optional extras absent: {wanted})")
        else:
            print(f"  ok       {name}")
    if problems:
        print(f"\n{problems} tool(s) have unmet dependencies (names above are pip package names).")
        return 1
    print("\nAll tools are ready.")
    return 0


def _cmd_unpack(rest: list[str]) -> int:
    parser = argparse.ArgumentParser(prog=f"{FOLDER}.py unpack",
                                     description="Write an embedded tool's original source back out.")
    parser.add_argument("tool", choices=list(TOOLS))
    parser.add_argument("-o", "--output", default=".", help="Directory to write into (default: .).")
    parser.add_argument("--stdout", action="store_true", help="Print to stdout instead of writing a file.")
    parser.add_argument("-f", "--force", action="store_true", help="Overwrite an existing file.")
    args = parser.parse_args(rest)
    text = _SOURCES[args.tool]
    if args.stdout:
        sys.stdout.write(text)
        return 0
    target = Path(args.output) / str(TOOLS[args.tool]["source"])
    if target.exists() and not args.force:
        print(f"refusing to overwrite {target} (use --force)", file=sys.stderr)
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    print(f"wrote {target}")
    return 0


def _usage() -> str:
    lines = [f"usage: python {FOLDER}.py <command> [args...]", "", "commands:"]
    lines.append("  list                     list the embedded tools")
    lines.append("  doctor                   report missing third-party packages")
    lines.append("  unpack <tool> [-o DIR]   write a tool's original source back out")
    for name, info in TOOLS.items():
        lines.append(f"  {name:<24} {info['summary']}")
    lines.append("")
    lines.append("run '<command> --help' for a tool's own options")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help", "help"}:
        print(_usage())
        return 0
    command = args[0]
    rest = args[1:]
    if command == "list":
        return _cmd_list()
    if command == "doctor":
        return _cmd_doctor()
    if command in {"unpack", "source"}:
        return _cmd_unpack(rest)
    if command in TOOLS:
        return run(command, rest)
    close = difflib.get_close_matches(command, list(TOOLS) + ["list", "doctor", "unpack"], n=1)
    hint = f" (did you mean {close[0]!r}?)" if close else ""
    print(f"{FOLDER}.py: unknown command {command!r}{hint}", file=sys.stderr)
    print(_usage(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
