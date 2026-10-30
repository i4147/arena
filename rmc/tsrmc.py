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
import tree_sitter_c
import tree_sitter_cpp
import tree_sitter_css
import tree_sitter_html
import tree_sitter_javascript
import tree_sitter_lua
import tree_sitter_rust
import tree_sitter_toml
import tree_sitter_typescript
from loguru import logger
from tree_sitter import Language, Node, Parser

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
WORKERS: int = 8
_PARSERS: dict[str, Parser] = {}
_SUPPORTED_EXTS: frozenset[str] = frozenset({
    ".rs",
    ".sh",
    ".c",
    ".cpp",
    ".cc",
    ".h",
    ".hh",
    ".hpp",
    ".js",
    ".ts",
    ".css",
    ".jsx",
    ".tsx",
    ".html",
    ".toml",
    ".lua",
})


def get_parser(lang_key: str) -> Parser:
    if lang_key not in _PARSERS:
        if lang_key == "rust":
            lang: Language = Language(tree_sitter_rust.language())
        elif lang_key == "bash":
            lang = Language(tree_sitter_bash.language())
        elif lang_key == "c":
            lang = Language(tree_sitter_c.language())
        elif lang_key == "cpp":
            lang = Language(tree_sitter_cpp.language())
        elif lang_key == "javascript":
            lang = Language(tree_sitter_javascript.language())
        elif lang_key == "typescript":
            lang = Language(tree_sitter_typescript.language_typescript())
        elif lang_key == "tsx":
            lang = Language(tree_sitter_typescript.language_tsx())
        elif lang_key == "css":
            lang = Language(tree_sitter_css.language())
        elif lang_key == "html":
            lang = Language(tree_sitter_html.language())
        elif lang_key == "toml":
            lang = Language(tree_sitter_toml.language())
        elif lang_key == "lua":
            lang = Language(tree_sitter_lua.language())
        else:
            msg = f"Unsupported language: {lang_key}"
            raise ValueError(msg)
        parser: Parser = Parser()
        parser.language = lang
        _PARSERS[lang_key] = parser
    return _PARSERS[lang_key]


def get_lang_key(path: Path) -> str:
    s: str = path.suffix.lower()
    if s == ".rs":
        return "rust"
    elif s in {".sh", ""}:
        return "bash"
    elif s == ".c":
        return "c"
    elif s in {".cpp", ".cc", ".h", ".hpp", ".hh"}:
        return "cpp"
    elif s in {".js", ".jsx"}:
        return "javascript"
    elif s == ".ts":
        return "typescript"
    elif s == ".tsx":
        return "tsx"
    elif s == ".css":
        return "css"
    elif s == ".html":
        return "html"
    elif s == ".toml":
        return "toml"
    elif s == ".lua":
        return "lua"
    return "bash"


def is_target_file(path: Path) -> bool:
    if path.is_dir() or path.is_symlink() or path.name.startswith("."):
        return False
    ext: str = path.suffix.lower()
    if ext in _SUPPORTED_EXTS:
        return True
    if ext == "":
        try:
            with open(path, "rb") as f:
                header: bytes = f.read(128)
            if header.startswith(b"#!") and (b"bash" in header or b"sh" in header):
                return True
        except Exception:
            pass
    return False


def should_preserve_comment(comment_bytes: bytes, lang_key: str) -> bool:
    stripped: bytes = comment_bytes.lstrip()
    if lang_key == "rust":
        return stripped.startswith((b"///", b"//!", b"/**", b"/*!"))
    elif lang_key == "bash":
        return stripped.startswith((b"#!", b"# shellcheck", b"##"))
    elif lang_key in {"c", "cpp"}:
        return stripped.startswith((b"///", b"/**", b"/*!"))
    elif lang_key in {"javascript", "typescript", "tsx"}:
        return stripped.startswith((b"/**", b"/*!"))
    elif lang_key == "css":
        return stripped.startswith(b"/*!")
    elif lang_key == "html":
        return stripped.startswith((b"<!--!", b"<!--["))
    elif lang_key == "lua":
        return stripped.startswith((b"---", b"--[[!"))
    elif lang_key == "toml":
        return stripped.startswith((b"#!", b"##"))
    return False


def collect_comment_ranges(root: Node, content: bytes, lang_key: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    stack: list[Node] = [root]
    while stack:
        node: Node = stack.pop()
        if node.type == "comment":
            comment_bytes: bytes = content[node.start_byte : node.end_byte]
            if not should_preserve_comment(comment_bytes, lang_key):
                ranges.append((node.start_byte, node.end_byte))
            continue
        for child in reversed(node.children):
            stack.append(child)
    return ranges


def remove_blank_lines(content: bytes) -> bytes:
    lines: list[bytes] = content.splitlines(keepends=True)
    non_blank: list[bytes] = [line for line in lines if line.strip(b" \t\r\n") != b""]
    return b"".join(non_blank)


def strip_comments(content: bytes, lang_key: str) -> tuple[bytes, int]:
    parser: Parser = get_parser(lang_key)
    tree = parser.parse(content)
    ranges: list[tuple[int, int]] = collect_comment_ranges(tree.root_node, content, lang_key)
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
        orig_size: int = len(content)
        try:
            old_text: str = content.decode("utf-8", errors="replace")
        except Exception:
            old_text = ""
        lang_key: str = get_lang_key(path)
        new_content, count = strip_comments(content, lang_key)
        new_content = remove_blank_lines(new_content)
        new_size: int = len(new_content)
        reduced: int = orig_size - new_size
        try:
            new_text: str = new_content.decode("utf-8", errors="replace")
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


def iter_target_files(paths: Iterable[Path]) -> Iterator[Path]:
    seen: set[Path] = set()
    for p in paths:
        if p.is_file():
            if is_target_file(p):
                rp: Path = p.resolve()
                if rp not in seen:
                    seen.add(rp)
                    yield p
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and is_target_file(f):
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
    files: list[Path] = list(iter_target_files(inputs))
    if not files:
        logger.error("No target files to process.")
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
