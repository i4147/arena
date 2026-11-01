#!/data/data/com.termux/files/usr/bin/env python3
"""g001 - consolidated comment stripping / minification toolkit.

Everything that used to live in this folder as separate scripts now lives in
this one file.  Each tool's original source is embedded verbatim below, so
nothing was lost: ``python g001.py unpack <tool>`` writes the original
file back out.

Per-language comment strippers collapsed into three tools: one generic
tree-sitter engine for 26 languages, one AST/docstring-aware Python
stripper, and one comment-strip + optional-minifier engine.

Tools
-----
strip    Tree-sitter comment remover for 26 languages: keep-patterns, cgo/shebang/directive vetoes, backups, diffs, dry-run and a worker pool.
         from uncomment.py (531 lines)
pystrip  Python-only stripper: AST based, docstring aware, preserves typing/fmt/noqa markers.
         from cleanpy.py (274 lines)
minify   Comment strip + optional real minifier for css/html/js/ts/tsx/lua/toml (merges the six clean-*.py scripts: rjsmin, jsmin, csscompressor, tinycss2, htmlmin, luaparse, tomllib validation).
         from minify.py (377 lines)

Usage
-----
    python g001.py list                 # list the tools
    python g001.py doctor               # report missing third-party packages
    python g001.py <tool> --help        # help for one tool
    python g001.py strip <args>
    python g001.py pystrip <args>
    python g001.py minify <args>
    python g001.py unpack <tool> [-o DIR] [--stdout]

Requires Python 3.10+.  Individual tools need their own third-party packages;
run ``doctor`` to see which are missing.
"""


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

FOLDER = "g001"


_HERE = Path(__file__).resolve().parent if "__file__" in globals() else None

TOOLS: dict[str, dict[str, object]] = {
    "strip": {"source": "uncomment.py", "lines": 531, "summary": 'Tree-sitter comment remover for 26 languages: keep-patterns, cgo/shebang/directive vetoes, backups, diffs, dry-run and a worker pool.'},
    "pystrip": {"source": "cleanpy.py", "lines": 274, "summary": 'Python-only stripper: AST based, docstring aware, preserves typing/fmt/noqa markers.'},
    "minify": {"source": "minify.py", "lines": 377, "summary": 'Comment strip + optional real minifier for css/html/js/ts/tsx/lua/toml (merges the six clean-*.py scripts: rjsmin, jsmin, csscompressor, tinycss2, htmlmin, luaparse, tomllib validation).'},
}

_SOURCES: dict[str, str] = {}
_LOADED: dict[str, types.ModuleType] = {}


def _decode(*chunks: str) -> str:
    """Fallback representation for sources that cannot be written as raw strings."""
    return zlib.decompress(base64.b85decode("".join(chunks))).decode("utf-8")


