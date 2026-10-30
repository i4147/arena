#!/usr/bin/env python3
"""codecs.py — one compression front end for the eleven single-stream tools in g005.

g005 held two families of scripts wearing twenty-four names:

    single streams   gzbz, lz4er, zstdtools, xztools, brer, compress_text,
                     fast_compress, compresstools, smart_archiver,
                     compression_benchmark, xcompress (single-file half)
    containers       tar / zip / 7z packing, merging and extraction — those
                     live in ``archive.py`` next door

This file owns the first family: *one file in, one file out*.  Every codec is
optional and probed at run time, so ``codecs.py doctor`` always tells the truth
about what this machine can do:

    gzip bz2 xz lzma zlib        stdlib, always present
    zstd brotli lz4 snappy ppmd  python wheels when installed, CLI when not
    zopfli                       optional, slower, smaller

    codecs.py compress notes.txt -a zstd --level 19 --verify
    codecs.py compress logs/ -r -a gzip -j 8 --remove-source
    codecs.py decompress notes.txt.zst -o out/
    codecs.py bench corpus.txt -a gzip,bz2,xz,zstd,brotli --json
    codecs.py inspect mystery.bin
    codecs.py doctor

Streams are written through a temporary file and renamed, so an interrupted run
never leaves a half-written ``.zst`` behind.  Codecs without a streaming API
(ppmd, zopfli, snappy) and any codec asked for ``--chunk-size`` use a framed
container that stores independent blocks; the frame records the codec name, so
``decompress`` still works after a rename.

Exit codes: 0 ok · 1 errors · 2 usage · 130 interrupted
"""

from __future__ import annotations

import argparse
import bz2
import contextlib
import fnmatch
import gzip
import hashlib
import importlib
import json
import lzma
import os
import shutil
import struct
import subprocess
import sys
import time
import zlib
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, ContextManager

__version__ = "1.0.0"
PROG = "codecs.py"

CHUNK = 1 << 20
FRAME_MAGIC = b"PYCHUNK\x01"
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", ".venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".cache", "target", "dist", "build",
}
ALREADY_COMPRESSED = {
    ".gz", ".bz2", ".xz", ".lzma", ".zst", ".br", ".lz4", ".sz", ".ppmd", ".zz", ".zip",
    ".7z", ".rar", ".whl", ".jar", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp3",
    ".mp4", ".mkv", ".mov", ".pdf", ".docx", ".xlsx", ".pptx",
}


# --------------------------------------------------------------------------- #
# optional backends                                                            #
# --------------------------------------------------------------------------- #


def _optional(name: str):
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 - a missing wheel is normal, not an error
        return None


_zstd = _optional("zstandard")
_brotli = _optional("brotli")
_lz4_frame = _optional("lz4.frame")
_snappy = _optional("snappy")
_pyppmd = _optional("pyppmd")
_zopfli = _optional("zopfli")


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _tool(name: str, *probe: str) -> str | None:
    for candidate in probe:
        if have(candidate):
            return candidate
    del name
    return None


# --------------------------------------------------------------------------- #
# adapters: raw sink/source -> compressed stream                               #
# --------------------------------------------------------------------------- #


class _BlockWriter:
    """Push-based codec (compressobj / brotli / snappy) dressed as a file."""

    def __init__(self, sink: BinaryIO, compress: Callable[[bytes], bytes], finish: Callable[[], bytes]):
        self._sink = sink
        self._compress = compress
        self._finish = finish
        self._closed = False

    def write(self, data: bytes) -> int:
        payload = self._compress(data)
        if payload:
            self._sink.write(payload)
        return len(data)

    def flush(self) -> None:
        self._sink.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tail = self._finish()
        if tail:
            self._sink.write(tail)


class _BlockReader:
    """Pull-based codec dressed as a readable file."""

    def __init__(self, source: BinaryIO, decompress: Callable[[bytes], bytes], flush: Callable[[], bytes]):
        self._source = source
        self._decompress = decompress
        self._flush = flush
        self._buffer = bytearray()
        self._eof = False
        self._done = False
        self._read = getattr(source, "read1", source.read)

    def read(self, size: int = -1) -> bytes:
        while True:
            if size is not None and size >= 0 and len(self._buffer) >= size:
                out = bytes(self._buffer[:size])
                del self._buffer[:size]
                return out
            if self._eof:
                if not self._done:
                    self._done = True
                    tail = self._flush()
                    if tail:
                        self._buffer.extend(tail)
                    if not size or size < 0:
                        out = bytes(self._buffer)
                        self._buffer.clear()
                        return out
                    continue
                out = bytes(self._buffer)
                self._buffer.clear()
                return out
            blob = self._read(CHUNK)
            if not blob:
                self._eof = True
                continue
            self._buffer.extend(self._decompress(blob))

    def close(self) -> None:
        self._source.close()


@contextlib.contextmanager
def _closing(obj):
    try:
        yield obj
    finally:
        with contextlib.suppress(Exception):
            obj.close()


