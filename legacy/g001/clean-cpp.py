#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import argparse
import sys
from multiprocessing import Pool
from pathlib import Path
from typing import TYPE_CHECKING

import tree_sitter_c
import tree_sitter_cpp
from loguru import logger
from tree_sitter import Language, Node, Parser

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
CPP_EXTS: frozenset[str] = frozenset({
    ".cc",
    ".cpp",
    ".cxx",
    ".c++",
    ".hpp",
    ".hh",
    ".hxx",
    ".h++",
    ".inl",
})
C_EXTS: frozenset[str] = frozenset({".c", ".h"})
ALL_EXTS: frozenset[str] = C_EXTS | CPP_EXTS
WORKERS: int = 8
_PARSERS: dict[str, Parser] = {}


def get_parser(ext: str) -> Parser:
    if ext not in _PARSERS:
        if ext in C_EXTS:
            lang: Language = Language(tree_sitter_c.language())
        elif ext in CPP_EXTS:
            lang = Language(tree_sitter_cpp.language())
        else:
            msg = f"Unsupported extension: {ext}"
            raise ValueError(msg)
        parser: Parser = Parser()
        parser.language = lang
        _PARSERS[ext] = parser
    return _PARSERS[ext]


def collect_comment_ranges(root: Node, content: bytes) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    stack: list[Node] = [root]
    while stack:
        node: Node = stack.pop()
        if node.type == "comment":
            comment_bytes = content[node.start_byte : node.end_byte]
            stripped = comment_bytes.lstrip()
            if not (stripped.startswith((b"///", b"/**", b"/*!"))):
                ranges.append((node.start_byte, node.end_byte))
            continue
        for child in reversed(node.children):
            stack.append(child)
    return ranges


def remove_blank_lines(content: bytes) -> bytes:
    lines = content.splitlines(keepends=True)
    non_blank = [line for line in lines if line.strip(b" \t\r\n") != b""]
    return b"".join(non_blank)


def strip_comments(
    content: bytes,
    ext: str,
    selected_ranges: list[tuple[int, int]] | None = None,
) -> tuple[bytes, int]:
    if selected_ranges is None:
        parser: Parser = get_parser(ext)
        tree = parser.parse(content)
        ranges: list[tuple[int, int]] = collect_comment_ranges(tree.root_node, content)
    else:
        ranges = selected_ranges
    if not ranges:
        return content, 0
    ranges.sort(key=lambda r: r[0])
    out: bytearray = bytearray()
    last: int = 0
    for start, end in ranges:
        out.extend(content[last:start])
        last = end
    out.extend(content[last:])
    return bytes(out), len(ranges)


def process_file(path: Path, base: Path) -> tuple[str, int, int, str]:
    try:
        path = Path(path).resolve()
        base = Path(base).resolve()
        content: bytes = path.read_bytes()
        orig_size = len(content)
        ext: str = path.suffix.lower()
        new_content, count = strip_comments(content, ext)
        new_content = remove_blank_lines(new_content)
        new_size = len(new_content)
        reduced = orig_size - new_size
        if new_content != content:
            path.write_bytes(new_content)
        try:
            rel: str = str(path.relative_to(base))
        except ValueError:
            rel = str(path)
        return rel, reduced, count, ""
    except Exception as exc:
        return str(path), 0, 0, str(exc)


def iter_cc_files(paths: Iterable[Path]) -> Iterator[Path]:
    seen: set[Path] = set()
    for p in paths:
        if p.is_file() and p.suffix.lower() in ALL_EXTS:
            rp: Path = p.resolve()
            if rp not in seen:
                seen.add(rp)
                yield p
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and f.suffix.lower() in ALL_EXTS:
                    rp = f.resolve()
                    if rp not in seen:
                        seen.add(rp)
                        yield f


def main() -> int:
    ap: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Remove non-vital comments and blank lines from C/C++ files in place (tree-sitter powered)."
    )
    ap.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Files or directories. Defaults to current directory recursively.",
    )
    args: argparse.Namespace = ap.parse_args()
    inputs: list[Path] = list(args.paths) if args.paths else [Path()]
    files: list[Path] = list(iter_cc_files(inputs))
    if not files:
        logger.error("No C/C++ files to process.")
        return 1
    base: Path = Path.cwd()
    total_reduced: int = 0
    errors: int = 0
    if len(files) == 1:
        rel, reduced, count, err = process_file(files[0], base)
        if err:
            errors += 1
            logger.error(f"{rel}: ERROR: {err}")
        else:
            total_reduced += reduced
            green_reduced = f"\033[32m{reduced}\033[0m"
            print(f"{rel}  {green_reduced}")
    else:
        with Pool(processes=WORKERS) as pool:
            results = [pool.apply_async(process_file, (p, base)) for p in files]
            for res in results:
                rel, reduced, _count, err = res.get()
                if err:
                    errors += 1
                    logger.error(f"{rel}: ERROR: {err}")
                    continue
                total_reduced += reduced
                green_reduced = f"\033[32m{reduced}\033[0m"
                print(f"{rel}  {green_reduced}")
    print(f"\nTotal space freed: {total_reduced} bytes")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