_SOURCES['strip'] = r"""#!/data/data/com.termux/files/usr/bin/env python

import argparse
import hashlib
import importlib
import importlib.util
import multiprocessing as mp
import os
import re
import shutil
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, NamedTuple

from loguru import logger
from tree_sitter import Language, Node, Parser

if TYPE_CHECKING:
    from multiprocessing.pool import Pool
Status = Literal["stripped", "unchanged", "skipped", "error"]
PoolMethod = Literal["imap_unordered", "imap", "map", "starmap", "apply_async", "apply"]
Veto = Callable[[Node, bytes], bool]
POOL_METHODS: Final[tuple[PoolMethod, ...]] = (
    "imap_unordered",
    "imap",
    "map",
    "starmap",
    "apply_async",
    "apply",
)
DEFAULT_SKIP_DIRS: Final[frozenset[str]] = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".cache",
    "target",
    "dist",
    "build",
})
COMMENT_TYPES: Final[frozenset[str]] = frozenset({"comment", "line_comment", "block_comment", "Comment"})
TEXT_TYPES: Final[frozenset[str]] = frozenset({"text", "CharData"})
_COMMON: Final[str] = (
    r"copyright|\(c\)\s*\d|\u00a9|spdx-|all rights reserved|permission is hereby granted"
    r"|licensed (?:under|to)|@license|@preserve|@generated|code generated|auto-?generated"
    r"|do not (?:remove|delete|edit|modify)|nosec|noqa|nolint|\bvim?:|-\*-|coding[:=]"
)
_C_FAMILY: Final[str] = r"\A/\*!|clang-format|clang-tidy|NOLINT|coverity|fallthrough|-Wno-|\bpragma\b"
_JS_FAMILY: Final[str] = (
    r"\A/\*!|\A///\s*<|@cc_on|eslint|@ts-|tslint|jshint|biome-ignore|prettier-ignore"
    r"|istanbul|c8 ignore|v8 ignore|webpack(?:Chunk|Mode|Prefetch|Preload)"
    r"|sourceMappingURL|sourceURL|@jsx|@flow|@vite-ignore|[@#]__(?:PURE|NO_SIDE_EFFECTS)__"
)
_SHELL_FAMILY: Final[str] = r"\A#!|shellcheck|shfmt"


class SkipFile(Exception): ...


class ValidationError(Exception): ...


def _is_cgo_preamble(node: Node, src: bytes) -> bool:
    sibling = node.next_sibling
    while sibling is not None and sibling.type in COMMENT_TYPES:
        sibling = sibling.next_sibling
    return (
        sibling is not None
        and sibling.type == "import_declaration"
        and b'"C"' in src[sibling.start_byte : sibling.end_byte]
    )


@dataclass(frozen=True, slots=True)
class LangSpec:
    module: str
    factory: str
    suffixes: frozenset[str]
    keep: re.Pattern[str]
    veto: Veto | None = None


def _spec(
    module: str,
    factory: str,
    suffixes: str,
    extra: str = "",
    veto: Veto | None = None,
) -> LangSpec:
    pattern = "|".join(part for part in (_COMMON, extra) if part)
    return LangSpec(
        module=module,
        factory=factory,
        suffixes=frozenset(suffixes.split()),
        keep=re.compile(pattern, re.IGNORECASE | re.DOTALL),
        veto=veto,
    )


LANGS: Final[dict[str, LangSpec]] = {
    "bash": _spec("tree_sitter_bash", "language", ".sh .bash", _SHELL_FAMILY),
    "zsh": _spec("tree_sitter_zsh", "language", ".zsh", _SHELL_FAMILY),
    "c": _spec("tree_sitter_c", "language", ".c .h", _C_FAMILY),
    "cpp": _spec("tree_sitter_cpp", "language", ".cc .cpp .cxx .hpp .hh .hxx", _C_FAMILY),
    "css": _spec(
        "tree_sitter_css",
        "language",
        ".css",
        r"\A/\*!|sourceMappingURL|@charset|prettier-ignore|stylelint",
    ),
    "go": _spec(
        "tree_sitter_go",
        "language",
        ".go",
        r"\A//go:|\A//\s*\+build|\A//line |\A//export |#cgo|lint:ignore|\A//\s*Code generated",
        veto=_is_cgo_preamble,
    ),
    "html": _spec(
        "tree_sitter_html",
        "language",
        ".html .htm",
        r"\A<!--\s*\[if|\[endif\]|\A<!--!|prettier-ignore"
        r"|\A<!--\s*#(?:include|exec|echo|config|set|if|else|endif)",
    ),
    "javascript": _spec("tree_sitter_javascript", "language", ".js .mjs .cjs .jsx", _JS_FAMILY),
    "typescript": _spec("tree_sitter_typescript", "language_typescript", ".ts .mts .cts", _JS_FAMILY),
    "tsx": _spec("tree_sitter_typescript", "language_tsx", ".tsx", _JS_FAMILY),
    "json": _spec("tree_sitter_json", "language", ".json .jsonc"),
    "lua": _spec(
        "tree_sitter_lua",
        "language",
        ".lua",
        r"\A---\s*@|luacheck|stylua|selene|luacov|@diagnostic",
    ),
    "php": _spec(
        "tree_sitter_php",
        "language_php",
        ".php .phtml",
        r"\A#!|phpcs|phpstan|psalm|php-cs-fixer|noinspection|@var|@codeCoverageIgnore",
    ),
    "ruby": _spec(
        "tree_sitter_ruby",
        "language",
        ".rb .rake .gemspec",
        r"\A#!|frozen_string_literal|rubocop|:nodoc:|:nocov:|typed:|sorbet|steep:"
        r"|shareable_constant_value|warn_indent",
    ),
    "rust": _spec(
        "tree_sitter_rust",
        "language",
        ".rs",
        r"rustfmt|clippy|SAFETY:|cbindgen",
    ),
    "toml": _spec("tree_sitter_toml", "language", ".toml", r"taplo|\A#:schema|:schema"),
    "xml": _spec(
        "tree_sitter_xml",
        "language_xml",
        ".xml .xsd .xsl .xslt .svg",
        r"\A<!--!|\A<!--\s*\[if|noinspection|xmllint",
    ),
    "vim": _spec("tree_sitter_vim", "language", ".vim"),
    # --- languages adopted from the retired cleants.py variant ---------------
    "java": _spec(
        "tree_sitter_java",
        "language",
        ".java",
        r"\A//go:|@formatter:|NOSONAR|noinspection|checkstyle|spotbugs|pmd:",
    ),
    "latex": _spec(
        "tree_sitter_latex",
        "language",
        ".tex .sty .cls",
        r"%!(?:TEX|BIB)|\\iffalse|\\fi\b|spellcheck|chktex",
    ),
    "perl": _spec(
        "tree_sitter_perl",
        "language",
        ".pl .pm .t",
        r"\A#!|## no critic|## use critic|perltidy|@formatter",
    ),
    "python": _spec(
        "tree_sitter_python",
        "language",
        ".py .pyi .pyw",
        r"\A#!|#\s*(?:type|fmt|noqa|pylint|mypy|pyright|isort|flake8|coding)[:=]"
        r"|@formatter|#\s*region|#\s*endregion",
    ),
    "query": _spec("tree_sitter_query", "language", ".scm", r"@formatter|;\+inherit"),
    "rst": _spec("tree_sitter_rst", "language", ".rst", r"\.\.\s*(?:include|literalinclude|toctree|code-block|note|warning)::"),
    "sql": _spec(
        "tree_sitter_sql",
        "language",
        ".sql",
        r"--\s*(?:\+|migrate|nolint|noqa)|@formatter|flyway|sqruff",
    ),
    "yaml": _spec(
        "tree_sitter_yaml",
        "language",
        ".yaml .yml",
        r"#\s*yaml-language-server|#\s*vim?:|@formatter|#\s*pragma|yamllint|noinspection",
    ),
}
AVAILABLE: Final[dict[str, LangSpec]] = {
    name: spec for name, spec in LANGS.items() if importlib.util.find_spec(spec.module) is not None
}
SUFFIX_TO_LANG: Final[dict[str, str]] = {suffix: name for name, spec in AVAILABLE.items() for suffix in spec.suffixes}


class Task(NamedTuple):
    path: Path
    backup: bool
    max_size: int
    dry_run: bool


@dataclass(frozen=True, slots=True)
class Outcome:
    path: str
    status: Status
    before: int = 0
    after: int = 0
    detail: str = ""

    @property
    def saved(self) -> int:
        return self.before - self.after


@dataclass(slots=True)
class Summary:
    counts: Counter[str] = field(default_factory=Counter)
    freed: int = 0
    stripped_before: int = 0

    def record(self, outcome: Outcome) -> None:
        self.counts[outcome.status] += 1
        if outcome.status == "stripped":
            self.freed += outcome.saved
            self.stripped_before += outcome.before


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    raise AssertionError


def iter_files(root: Path, skip_dirs: frozenset[str]) -> Iterator[Path]:
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            for entry in current.iterdir():
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name not in skip_dirs:
                        stack.append(entry)
                elif entry.is_file() and entry.suffix.lower() in SUFFIX_TO_LANG:
                    yield entry
        except OSError as exc:
            logger.error("{} | cannot list directory: {}", current, exc)


@cache
def _parser(name: str) -> Parser:
    spec = AVAILABLE[name]
    module = importlib.import_module(spec.module)
    return Parser(Language(getattr(module, spec.factory)()))


def walk(root: Node) -> Iterator[Node]:
    stack: list[Node] = [root]
    while stack:
        node = stack.pop()
        yield node
        if node.type not in COMMENT_TYPES:
            stack.extend(reversed(node.children))


def _flush(digest: hashlib._Hash, pending: list[bytes]) -> None:
    if pending:
        digest.update(b"T\0" + b" ".join(b"".join(pending).split()) + b"\0")
        pending.clear()


def fingerprint(root: Node, src: bytes) -> bytes:
    digest = hashlib.blake2b(digest_size=16)
    pending: list[bytes] = []
    for node in walk(root):
        if node.type in COMMENT_TYPES or node.child_count:
            continue
        chunk = src[node.start_byte : node.end_byte]
        if node.type in TEXT_TYPES:
            pending.append(chunk)
            continue
        _flush(digest, pending)
        digest.update(node.type.encode() + b"\0" + chunk + b"\0")
    _flush(digest, pending)
    return digest.digest()


def _expand(src: bytes, start: int, end: int) -> tuple[int, int]:
    line_start = src.rfind(b"\n", 0, start) + 1
    newline = src.find(b"\n", end)
    line_end = len(src) if newline == -1 else newline
    head = src[line_start:start]
    tail = src[end:line_end]
    if tail.strip():
        return start, end
    if head.strip():
        return start - (len(head) - len(head.rstrip())), end
    return line_start, min(line_end + 1, len(src))


def removal_spans(root: Node, src: bytes, spec: LangSpec) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    floor = 0
    for node in walk(root):
        if node.type not in COMMENT_TYPES:
            continue
        start, end = node.start_byte, node.end_byte
        while end > start and src[end - 1] in b"\r\n":
            end -= 1
        text = src[start:end].decode("utf-8", "replace")
        if spec.keep.search(text) or (spec.veto is not None and spec.veto(node, src)):
            continue
        start, end = _expand(src, start, end)
        start = max(start, floor)
        if start >= end:
            continue
        spans.append((start, end))
        floor = end
    return spans


def strip_comments(data: bytes, lang: str) -> bytes:
    spec = AVAILABLE[lang]
    parser = _parser(lang)
    root = parser.parse(data).root_node
    if root.has_error:
        msg = "source has syntax errors"
        raise SkipFile(msg)
    spans = removal_spans(root, data, spec)
    if not spans:
        return data
    view = memoryview(data)
    parts: list[memoryview] = []
    cursor = 0
    for start, end in spans:
        parts.append(view[cursor:start])
        cursor = end
    parts.append(view[cursor:])
    result = b"".join(parts)
    new_root = parser.parse(result).root_node
    if new_root.has_error:
        msg = "stripped result has syntax errors"
        raise ValidationError(msg)
    if fingerprint(new_root, result) != fingerprint(root, data):
        msg = "stripped result changed the token stream"
        raise ValidationError(msg)
    return result


def commit(path: Path, payload: bytes, backup: bool, ref: os.stat_result) -> None:
    current = path.stat()
    if (current.st_mtime_ns, current.st_size) != (ref.st_mtime_ns, ref.st_size):
        msg = "file modified concurrently"
        raise SkipFile(msg)
    if backup:
        shutil.copy2(path, path.with_name(f"{path.name}.bak"))
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def process_file(path: Path, backup: bool, max_size: int, dry_run: bool) -> Outcome:
    name = str(path)
    try:
        if path.is_symlink():
            msg = "symlink"
            raise SkipFile(msg)
        lang = SUFFIX_TO_LANG[path.suffix.lower()]
        ref = path.stat()
        if not 0 < ref.st_size <= max_size:
            msg = f"size {ref.st_size} outside accepted range"
            raise SkipFile(msg)
        data = path.read_bytes()
        if b"\0" in data:
            msg = "binary content"
            raise SkipFile(msg)
        result = strip_comments(data, lang)
        if result == data:
            return Outcome(name, "unchanged", len(data), len(data))
        if not dry_run:
            commit(path, result, backup, ref)
        return Outcome(name, "stripped", len(data), len(result))
    except SkipFile as exc:
        return Outcome(name, "skipped", detail=str(exc))
    except Exception as exc:
        return Outcome(name, "error", detail=f"{type(exc).__name__}: {exc}")


def _worker(task: Task) -> Outcome:
    return process_file(*task)


def run_pool(pool: Pool, method: PoolMethod, tasks: Iterable[Task], chunksize: int) -> Iterator[Outcome]:
    match method:
        case "imap_unordered":
            yield from pool.imap_unordered(_worker, tasks, chunksize)
        case "imap":
            yield from pool.imap(_worker, tasks, chunksize)
        case "map":
            yield from pool.map(_worker, tasks, chunksize)
        case "starmap":
            yield from pool.starmap(process_file, tasks, chunksize)
        case "apply_async":
            pending = [pool.apply_async(process_file, task) for task in tasks]
            for result in pending:
                yield result.get()
        case "apply":
            for task in tasks:
                yield pool.apply(process_file, task)
        case _:
            msg = f"unknown pool method: {method}"
            raise ValueError(msg)


def report(outcome: Outcome) -> None:
    match outcome:
        case Outcome(status="stripped"):
            logger.info(
                "{} | -{} B ({} -> {} B)",
                outcome.path,
                outcome.saved,
                outcome.before,
                outcome.after,
            )
        case Outcome(status="skipped"):
            logger.debug("{} | skipped: {}", outcome.path, outcome.detail)
        case Outcome(status="error"):
            logger.error("{} | {}", outcome.path, outcome.detail)
        case _:
            logger.debug("{} | unchanged", outcome.path)


def configure_logging(verbose: bool, log_file: Path | None) -> None:
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if verbose else "INFO",
        format="{time:HH:mm:ss} | {level: <8} | {message}",
    )
    if log_file is not None:
        logger.add(log_file, level="DEBUG", rotation="10 MB", encoding="utf-8")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="strip_comments")
    parser.add_argument("root", nargs="?", type=Path, default=Path())
    parser.add_argument("-b", "--backup", action="store_true")
    parser.add_argument("-n", "--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--pool-method", choices=POOL_METHODS, default="imap_unordered")
    parser.add_argument("--chunksize", type=int, default=8)
    parser.add_argument("--max-size", type=int, default=5 * 1024 * 1024)
    parser.add_argument("--skip-dir", action="append", default=[])
    parser.add_argument("--log-file", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(args.verbose, args.log_file)
    root: Path = args.root.resolve()
    if not root.is_dir():
        logger.error("{} is not a directory", root)
        return 2
    skip_dirs = DEFAULT_SKIP_DIRS | frozenset(args.skip_dir)
    logger.info("languages: {}", ", ".join(sorted(AVAILABLE)) or "none")
    tasks = (Task(path, args.backup, args.max_size, args.dry_run) for path in iter_files(root, skip_dirs))
    summary = Summary()
    with mp.Pool(processes=max(1, args.jobs)) as pool:
        for outcome in run_pool(pool, args.pool_method, tasks, args.chunksize):
            report(outcome)
            summary.record(outcome)
    logger.info(
        "stripped={} unchanged={} skipped={} errors={}",
        summary.counts["stripped"],
        summary.counts["unchanged"],
        summary.counts["skipped"],
        summary.counts["error"],
    )
    logger.info(
        "{}freed {} of {} in stripped files",
        "[dry-run] would have " if args.dry_run else "",
        human(summary.freed),
        human(summary.stripped_before),
    )
    return int(summary.counts["error"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
"""