# --------------------------------------------------------------------------- #
# codec registry                                                               #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Codec:
    name: str
    ext: str
    low: int
    high: int
    default: int
    hint: str
    open_write: Callable[[BinaryIO, int], ContextManager]
    open_read: Callable[[BinaryIO], ContextManager]
    once: Callable[[bytes, int], bytes]
    once_decode: Callable[[bytes], bytes]
    magic: tuple[bytes, ...] = ()
    aliases: tuple[str, ...] = ()
    chunked_only: bool = False


def _gzip_w(raw: BinaryIO, level: int):
    return _closing(gzip.GzipFile(filename="", fileobj=raw, mode="wb", compresslevel=level, mtime=0))


def _gzip_r(raw: BinaryIO):
    return _closing(gzip.GzipFile(filename="", fileobj=raw, mode="rb"))


def _bz2_w(raw: BinaryIO, level: int):
    return _closing(bz2.BZ2File(raw, "wb", compresslevel=level))


def _bz2_r(raw: BinaryIO):
    return _closing(bz2.BZ2File(raw, "rb"))


def _lzma_w(fmt: int):
    def open_write(raw: BinaryIO, level: int):
        return _closing(lzma.LZMAFile(raw, "wb", format=fmt, preset=level))

    return open_write


def _lzma_r(raw: BinaryIO):
    return _closing(lzma.LZMAFile(raw, "rb"))


