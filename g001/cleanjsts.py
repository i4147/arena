#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import sys
from multiprocessing.pool import Pool
from pathlib import Path
from typing import Final

import tree_sitter
import tree_sitter_javascript
import tree_sitter_typescript
from loguru import logger
from tree_sitter import Language, Parser

MAX_WORKERS: Final[int] = 8
SUPPORTED_EXTENSIONS: Final[tuple[str, ...]] = (".js", ".ts", ".jsx", ".tsx")
_JS_LANGUAGE: Final[Language] = Language(tree_sitter_javascript.language())
_TS_LANGUAGE: Final[Language] = Language(tree_sitter_typescript.language_typescript())
_TSX_LANGUAGE: Final[Language] = Language(tree_sitter_typescript.language_tsx())
_LANGUAGE_BY_EXTENSION: Final[dict[str, Language]] = {
    ".js": _JS_LANGUAGE,
    ".jsx": _JS_LANGUAGE,
    ".ts": _TS_LANGUAGE,
    ".tsx": _TSX_LANGUAGE,
}


def _collect_comment_ranges(node: tree_sitter.Node, out: list[tuple[int, int]]) -> None:
    if node.type == "comment":
        out.append((node.start_byte, node.end_byte))
        return
    for child in node.children:
        _collect_comment_ranges(child, out)


def _has_error(node: tree_sitter.Node) -> bool:
    if node.is_error or node.is_missing:
        return True
    return any(_has_error(child) for child in node.children)


def _remove_ranges(source: bytes, ranges: list[tuple[int, int]]) -> bytes:
    ranges.sort()
    parts: list[bytes] = []
    prev: int = 0
    for start, end in ranges:
        parts.append(source[prev:start])
        newline_count: int = source[start:end].count(b"\n")
        parts.append(b"\n" * newline_count)
        prev = end
    parts.append(source[prev:])
    return b"".join(parts)


def process_file(file_path: Path) -> str | None:
    try:
        language: Language | None = _LANGUAGE_BY_EXTENSION.get(file_path.suffix)
        if language is None:
            return f"Unsupported extension: {file_path}"
        source: bytes = file_path.read_bytes()
        parser: Parser = Parser(language)
        original_tree: tree_sitter.Tree = parser.parse(source)
        if _has_error(original_tree.root_node):
            return f"Error: original file {file_path} contains syntax errors"
        ranges: list[tuple[int, int]] = []
        _collect_comment_ranges(original_tree.root_node, ranges)
        if not ranges:
            return None
        cleaned: bytes = _remove_ranges(source, ranges)
        cleaned_tree: tree_sitter.Tree = parser.parse(cleaned)
        if _has_error(cleaned_tree.root_node):
            return f"Error: cleaned result for {file_path} contains syntax errors"
        file_path.write_bytes(cleaned)
        return None
    except Exception as e:
        return f"Error processing {file_path}: {e}"


def _gather_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if path.is_file():
            if path.suffix in SUPPORTED_EXTENSIONS:
                files.append(path)
        elif path.is_dir():
            for ext in SUPPORTED_EXTENSIONS:
                files.extend(path.rglob(f"*{ext}"))
    return files


def main() -> None:
    if len(sys.argv) > 1:
        paths: list[Path] = [Path(arg) for arg in sys.argv[1:]]
    else:
        paths = [Path.cwd()]
    files_to_process: list[Path] = _gather_files(paths)
    if not files_to_process:
        logger.info("No files to process")
        return
    with Pool(processes=MAX_WORKERS) as pool:
        results: list[str | None] = list(pool.imap_unordered(process_file, files_to_process))
    errors: list[str] = [r for r in results if r is not None]
    if errors:
        for error in errors:
            logger.error(error)
        sys.exit(1)
    logger.info("Processed {} file(s)", len(files_to_process))


if __name__ == "__main__":
    raise SystemExit(main())