_SOURCES['pystrip'] = r"""#!/data/data/com.termux/files/usr/bin/env python

import argparse
import ast
import sys
from dataclasses import dataclass
from multiprocessing import Pool
from pathlib import Path
from typing import TYPE_CHECKING

import libcst as cst
from libcst import RemovalSentinel
from libcst.metadata import MetadataWrapper, PositionProvider
from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
SKIP_DIRS: frozenset[str] = frozenset({".git", "__pycache__"})
PRESERVED_COMMENT_MARKERS: tuple[str, ...] = ("# fmt", "# type")
DEFAULT_POOL_SIZE: int = 8


@dataclass
class FileResult:
    path: str
    comments_removed: int = 0
    docstrings_removed: int = 0
    changed: bool = False
    error: str | None = None


def iter_python_files(paths: Iterable[Path]) -> Iterator[Path]:
    seen: set[Path] = set()
    for input_path in paths:
        path: Path = input_path.expanduser()
        try:
            if path.is_symlink():
                continue
            if path.is_file():
                if path.suffix == ".py":
                    resolved: Path = path.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        yield path
                continue
            if not path.is_dir():
                logger.warning(f"not found or unsupported: {path}")
                continue
            for child in path.rglob("*"):
                if not child.is_file() or child.suffix != ".py":
                    continue
                if child.is_symlink():
                    continue
                try:
                    resolved = child.resolve()
                except OSError:
                    continue
                if any(part in SKIP_DIRS for part in child.parts):
                    continue
                if resolved in seen:
                    continue
                seen.add(resolved)
                yield child
        except OSError as exc:
            logger.warning(f"cannot traverse {path}: {exc}")


def _comment_should_be_preserved(text: str, is_first_line: bool) -> bool:
    stripped: str = text.lstrip()
    if is_first_line and stripped.startswith("#!"):
        return True
    lower: str = text.lower()
    return any(marker in lower for marker in PRESERVED_COMMENT_MARKERS)


class _Transformer(cst.CSTTransformer):
    def __init__(self, remove_module_docstring: bool) -> None:
        self.remove_module_docstring: bool = remove_module_docstring
        self.comments_removed: int = 0
        self.docstrings_removed: int = 0

    def leave_Comment(self, original_node: cst.Comment, updated_node: cst.Comment) -> cst.Comment | RemovalSentinel:
        pos = self.get_metadata(PositionProvider, original_node, None)
        is_first_line: bool = pos is not None and pos.start.line == 1
        if _comment_should_be_preserved(original_node.value, is_first_line):
            return updated_node
        self.comments_removed += 1
        return cst.RemoveFromParent()

    def _handle_docstring(
        self,
        body: cst.IndentedBlock,
        remove: bool,
    ) -> cst.IndentedBlock:
        if not remove:
            return body
        statements: list[cst.BaseStatement] = list(body.body)
        if not statements:
            return body
        first: cst.BaseStatement = statements[0]
        if not (
            isinstance(first, cst.SimpleStatementLine)
            and first.body
            and isinstance(first.body[0], cst.Expr)
            and isinstance(first.body[0].value, cst.SimpleString)
        ):
            return body
        self.docstrings_removed += 1
        if len(statements) == 1:
            replacement: cst.BaseStatement = cst.SimpleStatementLine(body=[cst.Pass()])
            new_statements: list[cst.BaseStatement] = [replacement]
        else:
            new_statements = statements[1:]
        return body.with_changes(body=new_statements)

    def leave_FunctionDef(
        self,
        original_node: cst.FunctionDef,
        updated_node: cst.FunctionDef,
    ) -> cst.FunctionDef:
        new_body: cst.BaseSuite = updated_node.body
        if isinstance(new_body, cst.IndentedBlock):
            new_body = self._handle_docstring(new_body, True)
        return updated_node.with_changes(body=new_body)

    def leave_ClassDef(
        self,
        original_node: cst.ClassDef,
        updated_node: cst.ClassDef,
    ) -> cst.ClassDef:
        new_body: cst.BaseSuite = updated_node.body
        if isinstance(new_body, cst.IndentedBlock):
            new_body = self._handle_docstring(new_body, True)
        return updated_node.with_changes(body=new_body)

    def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:
        new_body: cst.BaseSuite = updated_node.body
        if isinstance(new_body, cst.IndentedBlock):
            new_body = self._handle_docstring(new_body, self.remove_module_docstring)
        return updated_node.with_changes(body=new_body)


def _remove_blank_lines(data: str) -> str:
    lines: list[str] = data.splitlines(keepends=True)
    result: list[str] = []
    for line in lines:
        if line.strip() == "":
            continue
        result.append(line)
    return "".join(result)


def process_file(path_str: str, remove_module_docstring: bool) -> FileResult:
    path: Path = Path(path_str)
    result: FileResult = FileResult(path=str(path))
    try:
        source_bytes: bytes = path.read_bytes()
        try:
            source: str = source_bytes.decode("utf-8")
        except UnicodeDecodeError:
            source = source_bytes.decode("utf-8-sig")
        try:
            ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            result.error = f"SyntaxError: {exc}"
            return result
        module: cst.Module = cst.parse_module(source)
        wrapper: MetadataWrapper = MetadataWrapper(module)
        transformer: _Transformer = _Transformer(remove_module_docstring)
        try:
            updated_module: cst.Module = wrapper.visit(transformer)
        except Exception:
            transformer = _Transformer(remove_module_docstring)
            updated_module = module.visit(transformer)
        updated_text: str = updated_module.code
        updated_text = _remove_blank_lines(updated_text)
        if not updated_text.endswith("\n") and updated_text:
            updated_text += "\n"
        ast.parse(updated_text, filename=str(path))
        if updated_text == source:
            return result
        path.write_text(updated_text, encoding="utf-8")
        result.comments_removed = transformer.comments_removed
        result.docstrings_removed = transformer.docstrings_removed
        result.changed = True
        return result
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        return result


def parse_args() -> argparse.Namespace:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=(
            "Remove Python comments and docstrings recursively, preserving "
            "shebangs, # fmt, # type, and module docstrings by default."
        )
    )
    parser.add_argument(
        "-r",
        "--remove-module-docstring",
        action="store_true",
        help="remove module-level docstrings too",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="files and/or directories to process (default: current directory)",
    )
    return parser.parse_args()


def main() -> int:
    args: argparse.Namespace = parse_args()
    input_paths: list[Path] = args.paths or [Path()]
    files: list[Path] = list(iter_python_files(input_paths))
    if not files:
        print("No Python files found.")
        return 0
    changed_files: int = 0
    total_comments: int = 0
    total_docstrings: int = 0
    errors: int = 0
    pool: Pool = Pool(processes=DEFAULT_POOL_SIZE)
    try:
        async_results: list[tuple[Path, object]] = [
            (
                path,
                pool.apply_async(
                    process_file,
                    (str(path), args.remove_module_docstring),
                ),
            )
            for path in files
        ]
        pool.close()
        for path, async_result in async_results:
            try:
                result: FileResult = async_result.get()
            except Exception as exc:
                errors += 1
                logger.error(f"{path}: {type(exc).__name__}: {exc}")
                continue
            if result.error:
                errors += 1
                logger.error(f"{result.path}: {result.error}")
                continue
            if result.changed:
                changed_files += 1
                total_comments += result.comments_removed
                total_docstrings += result.docstrings_removed
                print(
                    f"{result.path}: comments removed={result.comments_removed}, "
                    f"docstrings removed={result.docstrings_removed}"
                )
            else:
                print(f"{result.path}: no changes")
        pool.join()
    finally:
        pool.terminate()
    print(
        "Summary: "
        f"files changed={changed_files}, "
        f"comments removed={total_comments}, "
        f"docstrings removed={total_docstrings}, "
        f"errors={errors}"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
"""