def _zlib_w(raw: BinaryIO, level: int):
    engine = zlib.compressobj(level, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    return _closing(_BlockWriter(raw, engine.compress, engine.flush))


def _zlib_r(raw: BinaryIO):
    engine = zlib.decompressobj(16 + zlib.MAX_WBITS)
    return _closing(_BlockReader(raw, engine.decompress, engine.flush))


def _zstd_w(raw: BinaryIO, level: int):
    return _closing(_zstd.ZstdCompressor(level=level).stream_writer(raw, closefd=False))


def _zstd_r(raw: BinaryIO):
    return _closing(_zstd.ZstdDecompressor().stream_reader(raw, closefd=False))


def _brotli_w(raw: BinaryIO, level: int):
    engine = _brotli.Compressor(quality=level)
    return _closing(_BlockWriter(raw, engine.process, engine.finish))


def _brotli_r(raw: BinaryIO):
    engine = _brotli.Decompressor()
    return _closing(_BlockReader(raw, engine.process, lambda: b""))


def _lz4_w(raw: BinaryIO, level: int):
    return _closing(_lz4_frame.open(raw, "wb", compression_level=level))


def _lz4_r(raw: BinaryIO):
    return _closing(_lz4_frame.open(raw, "rb"))


def _snappy_w(raw: BinaryIO, level: int):
    engine = _snappy.StreamCompressor()
    return _closing(_BlockWriter(raw, engine.add_chunk, lambda: b""))


def _snappy_r(raw: BinaryIO):
    engine = _snappy.StreamDecompressor()
    return _closing(_BlockReader(raw, engine.decompress, lambda: b""))


def _no_stream(name: str):
    def unusable(*_args, **_kwargs):
        raise RuntimeError(f"{name} has no streaming API; it is written as framed chunks")

    return unusable


CODECS: dict[str, Codec] = {}


def _register(codec: Codec) -> None:
    CODECS[codec.name] = codec
    for alias in codec.aliases:
        CODECS[alias] = codec


_register(Codec(
    "gzip", ".gz", 0, 9, 6, "stdlib gzip",
    _gzip_w, _gzip_r,
    lambda data, level: gzip.compress(data, compresslevel=level, mtime=0),
    gzip.decompress,
    magic=(b"\x1f\x8b",), aliases=("gz",),
))

_register(Codec(
    "bz2", ".bz2", 1, 9, 9, "stdlib bz2",
    _bz2_w, _bz2_r,
    lambda data, level: bz2.compress(data, compresslevel=level),
    bz2.decompress,
    magic=(b"BZh",), aliases=("bzip2", "bz"),
))

_register(Codec(
    "xz", ".xz", 0, 9, 6, "stdlib lzma (xz container)",
    _lzma_w(lzma.FORMAT_XZ), _lzma_r,
    lambda data, level: lzma.compress(data, format=lzma.FORMAT_XZ, preset=level),
    lambda blob: lzma.decompress(blob),
    magic=(b"\xfd7zXZ\x00",), aliases=("lzma2",),
))

_register(Codec(
    "lzma", ".lzma", 0, 9, 6, "stdlib lzma (legacy .lzma)",
    _lzma_w(lzma.FORMAT_ALONE), _lzma_r,
    lambda data, level: lzma.compress(data, format=lzma.FORMAT_ALONE, preset=level),
    lambda blob: lzma.decompress(blob, format=lzma.FORMAT_ALONE),
    aliases=("lz",),
))

_register(Codec(
    "zlib", ".zz", 0, 9, 6, "stdlib zlib",
    _zlib_w, _zlib_r,
    lambda data, level: zlib.compress(data, level),
    zlib.decompress,
    aliases=("deflate",),
))

if _zstd is not None:
    _register(Codec(
        "zstd", ".zst", 1, 22, 3, "zstandard",
        _zstd_w, _zstd_r,
        lambda data, level: _zstd.ZstdCompressor(level=level).compress(data),
        lambda blob: _zstd.ZstdDecompressor().decompress(blob, max_output_size=1 << 31),
        magic=(b"\x28\xb5\x2f\xfd",), aliases=("zst",),
    ))
elif have("zstd"):
    _register(Codec(
        "zstd", ".zst", 1, 22, 3, "zstd CLI",
        lambda raw, level: _cli_writer(["zstd", "-q", "-c", f"-{level}"]),
        lambda raw: _cli_reader(["zstd", "-d", "-q", "-c"]),
        lambda data, level: subprocess.run(["zstd", "-q", "-c", f"-{level}"], input=data,
                                           capture_output=True, check=True).stdout,
        lambda blob: subprocess.run(["zstd", "-d", "-q", "-c"], input=blob,
                                    capture_output=True, check=True).stdout,
        magic=(b"\x28\xb5\x2f\xfd",), aliases=("zst",),
    ))

if _brotli is not None:
    _register(Codec(
        "brotli", ".br", 0, 11, 5, "brotli",
        _brotli_w, _brotli_r,
        lambda data, level: _brotli.compress(data, quality=level),
        _brotli.decompress,
        aliases=("br",),
    ))
elif have("brotli"):
    _register(Codec(
        "brotli", ".br", 0, 11, 5, "brotli CLI",
        lambda raw, level: _cli_writer(["brotli", "-q", str(level), "-c"]),
        lambda raw: _cli_reader(["brotli", "-d", "-c"]),
        lambda data, level: subprocess.run(["brotli", "-q", str(level), "-c"], input=data,
                                           capture_output=True, check=True).stdout,
        lambda blob: subprocess.run(["brotli", "-d", "-c"], input=blob,
                                    capture_output=True, check=True).stdout,
        aliases=("br",),
    ))

if _lz4_frame is not None:
    _register(Codec(
        "lz4", ".lz4", 0, 16, 0, "lz4.frame",
        _lz4_w, _lz4_r,
        lambda data, level: _lz4_frame.compress(data, compression_level=level),
        _lz4_frame.decompress,
        magic=(b"\x04\x22\x4d\x18",),
    ))
elif have("lz4"):
    _register(Codec(
        "lz4", ".lz4", 0, 16, 0, "lz4 CLI",
        lambda raw, level: _cli_writer(["lz4", "-q", f"-{level}", "-c"]),
        lambda raw: _cli_reader(["lz4", "-d", "-q", "-c"]),
        lambda data, level: subprocess.run(["lz4", "-q", f"-{level}", "-c"], input=data,
                                           capture_output=True, check=True).stdout,
        lambda blob: subprocess.run(["lz4", "-d", "-q", "-c"], input=blob,
                                    capture_output=True, check=True).stdout,
        magic=(b"\x04\x22\x4d\x18",),
    ))

if _snappy is not None:
    _register(Codec(
        "snappy", ".sz", 0, 0, 0, "python-snappy",
        _snappy_w, _snappy_r,
        lambda data, level: _snappy.compress(data),
        _snappy.decompress,
        magic=(b"\xff\x06\x00\x00sNaPpY",),
    ))

if _pyppmd is not None:
    _register(Codec(
        "ppmd", ".ppmd", 1, 9, 6, "pyppmd (framed blocks)",
        _no_stream("ppmd"), _no_stream("ppmd"),
        lambda data, level: _pyppmd.compress(data, max_order=level),
        _pyppmd.decompress,
        chunked_only=True,
    ))

if _zopfli is not None:
    _register(Codec(
        "zopfli", ".zopfli.gz", 1, 49, 15, "zopfli (slow, framed blocks)",
        _no_stream("zopfli"), _no_stream("zopfli"),
        lambda data, level: _zopfli.gzip.compress(data, numiterations=level, blocksplitting=True),
        gzip.decompress,
        chunked_only=True,
    ))


def _cli_writer(argv: list[str]):
    process = subprocess.Popen(argv, stdin=subprocess.PIPE)  # noqa: S603 - argv is fixed
    assert process.stdin is not None

    class _Pipe:
        def write(self, data: bytes) -> int:
            return process.stdin.write(data)  # type: ignore[union-attr]

        def flush(self) -> None:
            process.stdin.flush()  # type: ignore[union-attr]

        def close(self) -> None:
            process.stdin.close()  # type: ignore[union-attr]
            if process.wait() != 0:
                raise RuntimeError(f"{argv[0]} failed with exit {process.returncode}")

    return _closing(_Pipe())


def _cli_reader(argv: list[str]):
    process = subprocess.Popen(argv, stdout=subprocess.PIPE)  # noqa: S603 - argv is fixed
    assert process.stdout is not None

    class _Pipe:
        def read(self, size: int = -1) -> bytes:
            return process.stdout.read(size)  # type: ignore[union-attr]

        def close(self) -> None:
            process.stdout.close()  # type: ignore[union-attr]
            if process.wait() != 0:
                raise RuntimeError(f"{argv[0]} failed with exit {process.returncode}")

    return _closing(_Pipe())


def resolve_codec(name: str) -> Codec:
    key = name.strip().lower()
    if key not in CODECS:
        known = ", ".join(sorted(CODECS))
        raise SystemExit(f"unknown codec {name!r}; known: {known}")
    return CODECS[key]


def available_codecs() -> list[str]:
    seen: list[str] = []
    for codec in CODECS.values():
        if codec.name not in seen:
            seen.append(codec.name)
    return sorted(seen)


def clamp(codec: Codec, level: int) -> int:
    if level < 0:
        return codec.default
    return max(codec.low, min(codec.high, level))


# --------------------------------------------------------------------------- #
# framed chunks (codecs without streaming, or --chunk-size)                    #
# --------------------------------------------------------------------------- #


def chunked_compress(codec: Codec, src: Path, dst: Path, level: int, chunk_size: int) -> int:
    """Write MAGIC + codec name + [u64 len][payload]...
    Each block is compressed independently, so the file is seekable-ish and a
    damaged block only costs the block.
    """
    blocks = 0
    with src.open("rb") as fin, dst.open("wb") as fout:
        name = codec.name.encode("ascii")
        fout.write(FRAME_MAGIC + bytes([len(name)]) + name)
        while True:
            block = fin.read(chunk_size)
            if not block:
                break
            payload = codec.once(block, level)
            fout.write(struct.pack(">Q", len(payload)))
            fout.write(payload)
            blocks += 1
    return blocks


def chunked_decompress(src: Path, dst: Path) -> tuple[str, int]:
    """Returns (codec name, block count); raises ValueError on a broken frame."""
    blocks = 0
    with src.open("rb") as fin, dst.open("wb") as fout:
        header = fin.read(len(FRAME_MAGIC) + 1)
        if not header.startswith(FRAME_MAGIC):
            raise ValueError("not a chunked stream")
        name_len = header[len(FRAME_MAGIC)]
        name = fin.read(name_len).decode("ascii")
        codec = CODECS.get(name)
        if codec is None:
            raise ValueError(f"chunked stream needs codec {name!r}, which is not available")
        while True:
            size_raw = fin.read(8)
            if not size_raw:
                break
            if len(size_raw) != 8:
                raise ValueError("truncated block header")
            size = struct.unpack(">Q", size_raw)[0]
            payload = fin.read(size)
            if len(payload) != size:
                raise ValueError("truncated block payload")
            fout.write(codec.once_decode(payload))
            blocks += 1
    return name, blocks


def is_chunked(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(FRAME_MAGIC)) == FRAME_MAGIC
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# magic sniffing (extension -> magic -> library probe)                         #
# --------------------------------------------------------------------------- #


MAGIC_TABLE: tuple[tuple[bytes, int, str], ...] = (
    (b"PK\x03\x04", 0, "zip"),
    (b"PK\x05\x06", 0, "zip (empty)"),
    (b"7z\xbc\xaf\x27\x1c", 0, "7z"),
    (b"Rar!\x1a\x07", 0, "rar"),
    (b"\x1f\x8b", 0, "gzip"),
    (b"BZh", 0, "bz2"),
    (b"\xfd7zXZ\x00", 0, "xz"),
    (b"\x28\xb5\x2f\xfd", 0, "zstd"),
    (b"\x04\x22\x4d\x18", 0, "lz4"),
    (b"\x02\x21\x4c\x18", 0, "lz4 (legacy)"),
    (b"\xff\x06\x00\x00sNaPpY", 0, "snappy (framed)"),
    (b"ustar", 257, "tar"),
    (b"!<arch>\n", 0, "ar"),
)


def sniff(path: Path) -> tuple[str, str]:
    """(codec/format name, confidence) — extension first, then magic bytes."""
    suffix = path.suffix.lower()
    by_ext = {
        ".gz": "gzip", ".tgz": "gzip", ".bz2": "bz2", ".tbz2": "bz2", ".xz": "xz",
        ".txz": "xz", ".lzma": "lzma", ".zst": "zstd", ".zstd": "zstd", ".br": "brotli",
        ".lz4": "lz4", ".sz": "snappy", ".ppmd": "ppmd", ".zz": "zlib", ".zip": "zip",
        ".whl": "zip", ".7z": "7z", ".tar": "tar",
    }
    if is_chunked(path):
        return "chunked", "high"
    try:
        with path.open("rb") as handle:
            head = handle.read(512)
    except OSError:
        return "unknown", "low"
    for magic, offset, name in MAGIC_TABLE:
        if head[offset:offset + len(magic)] == magic:
            return name, "high"
    if suffix in by_ext:
        return by_ext[suffix], "medium"
    return "unknown", "low"


def declared_size(path: Path) -> int | None:
    """Uncompressed size from the stream footer/header, without decoding."""
    try:
        with path.open("rb") as handle:
            head = handle.read(64)
            if head[:2] == b"\x1f\x8b":
                handle.seek(-4, os.SEEK_END)
                return struct.unpack("<I", handle.read(4))[0]
            if head[:4] == b"\x28\xb5\x2f\xfd" and _zstd is not None:
                params = _zstd.get_frame_parameters(head)
                size = getattr(params, "content_size", 0) or 0
                return int(size) if size else None
            if head[:4] == b"\x04\x22\x4d\x18":
                flg = head[4]
                if flg & 0x08:
                    return struct.unpack("<Q", head[6:14])[0]
    except Exception:  # noqa: BLE001 - header sniffing is best effort
        return None
    return None


# --------------------------------------------------------------------------- #
# file level helpers                                                           #
# --------------------------------------------------------------------------- #


def human(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(size) < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def human_speed(bytes_per_second: float) -> str:
    return f"{human(bytes_per_second)}/s"


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            sha.update(block)
    return sha.hexdigest()


def strip_suffix(path: Path) -> Path:
    """``notes.txt.zst`` -> ``notes.txt``; unknown suffix -> ``name.out``."""
    for codec in {c.name: c for c in CODECS.values()}.values():
        if path.suffix.lower() == codec.ext:
            stripped = path.with_suffix("")
            return stripped if stripped.name else path.with_name(path.stem + ".out")
    if path.suffix.lower() in (".tgz", ".tbz2", ".txz"):
        return path.with_suffix(".tar")
    return path.with_name(path.name + ".out")


def looks_compressed(path: Path) -> bool:
    return path.suffix.lower() in ALREADY_COMPRESSED


def collect_files(paths: Sequence[Path], *, recursive: bool, exclude: Sequence[str],
                  suffixes: Sequence[str] | None = None) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for raw in paths or [Path.cwd()]:
        path = raw.expanduser()
        if not path.exists():
            print(f"{path}: no such file", file=sys.stderr)
            continue
        if path.is_file():
            candidates: Iterator[Path] = iter([path])
        elif recursive:
            candidates = (p for p in sorted(path.rglob("*")) if p.is_file())
        else:
            candidates = (p for p in sorted(path.glob("*")) if p.is_file())
        for candidate in candidates:
            if candidate.is_symlink():
                continue
            if path.is_dir():
                relative = candidate.relative_to(path).parts[:-1]
                if any(part in SKIP_DIRS for part in relative):
                    continue
            if any(fnmatch.fnmatch(candidate.name, pattern) or fnmatch.fnmatch(str(candidate), pattern)
                   for pattern in exclude):
                continue
            if suffixes is not None and candidate.suffix.lower() not in suffixes:
                continue
            key = candidate.resolve()
            if key not in seen:
                seen.add(key)
                found.append(candidate)
    return found


# --------------------------------------------------------------------------- #
# run context: dry-run, backup, json, quiet                                    #
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


def deliver(target: Path, writer: Callable[[Path], None], *, suffix: str | None = None) -> Path | None:
    if CTX.dry_run:
        say(f"[dry-run] would write {target}")
        CTX.files.append({"path": str(target), "action": "would-write"})
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.stem}.{os.getpid()}.tmp{suffix if suffix is not None else target.suffix}")
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
# compress / decompress                                                        #
# --------------------------------------------------------------------------- #


def compress_file(codec: Codec, src: Path, dst: Path, level: int, chunk_size: int,
                  verify: bool) -> dict:
    level = clamp(codec, level)
    if codec.chunked_only and chunk_size <= 0:
        chunk_size = 8 * CHUNK
    before = src.stat().st_size

    def writer(tmp: Path) -> None:
        if chunk_size > 0:
            chunked_compress(codec, src, tmp, level, chunk_size)
            return
        with src.open("rb") as fin, tmp.open("wb") as fout, codec.open_write(fout, level) as sink:
            shutil.copyfileobj(fin, sink, CHUNK)

    deliver(dst, writer, suffix=dst.suffix or ".bin")
    after = dst.stat().st_size if dst.exists() and not CTX.dry_run else 0
    record = {
        "path": str(src), "output": str(dst), "codec": codec.name, "level": level,
        "bytes_in": before, "bytes_out": after,
        "ratio": round(before / after, 2) if after else None,
    }
    if verify and dst.exists() and not CTX.dry_run:
        ok = verify_file(codec, src, dst, chunk_size)
        record["verified"] = ok
        if not ok:
            raise RuntimeError(f"verification failed for {dst}")
    return record


def verify_file(codec: Codec, src: Path, dst: Path, chunk_size: int) -> bool:
    temporary = dst.with_name(f".{dst.name}.{os.getpid()}.verify")
    try:
        if is_chunked(dst):
            chunked_decompress(dst, temporary)
        else:
            with dst.open("rb") as fin, temporary.open("wb") as fout, codec.open_read(fin) as stream:
                shutil.copyfileobj(stream, fout, CHUNK)
        return digest(src) == digest(temporary)
    finally:
        temporary.unlink(missing_ok=True)


def decompress_file(src: Path, dst: Path, codec: Codec | None, chunk_size: int) -> tuple[str, int]:
    """Returns (codec name, blocks); ``blocks`` is -1 for a plain stream."""
    entry: dict = {}

    def writer(tmp: Path) -> None:
        if is_chunked(src):
            name, blocks = chunked_decompress(src, tmp)
            entry.update(codec=name, blocks=blocks)
            return
        active = codec or resolve_codec(sniff(src)[0])
        with src.open("rb") as fin, tmp.open("wb") as fout, active.open_read(fin) as stream:
            shutil.copyfileobj(stream, fout, CHUNK)
        entry.update(codec=active.name, blocks=-1)

    deliver(dst, writer, suffix=dst.suffix or ".out")
    return entry.get("codec", "?"), entry.get("blocks", -1)


# --------------------------------------------------------------------------- #
# commands                                                                     #
# --------------------------------------------------------------------------- #


def cmd_compress(args: argparse.Namespace) -> int:
    codecs = [resolve_codec(name) for name in (args.algos or ["zstd"])]
    sources = collect_files(args.paths, recursive=args.recursive, exclude=args.exclude)
    if not sources:
        print("no input files", file=sys.stderr)
        return 1
    if args.recursive and not args.no_skip_ext:
        skipped = [p for p in sources if looks_compressed(p)]
        sources = [p for p in sources if not looks_compressed(p)]
        for path in skipped:
            note(f"skip (already compressed): {path}")
    if args.min_size:
        small = [p for p in sources if p.stat().st_size < args.min_size]
        sources = [p for p in sources if p.stat().st_size >= args.min_size]
        for path in small:
            note(f"skip (smaller than {human(args.min_size)}): {path}")
    if not sources:
        say("nothing to do")
        return 0

    jobs: list[tuple[Codec, Path, Path]] = []
    clashes = duplicate_targets([
        (src, (args.output_dir if args.output_dir else src.parent) / f"{src.name}{codec.ext}")
        for src in sources for codec in codecs
    ])
    if clashes:
        for target, origins in clashes.items():
            print(f"output {target} would be written from {len(origins)} inputs: "
                  f"{', '.join(str(path) for path in origins)}", file=sys.stderr)
        print("use -o with distinct names, or compress the directories separately", file=sys.stderr)
        return 2
    skipped = 0
    for src in sources:
        base = args.output_dir if args.output_dir else src.parent
        for codec in codecs:
            target = base / f"{src.name}{codec.ext}"
            if target.exists() and not CTX.force:
                skipped += 1
                note(f"exists, pass --force: {target}")
                continue
            jobs.append((codec, src, target))
    if not jobs:
        say(f"nothing written: {skipped} target(s) already exist (use --force)")
        return 1

    say(f"{len(sources)} file(s) x {len(codecs)} codec(s) = {len(jobs)} output(s)")

    def run(job: tuple[Codec, Path, Path]) -> dict:
        codec, src, target = job
        return compress_file(codec, src, target, args.level, args.chunk_size, args.verify)

    records: list[dict] = []

    def guard(job: tuple[Codec, Path, Path]) -> None:
        try:
            record = run(job)
            records.append(record)
            if not CTX.json:
                if CTX.dry_run:
                    say(f"  {job[1]} -[{record['codec']}]-> {job[2].name}  "
                        f"{human(record['bytes_in'])} (dry-run)")
                else:
                    ratio = f"{record['ratio']}x" if record["ratio"] else "-"
                    say(f"  {job[1]} -[{record['codec']}]-> {job[2].name}  "
                        f"{human(record['bytes_in'])} -> {human(record['bytes_out'])} ({ratio})")
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
            CTX.failures += 1
            print(f"{job[1]}: {type(exc).__name__}: {exc}", file=sys.stderr)

    if args.jobs > 1 and len(jobs) > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            list(pool.map(guard, jobs))
    else:
        for job in jobs:
            guard(job)

    if args.remove_source and not CTX.dry_run:
        produced: dict[str, list[Path]] = {}
        for record in records:
            produced.setdefault(record["path"], []).append(Path(record["output"]))
        for src in sources:
            outputs = produced.get(str(src), [])
            if outputs and all(path.exists() for path in outputs):
                src.unlink(missing_ok=True)
                note(f"removed source {src}")
    return 1 if CTX.failures else 0


def cmd_decompress(args: argparse.Namespace) -> int:
    sources = collect_files(args.paths, recursive=args.recursive, exclude=args.exclude)
    if not sources:
        print("no input files", file=sys.stderr)
        return 1
    codec = resolve_codec(args.codec) if args.codec != "auto" else None
    if codec is None:
        sources = [path for path in sources if is_chunked(path) or sniff(path)[0] in CODECS]
        if not sources:
            print("nothing looks compressed", file=sys.stderr)
            return 1

    targets = {
        src: (args.output_dir / strip_suffix(src).name if args.output_dir else strip_suffix(src))
        for src in sources
    }
    clashes = duplicate_targets([(src, target) for src, target in targets.items()])
    if clashes:
        for target, origins in clashes.items():
            print(f"output {target} would be written from {len(origins)} inputs: "
                  f"{', '.join(str(path) for path in origins)}", file=sys.stderr)
        print("extract one at a time, or move the sources apart", file=sys.stderr)
        return 2

    def run(src: Path) -> tuple[Path, str]:
        target = targets[src]
        if target.exists() and not CTX.force:
            raise FileExistsError(f"{target} exists; pass --force")
        name, blocks = decompress_file(src, target, codec, args.chunk_size)
        if args.verify and target.exists() and not CTX.dry_run and codec is not None:
            if not verify_file(codec, target, src, args.chunk_size):
                raise RuntimeError(f"verification failed for {src}")
        note(f"  {src} -[{name}{'+chunks' if blocks > 0 else ''}]-> {target}")
        return target, name

    def guard(src: Path) -> None:
        try:
            target, name = run(src)
            say(f"{src} -> {target} ({name})")
            if args.remove_source and target.exists() and not CTX.dry_run:
                src.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            CTX.failures += 1
            print(f"{src}: {type(exc).__name__}: {exc}", file=sys.stderr)

    if args.jobs > 1 and len(sources) > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            list(pool.map(guard, sources))
    else:
        for src in sources:
            guard(src)
    return 1 if CTX.failures else 0


def bench_corpus(paths: Sequence[Path]) -> bytes:
    if paths:
        blob = b""
        for path in paths:
            if path.is_file():
                blob += path.read_bytes()
        return blob
    sample = (
        b"the quick brown fox jumps over the lazy dog\n"
        b"0123456789 abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRSTUVWXYZ\n"
        b"lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor\n"
    )
    return sample * 4000


def cmd_bench(args: argparse.Namespace) -> int:
    blob = bench_corpus([path for path in args.paths if path.is_file()])
    if not blob:
        print("empty corpus", file=sys.stderr)
        return 1
    names = args.algos or ["gzip", "bz2", "xz", "zstd", "brotli", "lz4"]
    rows: list[dict] = []
    for name in names:
        codec = resolve_codec(name)
        level = clamp(codec, args.level if args.level is not None else codec.default)
        try:
            started = time.perf_counter()
            packed = codec.once(blob, level)
            pack_seconds = time.perf_counter() - started
            started = time.perf_counter()
            restored = codec.once_decode(packed)
            unpack_seconds = time.perf_counter() - started
            ok = restored == blob
        except Exception as exc:  # noqa: BLE001 - a broken codec is a row, not a crash
            rows.append({"codec": codec.name, "level": level, "error": f"{type(exc).__name__}: {exc}"})
            continue
        rows.append({
            "codec": codec.name,
            "level": level,
            "bytes_in": len(blob),
            "bytes_out": len(packed),
            "ratio": round(len(blob) / max(len(packed), 1), 2),
            "compress_mb_s": round(len(blob) / max(pack_seconds, 1e-9) / (1 << 20), 1),
            "decompress_mb_s": round(len(blob) / max(unpack_seconds, 1e-9) / (1 << 20), 1),
            "verified": ok,
            "backend": codec.hint,
        })
    if CTX.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"corpus: {human(len(blob))}\n")
    print(f"{'codec':<8}{'level':>6}{'size':>10}{'ratio':>8}{'c MB/s':>9}{'d MB/s':>9}  ok  backend")
    for row in rows:
        if "error" in row:
            print(f"{row['codec']:<8}{row['level']:>6}  {row['error']}")
            continue
        print(f"{row['codec']:<8}{row['level']:>6}{human(row['bytes_out']):>10}"
              f"{row['ratio']:>7.2f}x{row['compress_mb_s']:>9.1f}{row['decompress_mb_s']:>9.1f}"
              f"  {'yes' if row['verified'] else 'NO':<4}{row['backend']}")
    return 0 if all(row.get("verified", False) for row in rows) else 1


