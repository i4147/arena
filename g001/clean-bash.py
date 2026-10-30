#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import argparse
import difflib
import shutil
import sys
from multiprocessing import Pool
from pathlib import Path
from typing import TYPE_CHECKING

import tree_sitter_bash
from loguru import logger
from tree_sitter import Language, Node, Parser

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
WORKERS: int = 8
_PARSER: Parser | None = None


def get_parser() -> Parser:
    global _PARSER
    if _PARSER is None:
        lang: Language = Language(tree_sitter_bash.language())
        parser: Parser = Parser()
        parser.language = lang
        _PARSER = parser
    return _PARSER


def is_bash_file(path: Path) -> bool:
    if path.is_dir() or path.is_symlink() or path.name.startswith("."):
        return False
    ext = path.suffix.lower()
    if ext == ".sh":
        return True
    if ext == "":
        try:
            with open(path, "rb") as f:
                header = f.read(128)
            if header.startswith(b"#!") and (b"bash" in header or b"sh" in header):
                return True
        except Exception:
            pass
    return False


def collect_comment_ranges(root: Node, content: bytes) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    stack: list[Node] = [root]
    while stack:
        node: Node = stack.pop()
        if node.type == "comment":
            comment_bytes = content[node.start_byte : node.end_byte]
            stripped = comment_bytes.lstrip()
            if not (stripped.startswith((b"#!", b"# shellcheck", b"##"))):
                ranges.append((node.start_byte, node.end_byte))
            continue
        for child in reversed(node.children):
            stack.append(child)
    return ranges


def remove_blank_lines(content: bytes) -> bytes:
    lines = content.splitlines(keepends=True)
    non_blank = [line for line in lines if line.strip(b" \t\r\n") != b""]
    return b"".join(non_blank)


def strip_comments(content: bytes) -> tuple[bytes, int]:
    parser: Parser = get_parser()
    tree = parser.parse(content)
    ranges: list[tuple[int, int]] = collect_comment_ranges(tree.root_node, content)
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


def process_file(args_tuple: tuple[Path, Path, bool, bool]) -> tuple[str, int, int, str, str, str]:
    path, base, dry_run, backup = args_tuple
    try:
        path = Path(path).resolve()
        base = Path(base).resolve()
        content: bytes = path.read_bytes()
        orig_size = len(content)
        try:
            old_text = content.decode("utf-8", errors="replace")
        except Exception:
            old_text = ""
        new_content, count = strip_comments(content)
        new_content = remove_blank_lines(new_content)
        new_size = len(new_content)
        reduced = orig_size - new_size
        try:
            new_text = new_content.decode("utf-8", errors="replace")
        except Exception:
            new_text = ""
        if not dry_run and reduced > 0:
            if backup:
                backup_path = path.with_suffix(path.suffix + ".bak" if path.suffix else ".sh.bak")
                shutil.copy2(path, backup_path)
            path.write_bytes(new_content)
        try:
            rel: str = str(path.relative_to(base))
        except ValueError:
            rel = str(path)
        return rel, reduced, count, "", old_text, new_text
    except Exception as exc:
        return str(path), 0, 0, str(exc), "", ""


def iter_bash_files(paths: Iterable[Path]) -> Iterator[Path]:
    seen: set[Path] = set()
    for p in paths:
        if p.is_file():
            if is_bash_file(p):
                rp: Path = p.resolve()
                if rp not in seen:
                    seen.add(rp)
                    yield p
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and is_bash_file(f):
                    rp = f.resolve()
                    if rp not in seen:
                        seen.add(rp)
                        yield f


def print_colorful_diff(old_text: str, new_text: str, rel: str) -> None:
    diff = difflib.unified_diff(
        old_text.splitlines(keepends=True), new_text.splitlines(keepends=True), fromfile=f"a/{rel}", tofile=f"b/{rel}"
    )
    for line in diff:
        if line.startswith("+") and not line.startswith("+++"):
            sys.stdout.write(f"\033[32m{line}\033[0m")
        elif line.startswith("-") and not line.startswith("---"):
            sys.stdout.write(f"\033[31m{line}\033[0m")
        elif line.startswith("@@"):
            sys.stdout.write(f"\033[36m{line}\033[0m")
        else:
            sys.stdout.write(line)


def main() -> int:
    ap: argparse.ArgumentParser = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="*", type=Path)
    ap.add_argument("--diff", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--backup", action="store_true")
    args: argparse.Namespace = ap.parse_args()
    inputs: list[Path] = list(args.paths) if args.paths else [Path()]
    files: list[Path] = list(iter_bash_files(inputs))
    if not files:
        logger.error("No bash files to process.")
        return 1
    base: Path = Path.cwd()
    total_reduced: int = 0
    errors: int = 0
    tasks = [(p, base, args.dry_run, args.backup) for p in files]
    if len(files) == 1:
        rel, reduced, count, err, old_text, new_text = process_file(tasks[0])
        if err:
            errors += 1
            logger.error(f"{rel}: ERROR: {err}")
        else:
            total_reduced += reduced
            green_reduced = f"\033[32m{reduced}\033[0m"
            print(f"{rel}  {green_reduced}")
            if args.diff and reduced > 0:
                print_colorful_diff(old_text, new_text, rel)
    else:
        with Pool(processes=WORKERS) as pool:
            results = [pool.apply_async(process_file, t) for t in tasks]
            for res in results:
                rel, reduced, _count, err, old_text, new_text = res.get()
                if err:
                    errors += 1
                    logger.error(f"{rel}: ERROR: {err}")
                    continue
                total_reduced += reduced
                green_reduced = f"\033[32m{reduced}\033[0m"
                print(f"{rel}  {green_reduced}")
                if args.diff and reduced > 0:
                    print_colorful_diff(old_text, new_text, rel)
    print(f"\nTotal space freed: {total_reduced} bytes")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