_SOURCES['minify'] = r'''#!/data/data/com.termux/files/usr/bin/env python
"""Language-aware comment stripper plus optional minifier.

Merges the six near-identical ``clean-{css,html,js,ts,lua,toml}.py`` scripts
from ``g001`` into a single engine driven by a ``LangSpec`` table.  Behaviour
kept from the originals:

* tree-sitter comment removal followed by a re-parse sanity check (a file that
  no longer parses is reported as an error and left untouched),
* the optional third-party compressor backends (``csscompressor``, ``tinycss2``,
  ``htmlmin``, ``rjsmin``, ``jsmin``, ``luaparse``),
* HTML handling that additionally strips comments inside embedded ``<script>``
  and ``<style>`` blocks,
* Lua ``---`` doc comments are preserved,
* TOML output is validated with ``tomllib`` before it is written,
* ``--dry-run`` / ``--diff`` / ``--backup`` and a multiprocessing pool.

Fixes applied while merging:

* the originals walked with ``Path.walk()``, which only exists on Python 3.12+;
  this version uses ``os.walk`` so it runs everywhere,
* tree-sitter capture handling tolerates both the ``dict`` (<= 0.23) and the
  list-based (>= 0.24) ``QueryCursor.captures`` return shapes,
* ``--backend`` is validated against the backends the selected languages
  actually support instead of silently falling back to tree-sitter.
"""


import argparse
import difflib
import multiprocessing
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

WORKERS: int = os.cpu_count() or 1
SKIP_DIRS: frozenset[str] = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".idea", ".vscode", "dist", "build"}
)


@dataclass(frozen=True, slots=True)
class LangSpec:
    """Everything the engine needs to know about one language."""

    name: str
    module: str
    factory: str
    exts: tuple[str, ...]
    backends: tuple[str, ...] = ()
    keep_prefixes: tuple[bytes, ...] = ()
    validate: str = "tree"


LANG_SPECS: tuple[LangSpec, ...] = (
    LangSpec("css", "tree_sitter_css", "language", (".css",), ("csscompressor", "tinycss2")),
    LangSpec("html", "tree_sitter_html", "language", (".html", ".htm"), ("htmlmin",)),
    LangSpec("js", "tree_sitter_javascript", "language", (".js", ".mjs", ".cjs", ".jsx"), ("rjsmin", "jsmin")),
    LangSpec("ts", "tree_sitter_typescript", "language_typescript", (".ts", ".mts", ".cts"), ("rjsmin", "jsmin")),
    LangSpec("tsx", "tree_sitter_typescript", "language_tsx", (".tsx",), ("rjsmin", "jsmin")),
    LangSpec("lua", "tree_sitter_lua", "language", (".lua",), ("luaparse",), (b"---",)),
    LangSpec("toml", "tree_sitter_toml", "language", (".toml",), (), (), "tomllib"),
)
EXTS: dict[str, LangSpec] = {ext: spec for spec in LANG_SPECS for ext in spec.exts}
BACKENDS: frozenset[str] = frozenset({"tree-sitter"}).union(*(s.backends for s in LANG_SPECS))


def _import(missing: list[str], name: str) -> Any:
    try:
        return __import__(name)
    except ImportError:
        if name not in missing:
            missing.append(name)
        return None


def _language(spec: LangSpec, missing: list[str]) -> Any:
    from tree_sitter import Language

    module = _import(missing, spec.module)
    if module is None:
        return None
    return Language(getattr(module, spec.factory)())


def _captures(query: Any, node: Any) -> list[Any]:
    """Return the captured nodes for *query*, whatever tree-sitter version is installed."""
    from tree_sitter import QueryCursor

    cursor = QueryCursor(query)
    try:
        result = cursor.captures(node)
    except TypeError:
        try:
            result = cursor.captures(node, "comment")
        except Exception:
            return []
    if isinstance(result, dict):
        nodes: list[Any] = []
        for value in result.values():
            nodes.extend(value)
        return nodes
    out: list[Any] = []
    for item in result:
        out.append(item[0] if isinstance(item, tuple) else item)
    return out


def _comment_ranges(src: bytes, spec: LangSpec, root: Any, language: Any, missing: list[str]) -> list[tuple[int, int]]:
    from tree_sitter import Query

    ranges: list[tuple[int, int]] = []
    for node in _captures(Query(language, "(comment) @comment"), root):
        text = src[node.start_byte : node.end_byte]
        if any(text.lstrip().startswith(prefix) for prefix in spec.keep_prefixes):
            continue
        ranges.append((node.start_byte, node.end_byte))
    if spec.name == "html":
        ranges.extend(_embedded_ranges(src, root, language, missing))
    return ranges


def _embedded_ranges(src: bytes, root: Any, html_language: Any, missing: list[str]) -> list[tuple[int, int]]:
    """Comments living inside <style>/<script> blocks of an HTML document."""
    from tree_sitter import Language, Parser, Query

    ranges: list[tuple[int, int]] = []
    for capture, spec in (("style", LANG_SPECS[0]), ("script", LANG_SPECS[2])):
        for node in _captures(Query(html_language, f"({capture}_element) @{capture}"), root):
            module = _import(missing, spec.module)
            if module is None:
                continue
            inner = Language(getattr(module, spec.factory)())
            sub_tree = Parser(inner).parse(src[node.start_byte : node.end_byte])
            for sub_node in _captures(Query(inner, "(comment) @comment"), sub_tree.root_node):
                ranges.append(
                    (node.start_byte + sub_node.start_byte, node.start_byte + sub_node.end_byte)
                )
    return ranges


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _drop_ranges(src: bytes, ranges: list[tuple[int, int]]) -> bytes:
    parts: list[bytes] = []
    cursor = 0
    for start, end in _merge(ranges):
        parts.append(src[cursor:start])
        cursor = end
    parts.append(src[cursor:])
    return b"".join(parts)


def _valid(spec: LangSpec, data: bytes, missing: list[str]) -> bool:
    if spec.validate == "tomllib":
        try:
            import tomllib  # type: ignore[import-not-found]
        except ImportError:  # Python < 3.11
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError:
                tomllib = None  # type: ignore[assignment]
        if tomllib is None:
            return True
        try:
            tomllib.loads(data.decode("utf-8"))
        except Exception:
            return False
        return True
    from tree_sitter import Parser

    language = _language(spec, missing)
    if language is None:
        return False
    return not Parser(language).parse(data).root_node.has_error


def strip_comments(raw: bytes, spec: LangSpec) -> tuple[bytes, bool, str]:
    """Remove comments using tree-sitter. Returns ``(data, ok, detail)``."""
    if not raw:
        return raw, True, ""
    missing: list[str] = []
    try:
        from tree_sitter import Parser
    except ImportError:
        return b"", False, "tree-sitter is not installed (pip install tree-sitter)"
    language = _language(spec, missing)
    if language is None:
        return b"", False, f"missing {', '.join(missing)}"
    parser = Parser(language)
    tree = parser.parse(raw)
    if tree.root_node.has_error:
        return b"", False, f"{spec.name}: source does not parse"
    ranges = _comment_ranges(raw, spec, tree.root_node, language, missing)
    if not ranges:
        return raw, True, ""
    stripped = _drop_ranges(raw, ranges)
    if not _valid(spec, stripped, missing):
        return b"", False, f"{spec.name}: result does not re-parse; left untouched"
    return stripped, True, ""


def run_backend(raw: bytes, spec: LangSpec, backend: str) -> tuple[bytes, bool, str]:
    if backend == "tree-sitter":
        return strip_comments(raw, spec)
    try:
        module = __import__(backend)
    except ImportError:
        return b"", False, f"{backend} is not installed (pip install {backend})"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return b"", False, "source is not valid UTF-8"
    try:
        if backend in {"rjsmin", "jsmin"}:
            out = module.jsmin(text)
        elif backend == "csscompressor":
            out = module.compress(text)
        elif backend == "htmlmin":
            out = module.minify(text, remove_comments=True, remove_empty_space=True)
        elif backend == "tinycss2":
            tokens = module.parse_stylesheet(text, skip_comments=True)
            if any(isinstance(t, module.ast.ParseError) for t in tokens):
                return b"", False, "tinycss2 reported a parse error"
            out = module.serialize(tokens)
        elif backend == "luaparse":
            module.parse(text)
            out = text
        else:
            return b"", False, f"unsupported backend {backend!r}"
    except Exception as exc:  # third-party compressors raise anything
        return b"", False, f"{backend} failed: {exc}"
    data = out.encode("utf-8")
    if not _valid(spec, data, []):
        return b"", False, f"{backend}: result does not re-parse; left untouched"
    return data, True, ""


def iter_files(paths: list[str], wanted: dict[str, LangSpec]) -> Iterator[str]:
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists() or path.is_symlink():
            continue
        if path.is_file():
            if not path.name.startswith(".") and path.suffix.lower() in wanted:
                yield str(path.resolve())
            continue
        for root, dirs, files in os.walk(path, followlinks=False):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for name in files:
                if name.startswith("."):
                    continue
                if Path(name).suffix.lower() in wanted:
                    full = Path(root) / name
                    if not full.is_symlink():
                        yield str(full.resolve())


def process_file(task: tuple[str, str, str, bool, bool]) -> tuple[str, int, int, int, str | None, str, str]:
    file_path, base_dir, backend, dry_run, backup = task
    path = Path(file_path)
    rel = os.path.relpath(file_path, base_dir)
    spec = EXTS.get(path.suffix.lower())
    if spec is None:
        return rel, 0, 0, 0, f"unsupported suffix {path.suffix!r}", "", ""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return rel, 0, 0, 0, f"read error: {exc}", "", ""
    if not raw:
        return rel, 0, 0, 0, None, "", ""
    try:
        old_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return rel, 0, 0, 0, "not valid UTF-8", "", ""
    new, ok, detail = run_backend(raw, spec, backend)
    if not ok:
        return rel, 0, 0, 0, detail, "", ""
    try:
        new_text = new.decode("utf-8")
    except UnicodeDecodeError:
        return rel, 0, 0, 0, "result is not valid UTF-8", "", ""
    saved = len(raw) - len(new)
    if saved > 0 and not dry_run:
        try:
            if backup:
                shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
            path.write_bytes(new)
        except OSError as exc:
            return rel, 0, 0, 0, f"write error: {exc}", "", ""
    return rel, len(raw), len(new), max(saved, 0), None, old_text, new_text


def _report(rel: str, saved: int, ok: bool) -> None:
    marker = "\033[32m" if ok else "\033[31m"
    print(f"{rel}  {marker}{saved}\033[0m")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="minify",
        description="Strip comments (tree-sitter) and optionally minify css/html/js/ts/lua/toml files.",
    )
    parser.add_argument("paths", nargs="*", default=[], help="Files or directories (default: .).")
    parser.add_argument("-l", "--lang", action="append", choices=[s.name for s in LANG_SPECS],
                        help="Restrict to these languages (default: every supported one).")
    parser.add_argument("-b", "--backend", default="tree-sitter", choices=sorted(BACKENDS),
                        help="tree-sitter strips comments; the other backends run a real minifier.")
    parser.add_argument("--diff", action="store_true", help="Print a unified diff for every changed file.")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Report only, write nothing.")
    parser.add_argument("--backup", action="store_true", help="Keep a .bak copy of rewritten files.")
    parser.add_argument("-j", "--jobs", type=int, default=WORKERS, help=f"Worker processes (default: {WORKERS}).")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    selected = [s for s in LANG_SPECS if not args.lang or s.name in args.lang]
    if args.backend != "tree-sitter" and not any(args.backend in s.backends for s in selected):
        parser.error(f"backend {args.backend!r} is not available for the selected languages")
    wanted = {ext: spec for spec in selected for ext in spec.exts}
    inputs = args.paths or ["."]
    base_dir = os.getcwd()
    files = list(iter_files(inputs, wanted))
    if not files:
        print("No matching files.", file=sys.stderr)
        return 0
    tasks = [(f, base_dir, args.backend, args.dry_run, args.backup) for f in files]
    pool: Any = None
    total = 0
    errors = 0
    try:
        results: Iterator[Any]
        if len(tasks) == 1 or args.jobs <= 1:
            results = iter([process_file(task) for task in tasks])
        else:
            pool = multiprocessing.Pool(max(1, args.jobs))
            results = pool.imap_unordered(process_file, tasks)
        for rel, _old, _new, saved, error, old_text, new_text in results:
            if error:
                errors += 1
                print(f"{rel}: {error}", file=sys.stderr)
                continue
            total += saved
            _report(rel, saved, True)
            if args.diff and saved > 0:
                sys.stdout.writelines(
                    difflib.unified_diff(
                        old_text.splitlines(keepends=True),
                        new_text.splitlines(keepends=True),
                        fromfile=f"a/{rel}",
                        tofile=f"b/{rel}",
                    )
                )
    finally:
        if pool is not None:
            pool.close()
            pool.join()
    print(f"Total space freed: {total} bytes ({len(files)} file(s), {errors} error(s))")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
'''



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