def cmd_inspect(args: argparse.Namespace) -> int:
    sources = collect_files(args.paths, recursive=args.recursive, exclude=args.exclude)
    if not sources:
        print("no input files", file=sys.stderr)
        return 1
    rows: list[dict] = []
    for src in sources:
        kind, confidence = sniff(src)
        codec = CODECS.get(kind)
        size = src.stat().st_size
        row = {
            "path": str(src),
            "format": kind,
            "confidence": confidence,
            "bytes": size,
            "codec_available": bool(codec) or kind in ("chunked", "zip", "7z", "tar"),
            "declared_size": declared_size(src),
        }
        if kind == "chunked":
            try:
                with src.open("rb") as handle:
                    handle.read(len(FRAME_MAGIC))
                    name = handle.read(handle.read(1)[0]).decode("ascii")
                row["frame_codec"] = name
                row["codec_available"] = name in CODECS
            except Exception:  # noqa: BLE001
                row["frame_codec"] = "?"
        deep_codec = CODECS.get(row.get("frame_codec", "")) if kind == "chunked" else CODECS.get(kind)
        if args.deep and deep_codec is not None:
            temporary = src.with_name(f".{src.name}.{os.getpid()}.inspect")
            try:
                decompress_file(src, temporary, deep_codec, 0)
                row["actual_size"] = temporary.stat().st_size
            except Exception as exc:  # noqa: BLE001
                row["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                temporary.unlink(missing_ok=True)
        rows.append(row)
        if not CTX.json:
            declared = row.get("actual_size") or row.get("declared_size")
            extra = f"  {human(declared)} uncompressed" if declared else ""
            if "error" in row:
                extra += f"  [{row['error']}]"
            say(f"{src}  {kind} ({confidence})  {human(size)}{extra}")
    if CTX.json:
        print(json.dumps(rows, indent=2))
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    print(f"{PROG} {__version__}  python {sys.version.split()[0]}  ({os.cpu_count()} cpu)\n")
    print(f"{'codec':<8}{'ext':<10}{'levels':<9}{'backend':<36}status")
    for name in available_codecs():
        codec = CODECS[name]
        levels = f"{codec.low}-{codec.high}"
        print(f"{name:<8}{codec.ext:<10}{levels:<9}{codec.hint:<36}ok")
    missing = {
        "zstd": "pip install zstandard",
        "brotli": "pip install brotli",
        "lz4": "pip install lz4",
        "snappy": "pip install python-snappy",
        "ppmd": "pip install pyppmd",
        "zopfli": "pip install zopfli",
    }
    for name, hint in missing.items():
        if name not in CODECS:
            print(f"{name:<8}{'-':<10}{'-':<9}{'missing':<36}{hint}")
    print("\nchunked frames:", "available" if _zstd or _brotli else "limited to stdlib codecs")
    return 0


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def _sizes(text: str) -> int:
    """``"4M"`` -> 4194304."""
    text = text.strip().lower()
    units = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}
    if text and text[-1] in units:
        return int(float(text[:-1]) * units[text[-1]])
    if text.endswith(("kb", "mb", "gb")):
        return int(float(text[:-2]) * units[text[-2]])
    return int(text)


