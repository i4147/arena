#!/usr/bin/env python3
"""extract.py — pull one kind of declaration out of a Python tree into ``output/``.

Consolidates g004's six ex_* scripts:

    ex_class     -> classes      ex_comments  -> comments
    ex_func      -> functions    ex_ex        -> docstrings
    ex_imports   -> imports      ex_const     -> constants

``ex_comments`` and ``ex_ex`` never produced output: they filtered on tree-sitter
node types that do not exist (``expression_statements``, ``function_docstrings``).
Both are fixed here, and the stdlib ``ast``/``tokenize`` path needs no grammar
package at all.

Every run answers one question per directory: *"show me all the X in this tree"*.
The result is a runnable, importable module per output folder:

    output/<relative folder>/classes.py

Exit codes: 0 ok · 1 errors · 2 usage · 130 interrupted
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import io
import json
import os
import re
import sys
import textwrap
import time
import tokenize
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

__version__ = "1.0.0"
PROG = "extract.py"

DEFAULT_OUTPUT = Path("output")
DEFAULT_SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", ".idea", ".vscode", ".venv", "venv", "env",
        "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", ".tox", ".nox", ".cache", "build", "dist", "target",
        "site-packages", ".eggs", "output",
    }
)

KINDS: tuple[str, ...] = ("classes", "functions", "imports", "comments", "docstrings", "constants")
CONSTANT_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


# --------------------------------------------------------------------------- #
# source helpers                                                              #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Config:
    kinds: tuple[str, ...] = KINDS
    output: Path = DEFAULT_OUTPUT
    layout: str = "folder"  # folder | file | single
    nested: bool = False
    dedupe: str = "none"  # none | content | name
    unique: bool = False
    min_lines: int = 1
    header: bool = False
    strip: bool = False
    max_size: int = 4 * 1024 * 1024
    dry_run: bool = False
    quiet: bool = False


@dataclass(slots=True)
class Chunk:
    """One extracted declaration."""

    kind: str
    source: str
    line: int
    origin: str


def line_offsets(text: str) -> list[int]:
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def segment(lines: list[str], start: int, end: int) -> str:
    """1-based inclusive line range from ``lines``."""
    return "".join(lines[start - 1 : end])


def dedent_block(text: str) -> str:
    return textwrap.dedent(text).strip("\n")


# --------------------------------------------------------------------------- #
# AST extractors                                                              #
# --------------------------------------------------------------------------- #


def _iter_definitions(tree: ast.AST, nested: bool) -> Iterator[ast.AST]:
    if nested:
        yield from (node for node in ast.walk(tree) if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)))
        return
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _docstring_node(node: ast.AST) -> ast.Expr | None:
    body = getattr(node, "body", None)
    if not body:
        return None
    first = body[0]
    if not isinstance(first, ast.Expr) or not isinstance(first.value, ast.Constant):
        return None
    if not isinstance(first.value.value, str):
        return None
    return first


def extract_classes(tree: ast.Module, lines: list[str], nested: bool) -> list[Chunk]:
    out = []
    for node in _iter_definitions(tree, nested):
        if not isinstance(node, ast.ClassDef):
            continue
        text = dedent_block(segment(lines, node.lineno, node.end_lineno or node.lineno))
        out.append(Chunk("classes", text, node.lineno, node.name))
    return out


def extract_functions(tree: ast.Module, lines: list[str], nested: bool) -> list[Chunk]:
    out = []
    for node in _iter_definitions(tree, nested):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        text = dedent_block(segment(lines, node.lineno, node.end_lineno or node.lineno))
        out.append(Chunk("functions", text, node.lineno, node.name))
    return out


def extract_imports(tree: ast.Module, lines: list[str], nested: bool) -> list[Chunk]:
    out = []
    nodes: Iterable[ast.AST] = ast.walk(tree) if nested else tree.body
    for node in nodes:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        text = segment(lines, node.lineno, node.end_lineno or node.lineno).rstrip("\n")
        out.append(Chunk("imports", text, node.lineno, ast.unparse(node)))
    return out


def extract_docstrings(tree: ast.Module, lines: list[str], nested: bool) -> list[Chunk]:
    out = []
    owners: list[tuple[str, ast.AST]] = [("module", tree)]
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            owners.append((node.name, node))
        elif nested and isinstance(node, (ast.Module,)):
            continue
    for name, node in owners:
        expr = _docstring_node(node)
        if expr is None:
            continue
        text = dedent_block(segment(lines, expr.lineno, expr.end_lineno or expr.lineno))
        text = f"# {name}\n{text}" if name != "module" else text
        out.append(Chunk("docstrings", text, expr.lineno, name))
    return out


def extract_constants(tree: ast.Module, lines: list[str], nested: bool) -> list[Chunk]:
    out = []
    nodes: Iterable[ast.AST] = ast.walk(tree) if nested else tree.body
    for node in nodes:
        if isinstance(node, ast.Assign):
            if not all(isinstance(target, ast.Name) for target in node.targets):
                continue
            if not isinstance(node.value, ast.Constant):
                continue
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            if not names or not all(CONSTANT_RE.match(name) for name in names):
                continue
        elif isinstance(node, ast.AnnAssign):
            if not isinstance(node.target, ast.Name) or node.value is None:
                continue
            if not CONSTANT_RE.match(node.target.id):
                continue
            names = [node.target.id]
        else:
            continue
        text = segment(lines, node.lineno, node.end_lineno or node.lineno).rstrip("\n")
        for name in names:
            out.append(Chunk("constants", text, node.lineno, name))
    return out


def extract_comments(text: str) -> list[Chunk]:
    """Comments via ``tokenize`` — the honest replacement for ex_comments.py."""
    out: list[Chunk] = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type != tokenize.COMMENT:
                continue
            comment = token.string.rstrip()
            if comment.startswith("#!"):
                continue
            out.append(Chunk("comments", comment, token.start[0], comment[1:].strip()[:60]))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return out


EXTRACTORS = {
    "classes": extract_classes,
    "functions": extract_functions,
    "imports": extract_imports,
    "docstrings": extract_docstrings,
    "constants": extract_constants,
}


# --------------------------------------------------------------------------- #
# per-file work                                                               #
# --------------------------------------------------------------------------- #


def parse_file(path: Path, cfg: Config) -> tuple[str, list[Chunk], str | None]:
    try:
        if path.stat().st_size > cfg.max_size:
            return str(path), [], "larger than --max-size"
        raw = path.read_bytes()
    except OSError as exc:
        return str(path), [], f"{type(exc).__name__}: {exc}"
    if b"\x00" in raw[:4096]:
        return str(path), [], "binary"
    try:
        text = raw.decode("utf-8-sig")
        tree = ast.parse(text, filename=str(path))
    except (UnicodeDecodeError, SyntaxError) as exc:
        return str(path), [], f"{type(exc).__name__}: {exc}"

    lines = text.splitlines(keepends=True)
    chunks: list[Chunk] = []
    for kind in cfg.kinds:
        if kind == "comments":
            chunks.extend(extract_comments(text))
        else:
            chunks.extend(EXTRACTORS[kind](tree, lines, cfg.nested))
    for chunk in chunks:
        chunk.origin = str(path)
    return str(path), chunks, None


def _worker(args: tuple[str, Config]) -> tuple[str, list[Chunk], str | None]:
    return parse_file(Path(args[0]), args[1])


# --------------------------------------------------------------------------- #
# grouping & writing                                                          #
# --------------------------------------------------------------------------- #


def relative_folder(path: Path, roots: Sequence[Path]) -> Path:
    for root in roots:
        try:
            return path.parent.relative_to(root)
        except ValueError:
            continue
    return Path(path.parent.name)


def output_path(cfg: Config, kind: str, folder: Path) -> Path:
    if cfg.layout == "single":
        return cfg.output / f"{kind}.py"
    if cfg.layout == "file":
        return cfg.output / "files" / folder / f"{kind}.py"
    return cfg.output / folder / f"{kind}.py"


def render(chunks: Sequence[Chunk], kind: str, cfg: Config) -> str:
    blocks: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        body = chunk.source if not cfg.strip else chunk.source.strip()
        if not body or len(body.splitlines()) < cfg.min_lines:
            continue
        if cfg.unique:
            key = body.strip()
            if key in seen:
                continue
            seen.add(key)
        if cfg.header:
            blocks.append(f"# {Path(chunk.origin).as_posix()}:{chunk.line}\n{body}")
        else:
            blocks.append(body)
    if not blocks:
        return ""
    banner = f"# {kind} extracted by {PROG} {__version__} — {len(blocks)} item(s)\n\n" if cfg.header else ""
    return banner + "\n\n".join(blocks) + "\n"


@dataclass(slots=True)
class WriteReport:
    path: Path
    items: int
    status: str
    detail: str = ""


def write_outputs(
    by_kind: dict[str, "OrderedDict[Path, list[Chunk]]"],
    cfg: Config,
) -> list[WriteReport]:
    reports: list[WriteReport] = []
    for kind, folders in by_kind.items():
        if cfg.layout == "single":
            merged: list[Chunk] = []
            for chunks in folders.values():
                merged.extend(chunks)
            targets = [(cfg.output / f"{kind}.py", merged)]
        else:
            targets = [(output_path(cfg, kind, folder), chunks) for folder, chunks in folders.items()]
        for target, chunks in targets:
            content = render(chunks, kind, cfg)
            if not content:
                continue
            if cfg.dry_run:
                reports.append(WriteReport(target, len(chunks), "would-write"))
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            reports.append(WriteReport(target, len(chunks), "written"))
    return reports


# --------------------------------------------------------------------------- #
# traversal                                                                   #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Walk:
    recursive: bool = True
    skip_dirs: frozenset[str] = DEFAULT_SKIP_DIRS
    exclude: tuple[str, ...] = ()
    include: tuple[str, ...] = ()


def collect_targets(paths: Sequence[Path], walk: Walk, output: Path) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []

    def wanted(path: Path) -> bool:
        if path.suffix.lower() != ".py":
            return False
        if walk.include and not any(fnmatch.fnmatch(path.name, pattern) for pattern in walk.include):
            return False
        return not any(fnmatch.fnmatch(path.name, p) or fnmatch.fnmatch(str(path), p) for p in walk.exclude)

    def add(path: Path) -> None:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen or output.resolve() in key.parents:
            return
        seen.add(key)
        out.append(path)

    for raw in paths:
        path = raw.expanduser()
        if not path.exists() or path.is_symlink():
            continue
        if path.is_file():
            if wanted(path):
                add(path)
            continue
        if not path.is_dir():
            continue
        if walk.recursive:
            for root, dirs, files in os.walk(path):
                dirs[:] = sorted(d for d in dirs if d not in walk.skip_dirs and not d.startswith("."))
                for name in sorted(files):
                    candidate = Path(root) / name
                    if wanted(candidate) and not candidate.is_symlink():
                        add(candidate)
        else:
            for candidate in sorted(path.iterdir()):
                if candidate.is_file() and wanted(candidate):
                    add(candidate)
    return out


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Extract classes / functions / imports / comments / docstrings / constants into output/.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "was: ex_class.py, ex_func.py, ex_imports.py, ex_comments.py, ex_ex.py, ex_const.py\n"
            "\n"
            "layout:\n"
            "  folder  output/<folder of the source>/<kind>.py   (default)\n"
            "  file    output/files/<parent of the source>/<kind>.py\n"
            "  single  output/<kind>.py                          (everything merged)\n"
            "\n"
            "examples:\n"
            "  %(prog)s all src/\n"
            "  %(prog)s classes functions --layout single -o /tmp/out\n"
            "  %(prog)s constants --dedupe content --unique\n"
        ),
    )
    parser.add_argument("args", nargs="*", metavar="KIND|PATH",
                        help=f"leading tokens that name a kind ({', '.join(KINDS)}, all); "
                             "everything else is a file/directory (default: all + .)")
    parser.add_argument("-o", "--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--layout", choices=("folder", "file", "single"), default="folder")
    parser.add_argument("--nested", action="store_true", help="include nested classes/functions, not just top level")
    parser.add_argument("--dedupe", choices=("none", "content", "name"), default="none",
                        help="content: identical source files are read once")
    parser.add_argument("--unique", action="store_true", help="drop duplicate extracted blocks")
    parser.add_argument("--min-lines", type=int, default=1)
    parser.add_argument("--header", action="store_true", help="prefix each block with '# path:line'")
    parser.add_argument("--strip", action="store_true", help="strip each block (default: dedent only)")
    parser.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 1))
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    parser.add_argument("--include", action="append", default=[], metavar="GLOB")
    parser.add_argument("--no-recursive", action="store_true")
    parser.add_argument("--max-size", type=int, default=4 * 1024 * 1024)
    parser.add_argument("-n", "--dry-run", action="store_true")
    parser.add_argument("--json", nargs="?", const="-", default=None, metavar="FILE")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def split_kinds(values: Sequence[str]) -> tuple[str, ...]:
    if not values or "all" in values:
        return KINDS
    return tuple(dict.fromkeys(values))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    raw_kinds: list[str] = []
    raw_paths: list[str] = []
    for token in args.args:
        if not raw_paths and token in (*KINDS, "all"):
            raw_kinds.append(token)
        else:
            raw_paths.append(token)
    unknown = [t for t in raw_kinds if t not in (*KINDS, "all")]
    if unknown:
        parser.error(f"unknown kind: {', '.join(unknown)}")
    kinds = split_kinds(raw_kinds)
    roots = [Path(p).expanduser() for p in raw_paths] or [Path.cwd()]
    walk = Walk(recursive=not args.no_recursive, exclude=tuple(args.exclude), include=tuple(args.include))
    targets = collect_targets(roots, walk, args.output_dir)
    started = time.perf_counter()

    if not targets:
        if not args.quiet:
            print("no Python files found", file=sys.stderr)
        return 0

    cfg = Config(
        kinds=kinds,
        output=args.output_dir,
        layout=args.layout,
        nested=args.nested,
        dedupe=args.dedupe,
        unique=args.unique,
        min_lines=args.min_lines,
        header=args.header,
        strip=args.strip,
        max_size=args.max_size,
        dry_run=args.dry_run,
        quiet=args.quiet,
    )

    if args.jobs > 1 and len(targets) > 4:
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            results = list(pool.map(_worker, [(str(t), cfg) for t in targets]))
    else:
        results = [_worker((str(t), cfg)) for t in targets]

    errors = 0
    seen_content: set[str] = set()
    by_kind: dict[str, OrderedDict[Path, list[Chunk]]] = {kind: OrderedDict() for kind in kinds}
    for raw_path, chunks, error in results:
        if error:
            errors += 1
            if args.verbose:
                print(f"{raw_path}: {error}", file=sys.stderr)
            continue
        path = Path(raw_path)
        if args.dedupe == "content":
            digest = hashlib.sha1(path.read_bytes()).hexdigest()
            if digest in seen_content:
                continue
            seen_content.add(digest)
        elif args.dedupe == "name":
            key = path.name
            if key in seen_content:
                continue
            seen_content.add(key)
        folder = relative_folder(path, roots)
        for chunk in chunks:
            by_kind[chunk.kind].setdefault(folder, []).append(chunk)

    reports = write_outputs(by_kind, cfg)
    elapsed = time.perf_counter() - started

    if not args.quiet:
        for report in reports:
            marker = "would write" if report.status == "would-write" else "wrote"
            print(f"{marker} {report.path}  ({report.items} item(s))")
        total = sum(report.items for report in reports)
        print(f"\n{len(reports)} file(s), {total} item(s), {len(targets)} source file(s), {elapsed:.2f}s",
              file=sys.stderr)
    if errors and not args.quiet:
        print(f"{errors} file(s) skipped", file=sys.stderr)

    if args.json:
        payload = {
            "tool": PROG,
            "version": __version__,
            "elapsed": round(elapsed, 3),
            "sources": len(targets),
            "errors": errors,
            "outputs": [{"path": str(r.path), "items": r.items, "status": r.status} for r in reports],
        }
        text = json.dumps(payload, indent=2)
        if args.json == "-":
            print(text)
        else:
            Path(args.json).write_text(text, encoding="utf-8")
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
