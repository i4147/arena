#!/usr/bin/env python3
"""archive.py — one container front end for the thirteen archive tools in g005.

g005 held two families of scripts wearing twenty-four names:

    single streams   gzip/bz2/xz/zstd/brotli/lz4/ppmd — those live in
                     ``codecs.py`` next door
    containers       tar, tar.gz/bz2/xz/zst/br/lz4, zip, 7z — this file

    pack      dir/ files/ -> one archive            (archive_dir, compress_subdirs, br, brer)
    unpack    archive -> files                      (trydecomp, xtar, archive_tools extract)
    list      members, sizes, compression ratio     (xreport, smart_archiver)
    info      format, codec, totals                 (xreport)
    split     one archive -> N archives             (zstdtools split, pdftools split-size)
    remove    drop members, rewrite                 (rm_from_archive)
    merge     many archives -> one                  (archive_merger)
    convert   whl <-> tar.xz, zip <-> tar.zst       (artools archive-convert, whl-txz)
    check     integrity without extracting          (archive_tools check)
    fix       repair dir/name/name misextraction    (archive_tools fix)
    bench     the same tree in every format         (compression_benchmark, smart_archiver)
    doctor

Two rules are never relaxed:

* nothing is written outside the destination — member names are validated before
  extraction and every write goes through a temporary file that is renamed;
* a missing optional backend (py7zr, zstandard, lz4, brotli) is reported by
  ``archive.py doctor`` instead of failing halfway through a batch.

Exit codes: 0 ok · 1 errors · 2 usage · 130 interrupted
"""


import argparse
import bz2
import contextlib
import fnmatch
import gzip
import importlib
import json
import lzma
import os
import shutil
import sys
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "1.0.0"
PROG = "archive.py"
CHUNK = 1 << 20

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", ".venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".cache", "target", "dist", "build",
}


def _optional(name: str):
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 - optional wheel, not an error
        return None


_zstd = _optional("zstandard")
_brotli = _optional("brotli")
_lz4_frame = _optional("lz4.frame")
_py7zr = _optional("py7zr")


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


# --------------------------------------------------------------------------- #
# formats                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Format:
    name: str
    suffixes: tuple[str, ...]
    kind: str               # tar | zip | 7z | stream
    codec: str = ""         # stream codec wrapped around a tar, or the stream itself
    hint: str = ""

    @property
    def suffix(self) -> str:
        return self.suffixes[0]


FORMATS: dict[str, Format] = {}


def _fmt(name: str, suffixes: tuple[str, ...], kind: str, codec: str = "", hint: str = "") -> None:
    FORMATS[name] = Format(name, suffixes, kind, codec, hint)


_fmt("tar", (".tar",), "tar", hint="stdlib tarfile")
_fmt("tar.gz", (".tar.gz", ".tgz"), "tar", "gzip", "stdlib gzip")
_fmt("tar.bz2", (".tar.bz2", ".tbz2", ".tbz"), "tar", "bz2", "stdlib bz2")
_fmt("tar.xz", (".tar.xz", ".txz"), "tar", "xz", "stdlib lzma")
_fmt("tar.zst", (".tar.zst", ".tzst"), "tar", "zstd", "zstandard")
_fmt("tar.br", (".tar.br",), "tar", "brotli", "brotli")
_fmt("tar.lz4", (".tar.lz4",), "tar", "lz4", "lz4.frame")
_fmt("zip", (".zip", ".whl", ".jar"), "zip", hint="stdlib zipfile")
_fmt("7z", (".7z",), "7z", hint="py7zr")
_fmt("gzip", (".gz",), "stream", "gzip", "stdlib gzip")
_fmt("bz2", (".bz2",), "stream", "bz2", "stdlib bz2")
_fmt("xz", (".xz",), "stream", "xz", "stdlib lzma")
_fmt("lzma", (".lzma",), "stream", "lzma", "stdlib lzma (alone)")
_fmt("zstd", (".zst", ".zstd"), "stream", "zstd", "zstandard")
_fmt("brotli", (".br",), "stream", "brotli", "brotli")
_fmt("lz4", (".lz4",), "stream", "lz4", "lz4.frame")

SUFFIX_ORDER = sorted(
    ((suffix, fmt) for fmt in FORMATS.values() for suffix in fmt.suffixes),
    key=lambda item: len(item[0]),
    reverse=True,
)

MAGIC: tuple[tuple[bytes, int, str], ...] = (
    (b"7z\xbc\xaf\x27\x1c", 0, "7z"),
    (b"PK\x03\x04", 0, "zip"),
    (b"PK\x05\x06", 0, "zip"),
    (b"\x1f\x8b", 0, "gzip"),
    (b"BZh", 0, "bz2"),
    (b"\xfd7zXZ\x00", 0, "xz"),
    (b"\x28\xb5\x2f\xfd", 0, "zstd"),
    (b"\x04\x22\x4d\x18", 0, "lz4"),
)


def by_suffix(path: Path) -> Format | None:
    name = path.name.lower()
    for suffix, fmt in SUFFIX_ORDER:
        if name.endswith(suffix):
            return fmt
    return None


def by_magic(path: Path) -> str | None:
    try:
        with path.open("rb") as handle:
            head = handle.read(512)
    except OSError:
        return None
    for magic, offset, name in MAGIC:
        if head[offset:offset + len(magic)] == magic:
            return name
    if head[257:262] == b"ustar":
        return "tar"
    return None


def detect(path: Path) -> Format:
    """Extension first, magic second, tar-inside-a-stream third."""
    known = by_suffix(path)
    if known is not None:
        return known
    magic = by_magic(path)
    if magic in FORMATS:
        return FORMATS[magic]
    if magic == "tar":
        return FORMATS["tar"]
    raise ValueError(f"{path}: unknown archive format (pass --format)")