class _CodecList(argparse.Action):
    """``-a gzip,bz2 -a xz`` -> ["gzip", "bz2", "xz"]."""

    def __call__(self, parser, namespace, values, option_string=None):
        current = list(getattr(namespace, self.dest, None) or [])
        for piece in str(values).replace(" ", "").split(","):
            if piece:
                current.append(piece)
        setattr(namespace, self.dest, current)


def duplicate_targets(pairs: Sequence[tuple[Path, Path]]) -> dict[Path, list[Path]]:
    grouped: dict[Path, list[Path]] = {}
    for src, target in pairs:
        grouped.setdefault(target, []).append(src)
    return {target: sources for target, sources in grouped.items() if len(sources) > 1}


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-n", "--dry-run", action="store_true", help="report what would be written")
    common.add_argument("--backup", action="store_true", help="keep a .bak of overwritten outputs")
    common.add_argument("-f", "--force", action="store_true", help="overwrite existing outputs")
    common.add_argument("-q", "--quiet", action="store_true")
    common.add_argument("-v", "--verbose", action="store_true")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                        help="skip paths matching GLOB (repeatable)")
    common.add_argument("-r", "--recursive", dest="recursive", action="store_true",
                        help="descend into directories")
    common.add_argument("--no-recursive", dest="recursive", action="store_false",
                        help=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Unified single-stream compressor: gzip bz2 xz lzma zstd brotli lz4 ppmd.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "was: gzbz lz4er zstdtools xztools brer compress_text fast_compress compresstools\n"
            "     smart_archiver compression_benchmark xcompress\n"
            "\n"
            "examples:\n"
            "  %(prog)s compress notes.txt -a zstd --level 19 --verify\n"
            "  %(prog)s compress logs/ -r -a gzip -j 8 --remove-source\n"
            "  %(prog)s decompress notes.txt.zst -o out/\n"
            "  %(prog)s bench corpus.txt -a gzip,bz2,xz,zstd,brotli\n"
            "  %(prog)s inspect mystery.bin --deep\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subs = parser.add_subparsers(dest="command", required=True)

    def add_paths(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("paths", nargs="*", type=Path, metavar="PATH",
                         help="files and/or directories (default: .)")

    compress = subs.add_parser("compress", parents=[common], help="compress files")
    add_paths(compress)
    compress.add_argument("-a", "--algos", "--algo", action=_CodecList, metavar="CODEC",
                          help="gzip|bz2|xz|lzma|zlib|zstd|brotli|lz4|snappy|ppmd|zopfli (repeatable)")
    compress.add_argument("-l", "--level", type=int, default=-1, help="codec level (clamped per codec)")
    compress.add_argument("-o", "--output-dir", type=Path, default=None)
    compress.add_argument("--chunk-size", type=_sizes, default=0,
                          help="store independent blocks, e.g. 64M")
    compress.add_argument("--min-size", type=_sizes, default=0, help="skip files smaller than this")
    compress.add_argument("--no-skip-ext", action="store_true", help="also recompress archives and media")
    compress.add_argument("--remove-source", action="store_true")
    compress.add_argument("--verify", action="store_true", help="decompress again and compare")
    compress.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 1))

    decompress = subs.add_parser("decompress", parents=[common], help="decompress files")
    add_paths(decompress)
    decompress.add_argument("-a", "--codec", default="auto", metavar="CODEC",
                            help="force a codec instead of sniffing")
    decompress.add_argument("-o", "--output-dir", type=Path, default=None)
    decompress.add_argument("--chunk-size", type=_sizes, default=0, help="accepted for symmetry")
    decompress.add_argument("--remove-source", action="store_true")
    decompress.add_argument("--verify", action="store_true")
    decompress.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 1))

    bench = subs.add_parser("bench", parents=[common], help="compare codecs on one corpus")
    add_paths(bench)
    bench.add_argument("-a", "--algos", "--algo", action=_CodecList, metavar="CODEC")
    bench.add_argument("-l", "--level", type=int, default=None)

    inspect = subs.add_parser("inspect", parents=[common], help="sniff format and size without writing")
    add_paths(inspect)
    inspect.add_argument("--deep", action="store_true", help="decode once to learn the real size")

    subs.add_parser("doctor", help="which codecs are usable here")
    return parser


HANDLERS = {
    "compress": cmd_compress,
    "decompress": cmd_decompress,
    "bench": cmd_bench,
    "inspect": cmd_inspect,
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
    if CTX.json and args.command in ("compress", "decompress"):
        emit_json(args.command)
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