def stream_codec(name: str):
    if name == "gzip":
        return _GzipCodec()
    if name == "bz2":
        return _Bz2Codec()
    if name == "xz":
        return _LzmaCodec(lzma.FORMAT_XZ)
    if name == "lzma":
        return _LzmaCodec(lzma.FORMAT_ALONE)
    if name == "zstd":
        if _zstd is None:
            raise RuntimeError("zstandard is not installed: pip install zstandard")
        return _ZstdCodec()
    if name == "brotli":
        if _brotli is None:
            raise RuntimeError("brotli is not installed: pip install brotli")
        return _BrotliCodec()
    if name == "lz4":
        if _lz4_frame is None:
            raise RuntimeError("lz4 is not installed: pip install lz4")
        return _Lz4Codec()
    raise ValueError(f"no stream codec named {name!r}")


class _Closable:
    """Give every codec handle a plain ``close()`` (contextlib.closing lacks one)."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def close(self) -> None:
        self._inner.close()


class _GzipCodec:
    def write(self, raw, level: int):
        return _Closable(gzip.GzipFile(filename="", fileobj=raw, mode="wb",
                                       compresslevel=max(0, min(9, level)), mtime=0))

    def read(self, raw):
        return _Closable(gzip.GzipFile(filename="", fileobj=raw, mode="rb"))


class _Bz2Codec:
    def write(self, raw, level: int):
        return _Closable(bz2.BZ2File(raw, "wb", compresslevel=max(1, min(9, level))))

    def read(self, raw):
        return _Closable(bz2.BZ2File(raw, "rb"))


class _LzmaCodec:
    def __init__(self, fmt: int):
        self.fmt = fmt

    def write(self, raw, level: int):
        return _Closable(lzma.LZMAFile(raw, "wb", format=self.fmt, preset=max(0, min(9, level))))

    def read(self, raw):
        return _Closable(lzma.LZMAFile(raw, "rb"))


class _ZstdCodec:
    def write(self, raw, level: int):
        return _Closable(_zstd.ZstdCompressor(level=max(1, min(22, level))).stream_writer(raw, closefd=False))

    def read(self, raw):
        return _Closable(_zstd.ZstdDecompressor().stream_reader(raw, closefd=False))


class _BrotliCodec:
    def write(self, raw, level: int):
        engine = _brotli.Compressor(quality=max(0, min(11, level)))

        class _W:
            def write(self, data: bytes) -> int:
                payload = engine.process(data)
                if payload:
                    raw.write(payload)
                return len(data)

            def close(self) -> None:
                tail = engine.finish()
                if tail:
                    raw.write(tail)

        return _W()

    def read(self, raw):
        engine = _brotli.Decompressor()

        class _R:
            def __init__(self):
                self._buffer = b""
                self._eof = False

            def read(self, size: int = -1) -> bytes:
                while not self._eof and (size < 0 or len(self._buffer) < size):
                    blob = raw.read(CHUNK)
                    if not blob:
                        self._eof = True
                        break
                    self._buffer += engine.process(blob)
                if size < 0:
                    out, self._buffer = self._buffer, b""
                    return out
                out, self._buffer = self._buffer[:size], self._buffer[size:]
                return out

            def close(self) -> None:
                raw.close()

        return _R()


class _Lz4Codec:
    def write(self, raw, level: int):
        return _Closable(_lz4_frame.open(raw, "wb", compression_level=max(0, min(16, level))))

    def read(self, raw):
        return _Closable(_lz4_frame.open(raw, "rb"))


# --------------------------------------------------------------------------- #
# run context                                                                  #
# --------------------------------------------------------------------------- #


@dataclass
class Ctx:
    command: str = ""
    dry_run: bool = False
    backup: bool = False
    force: bool = False
    quiet: bool = False
    verbose: bool = False
    json: bool = False
    started: float = field(default_factory=time.perf_counter)
    files: list[dict] = field(default_factory=list)
    failures: int = 0


CTX = Ctx()


def say(message: str) -> None:
    if not CTX.quiet and not CTX.json:
        print(message)


def note(message: str) -> None:
    if CTX.verbose:
        print(message, file=sys.stderr)


def human(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(size) < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def deliver(target: Path, writer: Callable[[Path], None], *, suffix: str | None = None) -> Path | None:
    """Write through a temporary file; honour --dry-run and --backup."""
    if CTX.dry_run:
        say(f"[dry-run] would write {target}")
        CTX.files.append({"path": str(target), "action": "would-write"})
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    tail = suffix if suffix is not None else target.suffix
    tmp = target.with_name(f".{target.stem}.{os.getpid()}.tmp{tail}")
    try:
        writer(tmp)
        if target.exists() and CTX.backup:
            shutil.copy2(target, target.with_suffix(target.suffix + ".bak"))
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    size = target.stat().st_size
    CTX.files.append({"path": str(target), "bytes": size, "action": "wrote"})
    note(f"wrote {target} ({human(size)})")
    return target


# --------------------------------------------------------------------------- #
# member model                                                                 #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Member:
    name: str
    size: int
    is_dir: bool = False
    mtime: float = 0.0


def safe_member(name: str, strip: int = 0) -> Path | None:
    """Relative, traversal-free member path or None when it must be refused."""
    pure = name.replace("\\", "/")
    parts = [part for part in pure.split("/") if part not in ("", ".")]
    if not parts or pure.startswith("/") or any(part == ".." for part in parts):
        return None
    if strip:
        parts = parts[strip:]
        if not parts:
            return None
    return Path(*parts)


def matches(name: str, patterns: Sequence[str]) -> bool:
    return not patterns or any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


# --------------------------------------------------------------------------- #
# reading archives                                                             #
# --------------------------------------------------------------------------- #


@contextlib.contextmanager
def open_tar(path: Path, fmt: Format | None = None) -> Iterator[tarfile.TarFile]:
    """Streaming tar reader that understands the compressed tar formats."""
    fmt = fmt or detect(path)
    if fmt.kind != "tar":
        raise ValueError(f"{path}: not a tar container")
    if not fmt.codec:
        with tarfile.open(path, "r:") as tar:
            yield tar
        return
    codec = stream_codec(fmt.codec)
    raw = path.open("rb")
    try:
        stream = codec.read(raw)
        try:
            with tarfile.open(fileobj=stream, mode="r|") as tar:
                yield tar
        finally:
            with contextlib.suppress(Exception):
                stream.close()
    finally:
        with contextlib.suppress(Exception):
            raw.close()


def list_members(path: Path, fmt: Format | None = None) -> list[Member]:
    fmt = fmt or detect(path)
    if fmt.kind == "tar":
        with open_tar(path, fmt) as tar:
            return [Member(m.name, m.size, m.isdir(), m.mtime) for m in tar]
    if fmt.kind == "zip":
        with zipfile.ZipFile(path) as zf:
            return [Member(info.filename, info.file_size, info.is_dir()) for info in zf.infolist()]
    if fmt.kind == "7z":
        if _py7zr is None:
            raise RuntimeError("7z support needs py7zr: pip install py7zr")
        with _py7zr.SevenZipFile(path, "r") as zf:
            out: list[Member] = []
            for item in zf.list():
                out.append(Member(item.filename, item.uncompressed or 0, item.is_directory))
            return out
    # bare stream: exactly one virtual member
    return [Member(path.name, 0)]


def estimate_size(path: Path, fmt: Format) -> int | None:
    """Uncompressed size when the container or stream header knows it."""
    if fmt.kind == "stream":
        if fmt.codec == "gzip":
            try:
                with path.open("rb") as handle:
                    handle.seek(-4, os.SEEK_END)
                    return int.from_bytes(handle.read(4), "little")
            except OSError:
                return None
        if fmt.codec == "zstd" and _zstd is not None:
            try:
                with path.open("rb") as handle:
                    params = _zstd.get_frame_parameters(handle.read(64))
                size = getattr(params, "content_size", 0) or 0
                return int(size) if size else None
            except Exception:  # noqa: BLE001
                return None
        return None
    if fmt.kind == "zip":
        with zipfile.ZipFile(path) as zf:
            return sum(info.file_size for info in zf.infolist())
    return None


# --------------------------------------------------------------------------- #
# writing archives                                                             #
# --------------------------------------------------------------------------- #


def _iter_paths(root: Path, exclude: Sequence[str]) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        if path.is_symlink():
            continue
        if any(fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(str(path), pattern)
               for pattern in exclude):
            continue
        yield path


def create_archive(target: Path, sources: Sequence[Path], fmt: Format, level: int,
                   exclude: Sequence[str], compress: bool) -> tuple[int, int]:
    """Write ``sources`` into ``target``; returns (member count, bytes written)."""
    files = 0
    level = level if level >= 0 else 6

    def writer(tmp: Path) -> None:
        nonlocal files
        if fmt.kind == "tar":
            if fmt.codec:
                raw = tmp.open("wb")
                stream = stream_codec(fmt.codec).write(raw, level)
                try:
                    with tarfile.open(fileobj=stream, mode="w|") as tar:
                        for source in sources:
                            files += _add_to_tar(tar, source, exclude)
                finally:
                    stream.close()
                    raw.close()
            else:
                with tarfile.open(tmp, "w:") as tar:
                    for source in sources:
                        files += _add_to_tar(tar, source, exclude)
        elif fmt.kind == "zip":
            method = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
            with zipfile.ZipFile(tmp, "w", compression=method,
                                 compresslevel=level if compress else None) as zf:
                for source in sources:
                    for path in _iter_paths(source, exclude):
                        arcname = str(path.relative_to(source.parent)) if source.is_dir() else source.name
                        zf.write(path, arcname)
                        files += 1
        elif fmt.kind == "7z":
            if _py7zr is None:
                raise RuntimeError("7z support needs py7zr: pip install py7zr")
            with _py7zr.SevenZipFile(tmp, "w") as zf:
                for source in sources:
                    if source.is_dir():
                        zf.writeall(source, arcname=source.name)
                    else:
                        zf.write(source, arcname=source.name)
                    files += sum(1 for _ in _iter_paths(source, exclude))
        else:  # bare stream: exactly one file
            if len(sources) != 1 or not sources[0].is_file():
                raise ValueError(f"{target.name}: a {fmt.name} stream holds exactly one file")
            source = sources[0]
            raw = tmp.open("wb")
            stream = stream_codec(fmt.codec).write(raw, level)
            try:
                with source.open("rb") as fin:
                    shutil.copyfileobj(fin, stream, CHUNK)
            finally:
                stream.close()
                raw.close()
            files = 1

    deliver(target, writer, suffix=fmt.suffix)
    written = target.stat().st_size if target.exists() and not CTX.dry_run else 0
    return files, written


def _add_to_tar(tar: tarfile.TarFile, source: Path, exclude: Sequence[str]) -> int:
    """Add a file or tree, filtering skip dirs.  tar never compresses members."""
    count = 0
    if source.is_file():
        with source.open("rb") as handle:
            info = tar.gettarinfo(str(source), arcname=source.name)
            tar.addfile(info, handle)
        return 1
    for path in _iter_paths(source, exclude):
        arcname = str(path.relative_to(source.parent))
        info = tar.gettarinfo(str(path), arcname=arcname)
        if info.isfile():
            with path.open("rb") as handle:
                tar.addfile(info, handle)
        else:
            tar.addfile(info)
        count += 1
    return count


# --------------------------------------------------------------------------- #
# extraction                                                                   #
# --------------------------------------------------------------------------- #


def _prepare_target(dest: Path, member: Path) -> Path:
    target = dest / member
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def extract_archive(path: Path, dest: Path, *, fmt: Format | None = None, strip: int = 0,
                    patterns: Sequence[str] = (), skip_existing: bool = False) -> tuple[int, list[str]]:
    """Extract safely; returns (writes, refused member names)."""
    fmt = fmt or detect(path)
    dest.mkdir(parents=True, exist_ok=True)
    written = 0
    refused: list[str] = []

    if fmt.kind == "tar":
        with open_tar(path, fmt) as tar:
            for member in tar:
                if not matches(member.name, patterns):
                    continue
                relative = safe_member(member.name, strip)
                if relative is None:
                    refused.append(member.name)
                    continue
                target = _prepare_target(dest, relative)
                if skip_existing and target.exists():
                    continue
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if member.issym() or member.islnk():
                    note(f"skip link {member.name}")
                    continue
                source = tar.extractfile(member)
                if source is None:
                    continue
                with source, target.open("wb") as out:
                    shutil.copyfileobj(source, out, CHUNK)
                written += 1
        return written, refused

    if fmt.kind == "zip":
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if not matches(info.filename, patterns):
                    continue
                relative = safe_member(info.filename, strip)
                if relative is None:
                    refused.append(info.filename)
                    continue
                target = _prepare_target(dest, relative)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if skip_existing and target.exists():
                    continue
                with zf.open(info) as source, target.open("wb") as out:
                    shutil.copyfileobj(source, out, CHUNK)
                written += 1
        return written, refused

    if fmt.kind == "7z":
        if _py7zr is None:
            raise RuntimeError("7z support needs py7zr: pip install py7zr")
        with _py7zr.SevenZipFile(path, "r") as zf:
            names = zf.getnames()
            for name in names:
                if safe_member(name, strip) is None:
                    refused.append(name)
        names = [name for name in names if matches(name, patterns)]
        if patterns:
            zf = _py7zr.SevenZipFile(path, "r")
            try:
                zf.extract(path=dest, targets=names)
            finally:
                zf.close()
            return len(names), refused
        if refused:
            raise RuntimeError(f"{path}: refusing unsafe members: {refused[:3]}")
        zf = _py7zr.SevenZipFile(path, "r")
        try:
            zf.extractall(path=dest)
        finally:
            zf.close()
        written = len([name for name in names if matches(name, patterns)])
        return written, refused

    # bare stream
    codec = stream_codec(fmt.codec)
    target = dest / _stream_output_name(path, fmt)
    if strip or patterns and not matches(target.name, patterns):
        return 0, []
    if skip_existing and target.exists():
        return 0, []
    with path.open("rb") as fin, target.open("wb") as out:
        stream = codec.read(fin)
        try:
            shutil.copyfileobj(stream, out, CHUNK)
        finally:
            stream.close()
    return 1, refused


def _stream_output_name(path: Path, fmt: Format) -> str:
    name = path.name
    for suffix in fmt.suffixes:
        if name.lower().endswith(suffix):
            trimmed = name[: -len(suffix)]
            return trimmed or name + ".out"
    return name + ".out"


# --------------------------------------------------------------------------- #
# commands                                                                     #
# --------------------------------------------------------------------------- #


def require(path: Path, what: str = "path") -> bool:
    if path.exists():
        return True
    print(f"{path}: no such {what}", file=sys.stderr)
    return False


def contents(directory: Path) -> list[Path]:
    """Children of a staging directory, so arcnames do not gain its tmp prefix."""
    return sorted(directory.iterdir())


def resolve_format(name: str) -> Format:
    key = name.strip().lower().lstrip(".")
    aliases = {"tgz": "tar.gz", "tbz2": "tar.bz2", "tbz": "tar.bz2", "txz": "tar.xz",
               "tzst": "tar.zst", "gz": "gzip", "zst": "zstd", "br": "brotli", "whl": "zip"}
    key = aliases.get(key, key)
    if key not in FORMATS:
        known = ", ".join(sorted(FORMATS))
        raise SystemExit(f"unknown format {name!r}; known: {known}")
    return FORMATS[key]


def pick_default_format() -> Format:
    if _zstd is not None:
        return FORMATS["tar.zst"]
    return FORMATS["tar.gz"]


def cmd_pack(args: argparse.Namespace) -> int:
    fmt = resolve_format(args.format) if args.format != "auto" else pick_default_format()
    exact = args.output
    out_dir = args.output_dir or Path.cwd()
    if exact is None and args.output_dir is not None and by_suffix(args.output_dir) is not None:
        # `-o dist/app.tar.zst` / `-o app.tar.zst` mean "write this file"
        exact = args.output_dir
        out_dir = exact.parent
    sources = [Path(path).expanduser() for path in (args.paths or ["."])]
    for source in sources:
        if not require(source, "file or directory"):
            return 1
    level = args.level

    if args.per_subdir:
        roots = [source for source in sources if source.is_dir()]
        if not roots:
            print("--per-subdir needs a directory", file=sys.stderr)
            return 2
        jobs = [(sub, out_dir / f"{sub.name}{fmt.suffix}")
                for root in roots for sub in sorted(root.iterdir()) if sub.is_dir()]
        staging: Path | None = None
        if args.merge:
            staging = Path(tempfile.mkdtemp(prefix="archive-parts-"))
            jobs = [(sub, staging / f"{sub.name}{fmt.suffix}") for sub, _ in jobs]
    else:
        if exact is not None and len(sources) > 1:
            print("an exact output path takes exactly one source; use -o DIR for several", file=sys.stderr)
            return 2
        jobs = []
        for source in sources:
            target = exact if exact is not None else out_dir / f"{source.name}{fmt.suffix}"
            jobs.append((source, target))

    def run(job: tuple[Path, Path]) -> tuple[Path, int, int]:
        source, target = job
        count, written = create_archive(target, [source], fmt, level, args.exclude, not args.no_compress)
        return target, count, written

    made: list[Path] = []
    created_at = time.perf_counter()
    if args.jobs > 1 and len(jobs) > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            for target, count, written in pool.map(run, jobs):
                made.append(target)
                if not args.merge:
                    say(f"{target}  {count} member(s)  {human(written)}")
    else:
        for job in jobs:
            try:
                target, count, written = run(job)
            except Exception as exc:  # noqa: BLE001
                CTX.failures += 1
                print(f"{job[0]}: {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            made.append(target)
            if not args.merge:
                say(f"{target}  {count} member(s)  {human(written)}")

    if args.merge and made and not CTX.dry_run:
        merged = exact if exact is not None else out_dir / f"{sources[0].name}{fmt.suffix}"
        merge_archives(made, merged, fmt, level)
        say(f"merged {len(made)} part(s) -> {merged}  {human(merged.stat().st_size)}  "
            f"in {time.perf_counter() - created_at:.2f}s")
        if args.cleanup:
            for part in made:
                part.unlink(missing_ok=True)
            note(f"removed {len(made)} part(s)")
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)

    if args.cleanup and not args.merge and not CTX.dry_run:
        for source, target in jobs:
            if target.exists() and target in made:
                with contextlib.suppress(OSError):
                    if source.is_dir():
                        shutil.rmtree(source)
                    else:
                        source.unlink()
                    note(f"removed source {source}")
    return 1 if CTX.failures else 0


def merge_archives(inputs: Sequence[Path], target: Path, fmt: Format, level: int) -> None:
    """Concatenate members of several archives into one, de-duplicated by name."""
    members: dict[str, tuple[Path, Member]] = {}
    for item in inputs:
        item_fmt = detect(item)
        for member in list_members(item, item_fmt):
            members[member.name] = (item, member)

    def writer(tmp: Path) -> None:
        if fmt.kind == "tar":
            raw = tmp.open("wb")
            stream = stream_codec(fmt.codec).write(raw, level) if fmt.codec else None
            try:
                box = stream if stream is not None else raw
                with tarfile.open(fileobj=box, mode="w|") as tar:
                    for name, (item, member) in members.items():
                        _copy_member(tar, item, member, name)
            finally:
                if stream is not None:
                    stream.close()
                raw.close()
        else:
            with tempfile.TemporaryDirectory(prefix="archive-merge-") as tmpdir:
                for item in inputs:
                    extract_archive(item, Path(tmpdir), skip_existing=False)
                create_archive(tmp, contents(Path(tmpdir)), fmt, level, (), True)

    deliver(target, writer, suffix=fmt.suffix)


def _copy_member(tar: tarfile.TarFile, item: Path, member: Member, name: str) -> None:
    """Copy one member from ``item`` into ``tar`` without touching the disk."""
    fmt = detect(item)
    info = tarfile.TarInfo(name)
    info.size = member.size if not member.is_dir else 0
    info.mtime = int(member.mtime) or int(time.time())
    if member.is_dir:
        info.type = tarfile.DIRTYPE
        tar.addfile(info)
        return
    if fmt.kind == "tar":
        with open_tar(item, fmt) as source:
            for entry in source:
                if entry.name != member.name:
                    continue
                handle = source.extractfile(entry)
                data = handle.read() if handle is not None else b""
                break
            else:
                return
    elif fmt.kind == "zip":
        with zipfile.ZipFile(item) as zf:
            data = zf.read(member.name)
    elif fmt.kind == "7z":
        with tempfile.TemporaryDirectory(prefix="archive-member-") as tmpdir:
            extract_archive(item, Path(tmpdir), patterns=[member.name])
            candidate = Path(tmpdir) / safe_member(member.name)
            data = candidate.read_bytes() if candidate.exists() else b""
    else:
        with tempfile.TemporaryDirectory(prefix="archive-member-") as tmpdir:
            extract_archive(item, Path(tmpdir))
            candidates = [p for p in Path(tmpdir).rglob("*") if p.is_file()]
            data = candidates[0].read_bytes() if candidates else b""
    info.size = len(data)
    import io

    tar.addfile(info, io.BytesIO(data))


def cmd_unpack(args: argparse.Namespace) -> int:
    archives = [Path(path).expanduser() for path in args.paths]
    if not archives:
        print("nothing to unpack", file=sys.stderr)
        return 1
    missing = [archive for archive in archives if not archive.exists()]
    for archive in missing:
        print(f"{archive}: no such archive", file=sys.stderr)
    archives = [archive for archive in archives if archive.exists()]
    for archive in archives:
        try:
            fmt = resolve_format(args.format) if args.format != "auto" else detect(archive)
            destination = args.output_dir / archive.name if args.output_dir and len(archives) > 1 else (
                args.output_dir or Path.cwd())
            if CTX.dry_run:
                members = list_members(archive, fmt)
                keep = [m for m in members if matches(m.name, args.members)]
                say(f"[dry-run] would extract {len(keep)} of {len(members)} member(s) "
                    f"from {archive} into {destination}")
                for member in keep[:10]:
                    say(f"    {member.name}")
                CTX.files.append({"path": str(archive), "members": len(keep), "action": "would-extract"})
                continue
            written, refused = extract_archive(
                archive, destination, fmt=fmt, strip=args.strip_components,
                patterns=args.members, skip_existing=args.skip_existing,
            )
            for name in refused:
                CTX.failures += 1
                print(f"{archive}: refused unsafe member {name!r}", file=sys.stderr)
            CTX.files.append({"path": str(archive), "members": written, "action": "extracted"})
            say(f"{archive}  {written} file(s) -> {destination}")
        except Exception as exc:  # noqa: BLE001 - one bad archive must not stop the batch
            CTX.failures += 1
            print(f"{archive}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1 if CTX.failures else 0


def cmd_list(args: argparse.Namespace) -> int:
    rows: list[dict] = []
    failures = 0
    for raw in args.paths:
        archive = Path(raw).expanduser()
        if not require(archive, "archive"):
            failures += 1
            continue
        try:
            fmt = detect(archive)
            members = list_members(archive, fmt)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"{archive}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        total = sum(member.size for member in members)
        if not CTX.json:
            say(f"{archive}  {fmt.name}  {len(members)} member(s)  {human(total)} uncompressed")
        for member in members:
            rows.append({"archive": str(archive), "format": fmt.name, "name": member.name,
                         "size": member.size, "dir": member.is_dir})
            if not CTX.json:
                if args.long:
                    say(f"  {human(member.size):>10}  {member.name}")
                else:
                    say(f"  {member.name}")
    if CTX.json:
        print(json.dumps(rows, indent=2))
    return 1 if failures else 0


def cmd_info(args: argparse.Namespace) -> int:
    rows: list[dict] = []
    failures = 0
    for raw in args.paths:
        archive = Path(raw).expanduser()
        if not require(archive, "archive"):
            failures += 1
            continue
        try:
            fmt = detect(archive)
            stored = archive.stat().st_size
            members = list_members(archive, fmt) if fmt.kind != "stream" else []
            raw_size = estimate_size(archive, fmt)
            if raw_size is None and fmt.kind != "stream":
                raw_size = sum(member.size for member in members)
            row = {
                "path": str(archive),
                "format": fmt.name,
                "kind": fmt.kind,
                "codec": fmt.codec or fmt.name,
                "bytes": stored,
                "members": len(members) or 1,
                "uncompressed": raw_size,
                "ratio": round(raw_size / stored, 2) if raw_size and stored else None,
            }
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"{archive}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        rows.append(row)
        if not CTX.json:
            ratio = f"{row['ratio']}x" if row["ratio"] else "-"
            say(f"{archive}  {row['format']}  {row['members']} member(s)  "
                f"{human(stored)} -> {human(raw_size) if raw_size else '?'} ({ratio})")
    if CTX.json:
        print(json.dumps(rows, indent=2))
    return 1 if failures else 0


def cmd_check(args: argparse.Namespace) -> int:
    failures = 0
    for raw in args.paths:
        archive = Path(raw).expanduser()
        if not require(archive, "archive"):
            failures += 1
            continue
        try:
            fmt = detect(archive)
            if fmt.kind == "zip":
                with zipfile.ZipFile(archive) as zf:
                    bad = zf.testzip()
                if bad is not None:
                    raise RuntimeError(f"corrupt member {bad}")
                count = len(list_members(archive, fmt))
            elif fmt.kind == "tar":
                count = 0
                with open_tar(archive, fmt) as tar:
                    for member in tar:
                        if member.isfile():
                            handle = tar.extractfile(member)
                            if handle is not None:
                                while handle.read(CHUNK):
                                    pass
                        count += 1
            elif fmt.kind == "7z":
                if _py7zr is None:
                    raise RuntimeError("7z support needs py7zr: pip install py7zr")
                with _py7zr.SevenZipFile(archive, "r") as zf:
                    zf.test()
                    count = len(zf.getnames())
            else:
                codec = stream_codec(fmt.codec)
                count = 0
                with archive.open("rb") as fin:
                    stream = codec.read(fin)
                    try:
                        while block := stream.read(CHUNK):
                            count += len(block)
                    finally:
                        stream.close()
            say(f"{archive}  ok  {fmt.name}  {count} member(s)")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"{archive}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1 if failures else 0


def cmd_split(args: argparse.Namespace) -> int:
    source = Path(args.paths[0]).expanduser()
    if not require(source, "archive"):
        return 1
    fmt = detect(source)
    members = list_members(source, fmt)
    if not members:
        print(f"{source}: no members", file=sys.stderr)
        return 1
    parts = args.parts or 0
    if parts < 1:
        print("--parts must be >= 1", file=sys.stderr)
        return 2
    if parts > len(members):
        say(f"only {len(members)} member(s): writing {len(members)} part(s)")
        parts = len(members)
    out_dir = args.output_dir or source.parent
    target_fmt = resolve_format(args.format) if args.format != "auto" else fmt
    base = source.name
    for suffix in fmt.suffixes:
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    groups: list[list[Member]] = [[] for _ in range(parts)]
    for index, member in enumerate(members):
        groups[index % parts].append(member)
    written: list[Path] = []
    for number, group in enumerate(groups, 1):
        target = out_dir / f"{base}.part{number:02d}{target_fmt.suffix}"
        _write_group(source, fmt, group, target, target_fmt, args.level)
        written.append(target)
        say(f"{target}  {len(group)} member(s)")
    return 0


def _write_group(source: Path, source_fmt: Format, members: Sequence[Member], target: Path,
                 target_fmt: Format, level: int) -> None:
    def writer(tmp: Path) -> None:
        if target_fmt.kind != "tar":
            raise ValueError("split writes tar containers; pick --format tar*")
        raw = tmp.open("wb")
        stream = stream_codec(target_fmt.codec).write(raw, level) if target_fmt.codec else None
        try:
            box = stream if stream is not None else raw
            with tarfile.open(fileobj=box, mode="w|") as tar:
                for member in members:
                    _copy_member(tar, source, member, member.name)
        finally:
            if stream is not None:
                stream.close()
            raw.close()

    deliver(target, writer, suffix=target_fmt.suffix)


def cmd_remove(args: argparse.Namespace) -> int:
    archive = Path(args.paths[0]).expanduser()
    if not require(archive, "archive"):
        return 1
    fmt = detect(archive)
    doomed = set(args.members)
    members = list_members(archive, fmt)
    keep = [member for member in members if member.name not in doomed
            and not any(fnmatch.fnmatch(member.name, pattern) for pattern in doomed)]
    if len(keep) == len(members) and not args.force:
        print(f"{archive}: no member matched {', '.join(sorted(doomed))}", file=sys.stderr)
        return 1
    target = Path(args.output) if args.output else archive
    dest_fmt = resolve_format(args.format) if args.format != "auto" else fmt

    def writer(tmp: Path) -> None:
        if dest_fmt.kind == "tar":
            raw = tmp.open("wb")
            stream = stream_codec(dest_fmt.codec).write(raw, args.level) if dest_fmt.codec else None
            try:
                box = stream if stream is not None else raw
                with tarfile.open(fileobj=box, mode="w|") as tar:
                    for member in keep:
                        _copy_member(tar, archive, member, member.name)
            finally:
                if stream is not None:
                    stream.close()
                raw.close()
        else:
            with tempfile.TemporaryDirectory(prefix="archive-remove-") as tmpdir:
                extract_archive(archive, Path(tmpdir), fmt=fmt, patterns=[m.name for m in keep])
                create_archive(tmp, contents(Path(tmpdir)), dest_fmt, args.level, (), True)

    deliver(target, writer, suffix=dest_fmt.suffix)
    say(f"{archive}  removed {len(members) - len(keep)} member(s)  kept {len(keep)}")
    return 0


def cmd_merge(args: argparse.Namespace) -> int:
    inputs = [Path(path).expanduser() for path in args.paths]
    if not inputs:
        print("nothing to merge", file=sys.stderr)
        return 1
    missing = [item for item in inputs if not item.exists()]
    if missing:
        for item in missing:
            print(f"{item}: no such archive", file=sys.stderr)
        return 1
    target = Path(args.output)
    fmt = resolve_format(args.format) if args.format != "auto" else (
        detect(inputs[0]) if detect(inputs[0]).kind == "tar" else pick_default_format())
    merge_archives(inputs, target, fmt, args.level)
    say(f"merged {len(inputs)} archive(s) -> {target}")
    return 0


def cmd_convert(args: argparse.Namespace) -> int:
    source = Path(args.paths[0]).expanduser()
    source_fmt = detect(source)
    target_fmt = resolve_format(args.format) if args.format not in ("auto",) else None
    if target_fmt is None:
        target_fmt = FORMATS["tar.xz"] if source.suffix == ".whl" else pick_default_format()
    output = Path(args.output) if args.output else source.with_name(
        source.name[: -len(source_fmt.suffix)] + target_fmt.suffix
        if source.name.endswith(source_fmt.suffix) else source.name + target_fmt.suffix)
    with tempfile.TemporaryDirectory(prefix="archive-convert-") as tmpdir:
        extract_archive(source, Path(tmpdir), fmt=source_fmt)
        sources = contents(Path(tmpdir))
        if not sources:
            print(f"{source}: empty archive", file=sys.stderr)
            return 1
        count, written = create_archive(output, sources, target_fmt, args.level, (), True)
    say(f"{source} [{source_fmt.name}] -> {output} [{target_fmt.name}]  {count} member(s)  {human(written)}")
    return 0


def cmd_fix(args: argparse.Namespace) -> int:
    root = Path(args.paths[0]).expanduser()
    if not root.is_dir():
        print(f"{root}: not a directory", file=sys.stderr)
        return 1
    candidates = sorted((path for path in root.rglob("*") if path.is_dir()),
                        key=lambda path: len(path.parts), reverse=True)
    found = 0
    for directory in candidates:
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        if len(entries) != 1 or not entries[0].is_file() or entries[0].name != directory.name:
            continue
        found += 1
        held = entries[0]
        say(f"{'[fix]' if args.apply else '[dry]'} {directory}/ -> {held.name}")
        if not args.apply or CTX.dry_run:
            continue
        staged = directory.parent / (directory.name + ".fixtmp")
        with contextlib.suppress(OSError):
            shutil.rmtree(staged, ignore_errors=True)
        shutil.move(str(directory), str(staged))
        shutil.move(str(staged / directory.name), str(directory.parent / directory.name))
        staged.rmdir()
    say(f"{found} issue(s) {'fixed' if args.apply and not CTX.dry_run else 'found'}")
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    root = Path(args.paths[0]).expanduser() if args.paths else Path.cwd()
    if not root.exists():
        print(f"{root}: no such path", file=sys.stderr)
        return 1
    names = args.formats or ["tar", "tar.gz", "tar.xz", "tar.zst", "tar.bz2", "zip"]
    rows: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="archive-bench-") as tmpdir:
        for name in names:
            fmt = resolve_format(name)
            target = Path(tmpdir) / f"bench{fmt.suffix}"
            started = time.perf_counter()
            try:
                count, written = create_archive(target, [root], fmt, args.level, (), True)
            except Exception as exc:  # noqa: BLE001 - a missing backend is a row
                rows.append({"format": fmt.name, "error": f"{type(exc).__name__}: {exc}"})
                continue
            elapsed = time.perf_counter() - started
            raw = sum(member.size for member in list_members(target, fmt)) if fmt.kind != "stream" else 0
            rows.append({
                "format": fmt.name,
                "members": count,
                "bytes": written,
                "elapsed": round(elapsed, 2),
                "ratio": round(raw / written, 2) if raw and written else None,
                "mb_s": round(written / max(elapsed, 1e-9) / (1 << 20), 1),
            })
    if CTX.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"tree: {root}\n")
    print(f"{'format':<10}{'members':>8}{'size':>12}{'seconds':>9}{'ratio':>8}")
    for row in rows:
        if "error" in row:
            print(f"{row['format']:<10}  {row['error']}")
            continue
        ratio = f"{row['ratio']}x" if row["ratio"] else "-"
        print(f"{row['format']:<10}{row['members']:>8}{human(row['bytes']):>12}"
              f"{row['elapsed']:>9.2f}{ratio:>8}")
    return 0 if all("error" not in row for row in rows) else 1


def cmd_doctor(_args: argparse.Namespace) -> int:
    print(f"{PROG} {__version__}  python {sys.version.split()[0]}  ({os.cpu_count()} cpu)\n")
    print(f"{'format':<10}{'suffix':<10}{'kind':<7}{'backend':<26}status")
    for name, fmt in FORMATS.items():
        backend = fmt.hint
        status = "ok"
        if fmt.codec in ("zstd",) and _zstd is None:
            status = "missing wheel"
        if fmt.codec == "brotli" and _brotli is None:
            status = "missing wheel"
        if fmt.codec == "lz4" and _lz4_frame is None:
            status = "missing wheel"
        if fmt.kind == "7z" and _py7zr is None:
            status = "missing wheel"
        print(f"{name:<10}{fmt.suffix:<10}{fmt.kind:<7}{backend:<26}{status}")
    hints = []
    if _zstd is None:
        hints.append("pip install zstandard")
    if _brotli is None:
        hints.append("pip install brotli")
    if _lz4_frame is None:
        hints.append("pip install lz4")
    if _py7zr is None:
        hints.append("pip install py7zr")
    if hints:
        print("\nto unlock more formats: " + ", ".join(hints))
    return 0


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


class _ListAction(argparse.Action):
    """``--formats tar,zip --formats 7z`` -> ["tar", "zip", "7z"]."""

    def __call__(self, parser, namespace, values, option_string=None):
        current = list(getattr(namespace, self.dest, None) or [])
        for piece in str(values).replace(" ", "").split(","):
            if piece:
                current.append(piece)
        setattr(namespace, self.dest, current)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-n", "--dry-run", action="store_true", help="report what would be written")
    common.add_argument("--backup", action="store_true", help="keep a .bak of overwritten archives")
    common.add_argument("-f", "--force", action="store_true", help="overwrite / ignore missing matches")
    common.add_argument("-q", "--quiet", action="store_true")
    common.add_argument("-v", "--verbose", action="store_true")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    common.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 1))
    common.add_argument("-l", "--level", type=int, default=-1, help="codec level (clamped per backend)")

    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Unified archive toolkit: tar, tar.gz/bz2/xz/zst/br/lz4, zip, 7z.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "was: 7ziper archive_convert archive_dir archive_merger archive_tools artools br brer\n"
            "     compress_subdirs py7zmax rm_from_archive smart_archiver trydecomp xreport xtar\n"
            "\n"
            "examples:\n"
            "  %(prog)s pack src/ -o dist/ --format tar.zst\n"
            "  %(prog)s pack vendor/ --per-subdir --merge vendor.tar.zst --cleanup\n"
            "  %(prog)s unpack dist/src.tar.zst -o /tmp/out\n"
            "  %(prog)s list dist/src.tar.zst --long\n"
            "  %(prog)s split big.tar.zst --parts 4\n"
            "  %(prog)s convert pkg.whl --format tar.xz\n"
            "  %(prog)s bench . --formats tar.gz,tar.xz,tar.zst,zip\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subs = parser.add_subparsers(dest="command", required=True)

    def add_paths(sub: argparse.ArgumentParser, metavar: str = "PATH") -> None:
        sub.add_argument("paths", nargs="*", type=Path, metavar=metavar)

    pack = subs.add_parser("pack", parents=[common], help="create an archive")
    add_paths(pack, "PATH")
    pack.add_argument("-o", "--output-dir", type=Path, default=None, metavar="DIR")
    pack.add_argument("--output", type=Path, default=None, metavar="FILE", help="exact output file")
    pack.add_argument("--format", default="auto")
    pack.add_argument("--per-subdir", action="store_true", help="one archive per immediate subdirectory")
    pack.add_argument("--merge", action="store_true", help="merge the per-subdir archives into one")
    pack.add_argument("--cleanup", action="store_true", help="remove the sources after a verified pack")
    pack.add_argument("--no-compress", action="store_true", help="store members without deflating")

    unpack = subs.add_parser("unpack", parents=[common], help="extract an archive")
    add_paths(unpack, "ARCHIVE")
    unpack.add_argument("-o", "--output-dir", type=Path, default=None)
    unpack.add_argument("--format", default="auto")
    unpack.add_argument("--strip-components", type=int, default=0, metavar="N")
    unpack.add_argument("--members", action="append", default=[], metavar="GLOB")
    unpack.add_argument("--skip-existing", action="store_true")

    listing = subs.add_parser("list", parents=[common], help="list members")
    add_paths(listing, "ARCHIVE")
    listing.add_argument("--long", action="store_true", help="include sizes")

    info = subs.add_parser("info", parents=[common], help="format, members and ratio")
    add_paths(info, "ARCHIVE")

    check = subs.add_parser("check", parents=[common], help="verify integrity without extracting")
    add_paths(check, "ARCHIVE")

    split = subs.add_parser("split", parents=[common], help="split an archive into N parts")
    add_paths(split, "ARCHIVE")
    split.add_argument("--parts", type=int, default=2, metavar="N")
    split.add_argument("-o", "--output-dir", type=Path, default=None)
    split.add_argument("--format", default="auto")

    remove = subs.add_parser("remove", parents=[common], help="drop members and rewrite the archive")
    add_paths(remove, "ARCHIVE")
    remove.add_argument("members", nargs="+", metavar="MEMBER")
    remove.add_argument("-o", "--output", type=Path, default=None)
    remove.add_argument("--format", default="auto")

    merge = subs.add_parser("merge", parents=[common], help="merge archives into one")
    add_paths(merge, "ARCHIVE")
    merge.add_argument("-o", "--output", type=Path, required=True)
    merge.add_argument("--format", default="auto")

    convert = subs.add_parser("convert", parents=[common], help="re-pack into another container")
    add_paths(convert, "ARCHIVE")
    convert.add_argument("--format", default="auto")
    convert.add_argument("-o", "--output", type=Path, default=None)

    fix = subs.add_parser("fix", parents=[common], help="repair dir/name/name misextraction")
    add_paths(fix, "ROOT")
    fix.add_argument("--apply", action="store_true", help="rename for real (default: report only)")

    bench = subs.add_parser("bench", parents=[common], help="pack the same tree in every format")
    add_paths(bench, "PATH")
    bench.add_argument("--formats", action=_ListAction, metavar="LIST")

    subs.add_parser("doctor", help="which backends are usable here")
    return parser


HANDLERS = {
    "pack": cmd_pack,
    "unpack": cmd_unpack,
    "list": cmd_list,
    "info": cmd_info,
    "check": cmd_check,
    "split": cmd_split,
    "remove": cmd_remove,
    "merge": cmd_merge,
    "convert": cmd_convert,
    "fix": cmd_fix,
    "bench": cmd_bench,
    "doctor": cmd_doctor,
}


def emit_json(command: str) -> None:
    wrote = sum(1 for item in CTX.files if item.get("action") == "wrote")
    planned = sum(1 for item in CTX.files if item.get("action") == "would-write")
    payload = {
        "tool": PROG,
        "version": __version__,
        "command": command,
        "elapsed": round(time.perf_counter() - CTX.started, 3),
        "counts": {"files": len(CTX.files), "written": wrote, "planned": planned, "failed": CTX.failures},
        "files": CTX.files,
    }
    print(json.dumps(payload, indent=2))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    CTX.command = args.command
    for flag in ("dry_run", "backup", "force", "quiet", "verbose", "json"):
        setattr(CTX, flag, getattr(args, flag, False))
    if args.command == "doctor":
        return cmd_doctor(args)
    try:
        code = HANDLERS[args.command](args)
    except KeyboardInterrupt:
        return 130
    if CTX.json and args.command in ("pack", "unpack", "split", "remove", "merge", "convert"):
        emit_json(args.command)
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
