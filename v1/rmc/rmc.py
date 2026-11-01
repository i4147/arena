#!/data/data/com.termux/files/usr/bin/env python3
"""rmc - consolidated comment removal toolkit.

Everything that used to live in this folder as separate scripts now lives in
this one file.  Each tool's original source is embedded verbatim below, so
nothing was lost: ``python rmc.py unpack <tool>`` writes the original
file back out.

29 iterations of the same comment remover collapse into two tools: the
most complete stripper (three interchangeable engines) and the unused-import/definition cleaner.

Tools
-----
strip  Strip comments/docstrings with ast, tree-sitter or libcst backends; pool methods, check mode, JSON reports, backups, rich progress.
       from tmc.py (1291 lines)
clean  Find and remove unused imports/definitions, process vulture output, and patch or extract functions and blocks.
       from pycleaner.py (1275 lines)

Usage
-----
    python rmc.py list                 # list the tools
    python rmc.py doctor               # report missing third-party packages
    python rmc.py <tool> --help        # help for one tool
    python rmc.py strip <args>
    python rmc.py clean <args>
    python rmc.py unpack <tool> [-o DIR] [--stdout]

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

FOLDER = "rmc"


_HERE = Path(__file__).resolve().parent if "__file__" in globals() else None

TOOLS: dict[str, dict[str, object]] = {
    "strip": {"source": "tmc.py", "lines": 1291, "summary": 'Strip comments/docstrings with ast, tree-sitter or libcst backends; pool methods, check mode, JSON reports, backups, rich progress.'},
    "clean": {"source": "pycleaner.py", "lines": 1275, "summary": 'Find and remove unused imports/definitions, process vulture output, and patch or extract functions and blocks.'},
}

_SOURCES: dict[str, str] = {}
_LOADED: dict[str, types.ModuleType] = {}


def _decode(*chunks: str) -> str:
    """Fallback representation for sources that cannot be written as raw strings."""
    return zlib.decompress(base64.b85decode("".join(chunks))).decode("utf-8")


_SOURCES['strip'] = r'''#!/data/data/com.termux/files/usr/bin/env python

import argparse
import ast
import concurrent.futures as cf
import difflib
import fnmatch
import importlib
import importlib.util
import io
import json
import multiprocessing as mp
import os
import re
import shlex
import stat
import sys
import tempfile
import threading
import tokenize
import traceback
from collections.abc import Callable, Iterable, Iterator
import contextlib
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from multiprocessing.pool import ThreadPool
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, Protocol, cast

def atomic_write(path: Path, data: bytes | str) -> None:
    """Write *data* to *path* atomically.

    Local replacement for the ``dh`` helper import that this file used to pull
    from a module which is not part of the repository.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(tmp_fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(tmp_name, stat.S_IMODE(path.stat().st_mode))
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise

if TYPE_CHECKING:
    from libcst import CSTTransformer
    from rich.progress import Progress, TaskID

    _CSTTypes = tuple[type[CSTTransformer], type[CSTTransformer]]
from loguru import logger

_Edit = tuple[int, int, bytes]


class SkipFile(Exception):
    pass


@dataclass
class StripFileResult:
    path: Path
    saved: int = 0
    modified: bool = False
    error: str | None = None
    elapsed: float = 0.0
    diff: str = ""
    backup: Path | None = None
    traceback: str = ""
    skipped: bool = False
    skip_reason: str = ""


def parse_source(source: str) -> ast.Module:
    try:
        return ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        raise SkipFile(f"{type(exc).__name__}: {exc}") from None


class ASTStripper:
    _DIRECTIVE_RE = re.compile(
        r"^#\s*(?:type|fmt|mypy|pylint|ruff|pyright|basedpyright|noqa|"
        r"nosec|bandit|isort|yapf|autopep8|flake8|black|pyre|pytype|"
        r"pragma|coverage|doctest|pytest|cython|distutils|numba|"
        r"region|endregion|uv)\b|^#\s*(?:%%|</?editor-fold\b)",
        re.I,
    )
    _COOKIE_RE = re.compile(rb"coding[:=]\s*[-\w.]+")

    def __init__(
        self,
        strip_comments: bool,
        strip_docstrings: bool,
        remove_blank_lines: bool = False,
        strip_all: bool = False,
    ) -> None:
        self.strip_comments = strip_comments or strip_all
        self.strip_docstrings = strip_docstrings or strip_all
        self.strip_all = strip_all
        self.remove_blank_lines = remove_blank_lines

    @staticmethod
    def _line_start(src: bytes, pos: int) -> int:
        return src.rfind(b"\n", 0, pos) + 1

    @staticmethod
    def _line_end(src: bytes, pos: int) -> int:
        end = src.find(b"\n", pos)
        return len(src) if end < 0 else end + 1

    def _keep_comment(self, text: str, row: int) -> bool:
        if self.strip_all:
            return False
        return bool(
            (row == 0 and text.startswith("#!"))
            or (row < 2 and self._COOKIE_RE.search(text.encode("utf-8")))
            or self._DIRECTIVE_RE.match(text)
        )

    def _collect_comments(self, src: bytes) -> list[_Edit]:
        source = src.decode("utf-8")
        lines = io.StringIO(source).readlines()
        offsets: list[int] = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line.encode("utf-8")))
        edits: list[_Edit] = []
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type != tokenize.COMMENT:
                continue
            row, column = token.start
            if self._keep_comment(token.string, row - 1):
                continue
            line = lines[row - 1]
            line_start = offsets[row - 1]
            start = line_start + len(line[:column].encode("utf-8"))
            end = line_start + len(line[: token.end[1]].encode("utf-8"))
            if not src[line_start:start].strip(b" \t"):
                edits.append((line_start, self._line_end(src, end), b""))
            else:
                while start > line_start and src[start - 1 : start] in (b" ", b"\t"):
                    start -= 1
                edits.append((start, end, b""))
        return edits

    def _collect_docstrings(self, src: bytes, tree: ast.AST) -> list[_Edit]:
        lines = io.BytesIO(src).readlines()
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        edits: list[_Edit] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.Module) and not self.strip_all:
                continue
            if not node.body:
                continue
            first = node.body[0]
            if not (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                continue
            if first.end_lineno is None or first.end_col_offset is None:
                continue
            start = offsets[first.lineno - 1] + first.col_offset
            end = offsets[first.end_lineno - 1] + first.end_col_offset
            line_start = self._line_start(src, start)
            line_end = self._line_end(src, end)
            alone_on_lines = not src[line_start:start].strip() and not src[end:line_end].strip()
            next_is_string = (
                len(node.body) > 1
                and isinstance(node.body[1], ast.Expr)
                and isinstance(node.body[1].value, ast.Constant)
                and isinstance(node.body[1].value.value, str)
            )
            if alone_on_lines and not next_is_string and (isinstance(node, ast.Module) or len(node.body) > 1):
                edits.append((line_start, line_end, b""))
            elif isinstance(node, ast.Module) and not next_is_string:
                tail = end
                while tail < len(src) and src[tail : tail + 1] in (b" ", b"\t"):
                    tail += 1
                if src[tail : tail + 1] == b";":
                    tail += 1
                    while tail < len(src) and src[tail : tail + 1] in (b" ", b"\t"):
                        tail += 1
                    end = tail
                edits.append((start, end, b""))
            else:
                edits.append((start, end, b"pass"))
        return edits

    def strip(self, source: str) -> str:
        tree = parse_source(source)
        src = source.encode("utf-8")
        if self.strip_docstrings:
            src = self._apply(src, self._collect_docstrings(src, tree))
        if self.strip_comments and b"#" in src:
            src = self._apply(src, self._collect_comments(src))
        result = src.decode("utf-8")
        if self.remove_blank_lines:
            result = remove_blank_lines(result)
        elif result != source:
            ast.parse(result)
        return result

    @staticmethod
    def _apply(src: bytes, edits: list[_Edit]) -> bytes:
        out = bytearray()
        cursor = 0
        for start, end, replacement in sorted(edits):
            if start < cursor:
                raise ValueError("Overlapping source edits")
            out.extend(src[cursor:start])
            out.extend(replacement)
            cursor = end
        out.extend(src[cursor:])
        return bytes(out)

    def strip_file(
        self,
        path: Path,
        *,
        dryrun: bool = False,
        backup: bool = False,
        show_diff: bool = False,
    ) -> StripFileResult:
        started = perf_counter()
        result = StripFileResult(path)
        try:
            if path.is_symlink():
                raise ValueError("Refusing to replace a symbolic link")
            original = path.read_bytes()
            try:
                encoding, _ = tokenize.detect_encoding(io.BytesIO(original).readline)
                source = original.decode(encoding)
            except (SyntaxError, UnicodeDecodeError, LookupError) as exc:
                raise SkipFile(f"{type(exc).__name__}: {exc}") from None
            stripped = self.strip(source)
            if stripped != source:
                output_encoding = "utf-8" if self.strip_all and self.strip_comments else encoding
                data = stripped.encode(output_encoding)
                actual_encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
                if data.decode(actual_encoding) != stripped:
                    raise ValueError("Output encoding declaration does not match serialized source")
                result.saved = len(original) - len(data)
                result.modified = True
                if show_diff:
                    result.diff = make_diff(source, stripped, path.name)
                if not dryrun:
                    if backup:
                        if path.is_symlink() or path.read_bytes() != original:
                            raise RuntimeError("File changed while processing; not replaced")
                        result.backup = create_backup(path, original)
                    atomic_write(path, data)
        except SkipFile as exc:
            result.skipped = True
            result.skip_reason = str(exc)
            result.saved = 0
            result.modified = False
        except Exception as exc:
            result.error = f"{type(exc).__name__}: {exc}"
            result.traceback = traceback.format_exc()
            result.saved = 0
            result.modified = False
        result.elapsed = perf_counter() - started
        return result


def make_diff(before: str, after: str, name: str) -> str:
    output: list[str] = []
    for line in difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{name}",
        tofile=f"b/{name}",
    ):
        output.append(line)
        if not line.endswith("\n"):
            output.append("\n\\ No newline at end of file\n")
    return "".join(output)


def create_backup(path: Path, original: bytes) -> Path:
    backup = path.with_name(path.name + ".bak")
    with backup.open("xb") as stream:
        try:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
            backup.chmod(stat.S_IMODE(path.stat().st_mode))
        except BaseException:
            backup.unlink(missing_ok=True)
            raise
    return backup


def remove_blank_lines(source: str) -> str:
    protected: set[int] = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.start[0] != token.end[0]:
            protected.update(range(token.start[0], token.end[0] + 1))
    lines = io.StringIO(source).readlines()
    for row, line in enumerate(lines, 1):
        if line.rstrip("\r\n").endswith("\\"):
            protected.add(row + 1)
        if row <= 2 and ASTStripper._COOKIE_RE.search(line.encode("utf-8")):
            protected.update(range(1, row + 1))
    result = "".join(line for row, line in enumerate(lines, 1) if row in protected or line.strip(" \t\f\r\n"))
    before = ast.dump(ast.parse(source), include_attributes=False)
    after = ast.dump(ast.parse(result), include_attributes=False)
    if before != after:
        raise ValueError("Blank-line removal would change the AST; not written")
    return result


class TSStripper(ASTStripper):
    def __init__(
        self,
        strip_comments: bool,
        strip_docstrings: bool,
        remove_blank_lines: bool = False,
        strip_all: bool = False,
    ) -> None:
        super().__init__(strip_comments, strip_docstrings, remove_blank_lines, strip_all)
        import tree_sitter
        import tree_sitter_python

        self._parser = tree_sitter.Parser(tree_sitter.Language(tree_sitter_python.language()))

    def _collect_comments(self, src: bytes) -> list[_Edit]:
        edits: list[_Edit] = []
        cursor = self._parser.parse(src).walk()
        while True:
            node = cursor.node
            descend = True
            if node is not None and node.type == "comment":
                descend = False
                text = src[node.start_byte : node.end_byte].decode("utf-8")
                if not self._keep_comment(text, node.start_point.row):
                    start = self._line_start(src, node.start_byte)
                    if not src[start : node.start_byte].strip(b" \t"):
                        edits.append((start, self._line_end(src, node.end_byte), b""))
                    else:
                        start = node.start_byte
                        while start and src[start - 1 : start] in (b" ", b"\t"):
                            start -= 1
                        edits.append((start, node.end_byte, b""))
            if descend and cursor.goto_first_child():
                continue
            while not cursor.goto_next_sibling():
                if not cursor.goto_parent():
                    return edits


def _cst_transformer_types(strip_all: bool) -> _CSTTypes:
    import libcst as cst

    thread = threading.current_thread()
    existing = cast("dict[bool, _CSTTypes] | None", getattr(thread, "_strip_python_cst_types_v1", None))
    cache: dict[bool, _CSTTypes] = existing if existing is not None else {}
    if existing is None:
        setattr(thread, "_strip_python_cst_types_v1", cache)
    if strip_all in cache:
        return cache[strip_all]

    class _CommentRemover(cst.CSTTransformer):
        def __init__(self) -> None:
            super().__init__()
            self._header_comments: set[int] = set()

        def visit_Module(self, node: cst.Module) -> bool:
            if not strip_all:
                for row, line in enumerate(node.header[:2]):
                    comment = line.comment
                    if comment is not None and (
                        (row == 0 and comment.value.startswith("#!"))
                        or ASTStripper._COOKIE_RE.search(comment.value.encode("utf-8"))
                    ):
                        self._header_comments.add(id(comment))
            return True

        def _keep(self, comment: cst.Comment) -> bool:
            return not strip_all and bool(
                id(comment) in self._header_comments or ASTStripper._DIRECTIVE_RE.match(comment.value)
            )

        def leave_EmptyLine(
            self, original_node: cst.EmptyLine, updated_node: cst.EmptyLine
        ) -> cst.EmptyLine | cst.RemovalSentinel:
            if original_node.comment is not None and not self._keep(original_node.comment):
                return cst.RemoveFromParent()
            return updated_node

        def leave_TrailingWhitespace(
            self,
            original_node: cst.TrailingWhitespace,
            updated_node: cst.TrailingWhitespace,
        ) -> cst.TrailingWhitespace:
            if original_node.comment is not None and not self._keep(original_node.comment):
                return updated_node.with_changes(comment=None, whitespace=cst.SimpleWhitespace(""))
            return updated_node

    class _DocstringRemover(cst.CSTTransformer):
        @staticmethod
        def _is_docstring(expr: cst.CSTNode) -> bool:
            if not isinstance(expr, cst.Expr):
                return False
            if not isinstance(expr.value, (cst.SimpleString, cst.ConcatenatedString)):
                return False
            try:
                value = ast.literal_eval(cst.Module([]).code_for_node(expr.value))
            except (ValueError, SyntaxError):
                return False
            return isinstance(value, str)

        def _strip_suite(self, suite: cst.BaseSuite) -> cst.BaseSuite:
            if isinstance(suite, cst.SimpleStatementSuite):
                if suite.body and self._is_docstring(suite.body[0]):
                    first = suite.body[0]
                    return suite.with_changes(body=[cst.Pass(semicolon=first.semicolon), *suite.body[1:]])
                return suite
            if not isinstance(suite, cst.IndentedBlock) or not suite.body:
                return suite
            first = suite.body[0]
            if not isinstance(first, cst.SimpleStatementLine) or not first.body:
                return suite
            if not self._is_docstring(first.body[0]):
                return suite
            if (
                len(first.body) == 1
                and len(suite.body) > 1
                and first.trailing_whitespace.comment is None
                and not (
                    isinstance(suite.body[1], cst.SimpleStatementLine) and self._is_docstring(suite.body[1].body[0])
                )
            ):
                rest = list(suite.body[1:])
                next_stmt = rest[0]
                assert isinstance(next_stmt, (cst.SimpleStatementLine, cst.BaseCompoundStatement))
                rest[0] = next_stmt.with_changes(leading_lines=(*first.leading_lines, *next_stmt.leading_lines))
                return suite.with_changes(body=rest)
            replacement = first.with_changes(body=[cst.Pass(semicolon=first.body[0].semicolon), *first.body[1:]])
            return suite.with_changes(body=[replacement, *suite.body[1:]])

        def leave_Module(self, original_node: cst.Module, updated_node: cst.Module) -> cst.Module:
            if not strip_all or not updated_node.body:
                return updated_node
            first = updated_node.body[0]
            if not isinstance(first, cst.SimpleStatementLine) or not first.body:
                return updated_node
            if not self._is_docstring(first.body[0]):
                return updated_node
            if len(first.body) > 1:
                remaining = (
                    [cst.Pass(semicolon=first.body[0].semicolon), *first.body[1:]]
                    if self._is_docstring(first.body[1])
                    else list(first.body[1:])
                )
                replacement = first.with_changes(body=remaining)
                return updated_node.with_changes(body=[replacement, *updated_node.body[1:]])
            next_is_string = (
                len(updated_node.body) > 1
                and isinstance(updated_node.body[1], cst.SimpleStatementLine)
                and self._is_docstring(updated_node.body[1].body[0])
            )
            if next_is_string:
                replacement = first.with_changes(body=[cst.Pass()])
                return updated_node.with_changes(body=[replacement, *updated_node.body[1:]])
            return updated_node.with_changes(body=updated_node.body[1:])

        def leave_FunctionDef(self, original_node: cst.FunctionDef, updated_node: cst.FunctionDef) -> cst.FunctionDef:
            return updated_node.with_changes(body=self._strip_suite(updated_node.body))

        def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef) -> cst.ClassDef:
            return updated_node.with_changes(body=self._strip_suite(updated_node.body))

    result: _CSTTypes = (_CommentRemover, _DocstringRemover)
    cache[strip_all] = result
    return result


def _libcst_strip(source: str, comments: bool, docstrings: bool, strip_all: bool = False) -> str:
    import libcst as cst

    comment_remover, docstring_remover = _cst_transformer_types(strip_all)
    parse_source(source)
    module = cst.parse_module(source)
    if docstrings:
        module = module.visit(docstring_remover())
    if comments:
        module = module.visit(comment_remover())
    result = module.code
    ast.parse(result)
    return result


class CSTStripper(ASTStripper):
    def strip(self, source: str) -> str:
        result = _libcst_strip(source, self.strip_comments, self.strip_docstrings, self.strip_all)
        return remove_blank_lines(result) if self.remove_blank_lines else result


@dataclass(frozen=True)
class FileOptions:
    comments: bool = False
    docstrings: bool = False
    backend: str = "libcst"
    blank_lines: bool = False
    strip_all: bool = False
    dryrun: bool = False
    backup: bool = False
    diff: bool = False


@dataclass(frozen=True)
class Task:
    path: Path
    options: FileOptions


class _WorkerState:
    def __init__(self) -> None:
        self.strippers: dict[tuple[str, bool, bool, bool, bool], ASTStripper] = {}


_STRIPPER_TYPES: dict[str, type[ASTStripper]] = {
    "tree-sitter": TSStripper,
    "libcst": CSTStripper,
    "ast": ASTStripper,
}


def get_worker_stripper(options: FileOptions) -> ASTStripper:
    thread = threading.current_thread()
    state = cast("_WorkerState | None", getattr(thread, "_strip_python_worker_state_v1", None))
    if state is None:
        state = _WorkerState()
        setattr(thread, "_strip_python_worker_state_v1", state)
    key = (options.backend, options.comments, options.docstrings, options.blank_lines, options.strip_all)
    cached = state.strippers.get(key)
    if cached is None:
        cached = _STRIPPER_TYPES[options.backend](
            options.comments, options.docstrings, options.blank_lines, options.strip_all
        )
        state.strippers[key] = cached
    return cached


def process_file(task: Task) -> StripFileResult:
    started = perf_counter()
    path, options = task.path, task.options
    try:
        stripper = get_worker_stripper(options)
        result = stripper.strip_file(path, dryrun=options.dryrun, backup=options.backup, show_diff=options.diff)
    except Exception as exc:
        result = StripFileResult(path, error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
    result.elapsed = perf_counter() - started
    return result


def check_interpreter_backend(backend: str) -> str:
    try:
        get_worker_stripper(FileOptions(backend=backend, comments=True)).strip("x = 1 # probe\n")
    except Exception as exc:
        return f"{type(exc).__name__}: {exc}"
    return ""


def result_to_wire(result: StripFileResult) -> dict[str, Any]:
    return {
        "path": str(result.path),
        "saved": result.saved,
        "modified": result.modified,
        "error": result.error,
        "elapsed": result.elapsed,
        "diff": result.diff,
        "backup": str(result.backup) if result.backup else None,
        "traceback": result.traceback,
        "skipped": result.skipped,
        "skip_reason": result.skip_reason,
    }


def result_from_wire(payload: bytes) -> StripFileResult:
    data = cast("dict[str, Any]", json.loads(payload))
    return StripFileResult(
        path=Path(data["path"]),
        saved=int(data["saved"]),
        modified=bool(data["modified"]),
        error=data["error"],
        elapsed=float(data["elapsed"]),
        diff=data["diff"],
        backup=Path(data["backup"]) if data["backup"] else None,
        traceback=data["traceback"],
        skipped=bool(data["skipped"]),
        skip_reason=data["skip_reason"],
    )


def process_file_wire(payload: bytes) -> bytes:
    data = cast("dict[str, Any]", json.loads(payload))
    result = process_file(Task(Path(data["path"]), FileOptions(**data["options"])))
    return json.dumps(result_to_wire(result), ensure_ascii=False).encode("utf-8")


SKIP_DIRS: frozenset[str] = frozenset({
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".ipynb_checkpoints",
    "build",
    "pip",
    "numpy",
    "scipy",
    "pandas",
    "setuptools",
    ".idea",
    ".vscode",
    "packaging",
    "lazy",
    "numba",
})


class WalkFiles(Protocol):
    def __call__(self, root: str | Path, /) -> list[Path]: ...


@dataclass(frozen=True)
class DiscoveryIssue:
    path: Path
    error: str


def record_discovery_error(path: Path, exc: Exception, issues: list[DiscoveryIssue] | None) -> None:
    error = f"{type(exc).__name__}: {exc}"
    if issues is not None:
        issues.append(DiscoveryIssue(path, error))
    logger.bind(traceback=traceback.format_exc(), file_path=str(path)).error("Discovery {}: {}", path.name, error)


def is_skip_dir(path: Path) -> bool:
    return path.name in SKIP_DIRS or path.name.endswith(".egg-info")


def is_python_file(path: Path, issues: list[DiscoveryIssue] | None = None) -> bool:
    if path.suffix.lower() == ".py":
        return True
    if path.suffix:
        return False
    try:
        with path.open("rb") as stream:
            first_line = stream.readline(4096).decode("utf-8", errors="replace")
    except OSError as exc:
        record_discovery_error(path, exc, issues)
        return False
    if not first_line.startswith("#!"):
        return False
    try:
        words = shlex.split(first_line[2:].strip())
    except ValueError:
        return False
    if not words:
        return False
    interpreter = Path(words[0]).name
    if interpreter == "env":
        candidates: list[str] = []
        try:
            for word in words[1:]:
                candidates.extend(shlex.split(word))
        except ValueError:
            return False
        interpreter = Path(next((word for word in candidates if not word.startswith("-") and "=" not in word), "")).name
    return re.fullmatch(r"(?:python|pypy)(?:[23](?:\.\d+)*)?", interpreter) is not None


def is_excluded(path: Path, root: Path, patterns: tuple[str, ...]) -> bool:
    if not patterns:
        return False
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        return True
    return any(
        fnmatch.fnmatchcase(path.name, pattern) or fnmatch.fnmatchcase(relative, pattern) for pattern in patterns
    )


def pathlib_walk_files(
    root_dir: Path,
    *,
    exclude_dirs: frozenset[str] = frozenset(),
    excludes: tuple[str, ...] = (),
    issues: list[DiscoveryIssue] | None = None,
) -> Iterator[Path]:
    pending = [root_dir]
    while pending:
        directory = pending.pop()
        try:
            for path in directory.iterdir():
                if is_excluded(path, root_dir, excludes):
                    continue
                try:
                    if sys.version_info >= (3, 14):
                        info = path.info
                        if info.is_symlink():
                            continue
                        is_directory = info.is_dir(follow_symlinks=False)
                        is_regular = not is_directory and info.is_file(follow_symlinks=False)
                    else:
                        mode = path.lstat().st_mode
                        is_directory = stat.S_ISDIR(mode)
                        is_regular = stat.S_ISREG(mode)
                    if is_directory:
                        if not is_skip_dir(path) and path.name not in exclude_dirs:
                            pending.append(path)
                    elif is_regular:
                        yield path
                except OSError as exc:
                    record_discovery_error(path, exc, issues)
        except OSError as exc:
            record_discovery_error(directory, exc, issues)


def _fastwalk_parent_ok(parent: Path, root: Path, exclude_dirs: frozenset[str], excludes: tuple[str, ...]) -> bool:
    current = parent
    while current != root and current != current.parent:
        if (
            is_skip_dir(current)
            or current.name in exclude_dirs
            or is_excluded(current, root, excludes)
            or current.is_symlink()
        ):
            return False
        current = current.parent
    return True


def iter_python_files(
    root_dir: Path,
    *,
    use_fastwalk: bool = False,
    exclude_dirs: frozenset[str] = frozenset(),
    excludes: tuple[str, ...] = (),
    issues: list[DiscoveryIssue] | None = None,
) -> Iterator[Path]:
    root_dir = Path(os.path.abspath(root_dir.expanduser()))
    mode = root_dir.lstat().st_mode
    if stat.S_ISLNK(mode):
        return
    if stat.S_ISREG(mode):
        if not is_excluded(root_dir, root_dir.parent, excludes) and is_python_file(root_dir, issues):
            yield root_dir
        return
    if not stat.S_ISDIR(mode):
        raise ValueError(f"Input is not a regular file or directory: {root_dir}")
    if use_fastwalk:
        walk_files = cast(WalkFiles, importlib.import_module("fastwalk").walk_files)
        safe_parents: dict[Path, bool] = {root_dir: True}
        for found in walk_files(root_dir):
            path = found if found.is_absolute() else root_dir / found
            try:
                relative = path.relative_to(root_dir)
                if ".." in relative.parts or is_excluded(path, root_dir, excludes):
                    continue
                parent_ok = safe_parents.get(path.parent)
                if parent_ok is None:
                    parent_ok = _fastwalk_parent_ok(path.parent, root_dir, exclude_dirs, excludes)
                    safe_parents[path.parent] = parent_ok
                if parent_ok and stat.S_ISREG(path.lstat().st_mode) and is_python_file(path, issues):
                    yield path
            except (OSError, ValueError) as exc:
                record_discovery_error(path, exc, issues)
    else:
        for path in pathlib_walk_files(root_dir, exclude_dirs=exclude_dirs, excludes=excludes, issues=issues):
            if is_python_file(path, issues):
                yield path


def iter_input_files(
    inputs: Iterable[Path],
    *,
    use_fastwalk: bool = False,
    exclude_dirs: frozenset[str] = frozenset(),
    excludes: tuple[str, ...] = (),
    issues: list[DiscoveryIssue] | None = None,
) -> Iterator[Path]:
    seen: set[Path] = set()
    for input_path in inputs:
        try:
            for path in iter_python_files(
                input_path,
                use_fastwalk=use_fastwalk,
                exclude_dirs=exclude_dirs,
                excludes=excludes,
                issues=issues,
            ):
                if path not in seen:
                    seen.add(path)
                    yield path
        except (OSError, ValueError) as exc:
            record_discovery_error(input_path, exc, issues)


METHODS = (
    "mp.map",
    "mp.imap",
    "mp.imap_unordered",
    "mp.map_async",
    "mp.starmap",
    "mp.starmap_async",
    "mp.apply",
    "mp.apply_async",
    "threadpool.map",
    "threadpool.imap",
    "threadpool.imap_unordered",
    "threadpool.map_async",
    "threadpool.starmap",
    "threadpool.starmap_async",
    "threadpool.apply",
    "threadpool.apply_async",
    "cf.process",
    "cf.thread",
    "cf.interpreter",
    "joblib",
)


def run_tasks(tasks: list[Task], method: str, workers: int, chunksize: int | None = None) -> Iterator[StripFileResult]:
    if not tasks:
        return
    workers = min(workers, len(tasks))
    if workers == 1:
        for task in tasks:
            yield process_file(task)
        return
    chunk = chunksize or max(1, min(64, -(-len(tasks) // (4 * workers))))
    if method.startswith(("mp.", "threadpool.")):
        pool_type: Callable[[int], Any] = mp.Pool if method.startswith("mp.") else ThreadPool
        operation = method.split(".", 1)[1]
        with pool_type(workers) as pool:
            if operation == "apply":
                for task in tasks:
                    yield pool.apply(process_file, (task,))
            elif operation == "apply_async":
                pending = [pool.apply_async(process_file, (task,)) for task in tasks]
                for async_result in pending:
                    yield async_result.get()
            elif operation in ("starmap", "starmap_async"):
                mapped = getattr(pool, operation)(process_file, [(task,) for task in tasks], chunksize=chunk)
                yield from mapped.get() if operation.endswith("_async") else mapped
            elif operation == "map_async":
                yield from pool.map_async(process_file, tasks, chunksize=chunk).get()
            else:
                yield from getattr(pool, operation)(process_file, tasks, chunksize=chunk)
    elif method.startswith("cf."):
        name = {
            "cf.process": "ProcessPoolExecutor",
            "cf.thread": "ThreadPoolExecutor",
            "cf.interpreter": "InterpreterPoolExecutor",
        }[method]
        executor_type = getattr(cf, name, None)
        if executor_type is None:
            raise RuntimeError(f"{name} requires Python 3.14+")
        with executor_type(max_workers=workers) as executor:
            if method == "cf.interpreter":
                for backend in {task.options.backend for task in tasks}:
                    problem = executor.submit(check_interpreter_backend, backend).result()
                    if problem:
                        raise RuntimeError(
                            f"{backend} cannot run in InterpreterPoolExecutor: {problem}. "
                            "Use -b ast or --pool-method cf.process instead."
                        )
                payloads = (
                    json.dumps({"path": str(task.path), "options": asdict(task.options)}).encode("utf-8")
                    for task in tasks
                )
                for payload in executor.map(process_file_wire, payloads):
                    yield result_from_wire(payload)
            else:
                yield from executor.map(process_file, tasks, chunksize=chunk)
    else:
        try:
            from joblib import Parallel, delayed
        except ImportError as exc:
            raise RuntimeError("Install joblib: python -m pip install joblib") from exc
        yield from Parallel(n_jobs=workers, backend="loky", return_as="generator")(
            delayed(process_file)(task) for task in tasks
        )


def write_json_report(path: Path, report: dict[str, object]) -> None:
    data = (json.dumps(report, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def colorize_diff(diff: str, color: bool) -> str:
    if not color:
        return diff
    output: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith(("---", "+++")):
            code = "1"
        elif line.startswith("+"):
            code = "32"
        elif line.startswith("-"):
            code = "31"
        elif line.startswith("@@"):
            code = "36"
        else:
            code = ""
        output.append(f"\033[{code}m{line}\033[0m" if code else line)
    return "".join(output)


def make_rich_ui(color_mode: str) -> tuple[Progress, Callable[[str], None]]:
    from rich.console import Console
    from rich.progress import BarColumn
    from rich.progress import Progress as RichProgress
    from rich.progress import TaskProgressColumn, TextColumn, TimeElapsedColumn
    from rich.text import Text

    force_terminal = True if color_mode == "always" else False if color_mode == "never" else None
    console = Console(stderr=True, force_terminal=force_terminal, no_color=color_mode == "never")
    progress = RichProgress(
        TextColumn("{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TextColumn("{task.completed:.0f}/{task.total:.0f}"),
        TimeElapsedColumn(),
        console=console,
    )

    def emit(message: str) -> None:
        console.print(Text.from_ansi(message.rstrip("\n")))

    return progress, emit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Strip comments and docstrings from Python files safely.")
    parser.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        metavar="PATH",
        help="Files/directories to process (default: current directory recursively)",
    )
    parser.add_argument("--fastwalk", action="store_true", help="Use the optional Rust fastwalk walker")
    parser.add_argument("-c", action="store_true", help="Strip comments; keep directives and headers")
    parser.add_argument("-d", action="store_true", help="Strip function/class docstrings; keep module docstrings")
    parser.add_argument(
        "-a",
        "--all",
        action="store_true",
        help="Strip ALL comments and docstrings, including module docs, headers and directives",
    )
    parser.add_argument(
        "-b",
        "--backend",
        choices=("tree-sitter", "libcst", "ast"),
        default="libcst",
        help="Stripping backend (default: tree-sitter)",
    )
    parser.add_argument("--remove-blank-lines", action="store_true", help="Remove blank lines outside strings")
    parser.add_argument(
        "--pool-method",
        choices=METHODS,
        default="cf.process",
        help="Execution backend (default: cf.process)",
    )
    parser.add_argument("-j", "--workers", type=int, default=4, help="Worker count (default: 4)")
    parser.add_argument("--chunksize", type=int, help="Pool batch size (default: adaptive, up to 64)")
    parser.add_argument(
        "--exclude-dir",
        action="append",
        default=[],
        metavar="NAME",
        help="Additional directory basename to prune (repeatable)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="Exclude a basename or path relative to each input directory (repeatable)",
    )
    parser.add_argument("--report-json", type=Path, metavar="PATH", help="Write an atomic JSON run report")
    parser.add_argument(
        "--error-log",
        type=Path,
        default=Path("strip_python.errors.log"),
        help="Error log with tracebacks (default: strip_python.errors.log; created only on error)",
    )
    parser.add_argument("--diff", action="store_true", help="Show unified diffs")
    parser.add_argument("--backup", action="store_true", help="Save originals as NAME.bak; never overwrite backups")
    parser.add_argument("--dryrun", "--dry-run", action="store_true", help="Preview changes without writing")
    parser.add_argument("--check", action="store_true", help="Dry run; exit with status 1 if any file would change")
    parser.add_argument("-t", "--time", action="store_true", help="Show processing time for every file")
    parser.add_argument("--rich", action="store_true", help="Rich progress instead of per-file byte reports")
    parser.add_argument("--color", choices=("auto", "always", "never"), default="auto", help="Color output")
    parser.add_argument("--log-file", type=Path, help="Also append DEBUG-level Loguru logs to this file")
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="WARNING",
        help="Console log level (default: WARNING)",
    )
    return parser


def configure_logging(args: argparse.Namespace, emit: Callable[[str], None] | None) -> bool:
    logger.remove()
    logger.configure(extra={"traceback": "", "file_path": ""})
    log_format = "<level>{level: <7}</level> | {message}"
    if emit is not None:
        logger.add(emit, level=args.log_level, format=log_format, colorize=False)
    else:
        logger.add(
            sys.stderr,
            level=args.log_level,
            format=log_format,
            colorize=args.color == "always"
            or (args.color == "auto" and sys.stderr.isatty() and "NO_COLOR" not in os.environ),
        )
    file_format = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level} | {extra[file_path]} | {message}\n{extra[traceback]}"
    try:
        logger.add(
            args.error_log,
            level="ERROR",
            encoding="utf-8",
            delay=True,
            rotation="5 MB",
            retention=3,
            diagnose=False,
            format=file_format,
        )
        if args.log_file is not None:
            logger.add(args.log_file, level="DEBUG", encoding="utf-8", diagnose=False, format=file_format)
    except (OSError, ValueError) as exc:
        logger.exception("Cannot configure log output: {}", exc)
        return False
    return True


def main() -> int:
    parser = build_parser()
    args = parser.parse_intermixed_args()
    if not (args.c or args.d or args.all or args.remove_blank_lines):
        args.c = args.d = True
    if args.check:
        args.dryrun = True
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    if args.chunksize is not None and args.chunksize < 1:
        parser.error("--chunksize must be at least 1")
    if any(not name or "/" in name or "\\" in name for name in args.exclude_dir):
        parser.error("--exclude-dir accepts a directory basename, not a path; use --exclude for patterns")
    if args.pool_method == "cf.interpreter" and not hasattr(cf, "InterpreterPoolExecutor"):
        parser.error("cf.interpreter requires Python 3.14+")
    dependencies: dict[str, str] = {}
    if args.backend == "tree-sitter":
        dependencies.update(tree_sitter="tree-sitter", tree_sitter_python="tree-sitter-python")
    elif args.backend == "libcst":
        dependencies["libcst"] = "libcst"
    if args.pool_method == "joblib":
        dependencies["joblib"] = "joblib"
    if args.rich:
        dependencies["rich"] = "rich"
    missing = [package for module, package in dependencies.items() if importlib.util.find_spec(module) is None]
    if missing:
        parser.error("install dependencies: python -m pip install " + " ".join(missing))
    color = args.color == "always" or (args.color == "auto" and sys.stdout.isatty() and "NO_COLOR" not in os.environ)
    progress: Progress | None = None
    emit: Callable[[str], None] | None = None
    if args.rich:
        progress, emit = make_rich_ui(args.color)
    if not configure_logging(args, emit):
        return 1
    started = perf_counter()
    discovery_issues: list[DiscoveryIssue] = []
    try:
        inputs: list[Path] = args.inputs or [Path.cwd()]
        for input_path in inputs:
            expanded = input_path.expanduser()
            if not os.path.lexists(expanded):
                raise FileNotFoundError(f"Input does not exist: {input_path}")
        if args.fastwalk and not callable(getattr(importlib.import_module("fastwalk"), "walk_files", None)):
            raise AttributeError("fastwalk.walk_files is missing or is not callable")
        outputs = {
            Path(os.path.abspath(output))
            for output in (args.error_log, args.log_file, args.report_json)
            if output is not None
        }
        paths = sorted(
            path
            for path in iter_input_files(
                inputs,
                use_fastwalk=args.fastwalk,
                exclude_dirs=frozenset(args.exclude_dir),
                excludes=tuple(args.exclude),
                issues=discovery_issues,
            )
            if path not in outputs
        )
    except (ImportError, AttributeError) as exc:
        logger.exception("Cannot load fastwalk.walk_files API: {}. Install your custom fastwalk package.", exc)
        return 1
    except Exception as exc:
        logger.exception("File discovery failed: {}", exc)
        return 1
    options = FileOptions(
        comments=args.c,
        docstrings=args.d,
        backend=args.backend,
        blank_lines=args.remove_blank_lines,
        strip_all=args.all,
        dryrun=args.dryrun,
        backup=args.backup,
        diff=args.diff,
    )
    tasks = [Task(path, options) for path in paths]
    logger.info(
        "Found {} Python files; backend={}, pool={}, workers={}, dryrun={}",
        len(paths),
        args.backend,
        args.pool_method,
        args.workers,
        args.dryrun,
    )
    if args.all:
        logger.warning("--all removes executable shebangs, encoding comments and tool directives too.")

    def green(number: int) -> str:
        text = f"{number:,}"
        return f"\033[32m{text}\033[0m" if color else text

    total = changed = processed = skipped = 0
    errors = len(discovery_issues)
    report_rows: list[dict[str, object]] = []
    width = max((len(path.name) for path in paths), default=0)
    progress_id: TaskID | None = None
    context = progress if progress is not None else nullcontext()
    with context:
        if progress is not None:
            progress_id = progress.add_task("Processing", total=len(paths))
        try:
            for result in run_tasks(tasks, args.pool_method, args.workers, args.chunksize):
                processed += 1
                if args.report_json is not None:
                    report_rows.append({
                        "path": str(result.path),
                        "modified": result.modified,
                        "bytes_reduced": result.saved,
                        "elapsed_seconds": result.elapsed,
                        "error": result.error,
                        "skipped": result.skipped,
                        "skip_reason": result.skip_reason or None,
                        "backup": str(result.backup) if result.backup else None,
                    })
                if result.error:
                    errors += 1
                    logger.bind(traceback=result.traceback, file_path=str(result.path)).error(
                        "{}: {}", result.path.name, result.error
                    )
                elif result.skipped:
                    skipped += 1
                    logger.warning("Skipped {}: {}", result.path, result.skip_reason)
                else:
                    total += result.saved
                    changed += result.modified
                    logger.debug(
                        "{}: modified={}, bytes_reduced={}, seconds={:.6f}, backup={}",
                        result.path.name,
                        result.modified,
                        result.saved,
                        result.elapsed,
                        result.backup.name if result.backup else None,
                    )
                    if result.diff:
                        diff = colorize_diff(result.diff, color)
                        if emit is not None:
                            emit(diff)
                        else:
                            print(diff, end="", flush=True)
                    if progress is None:
                        timing = f"  {result.elapsed:.4f}s" if args.time else ""
                        print(f"{result.path.name:<{width}}  {green(result.saved)}{timing}", flush=True)
                if progress is not None and progress_id is not None:
                    progress.update(progress_id, advance=1, description=result.path.name)
                    if args.time and emit is not None:
                        emit(f"{result.path.name}  {result.elapsed:.4f}s")
                elif args.time and (result.error or result.skipped):
                    print(f"{result.path.name:<{width}}  {result.elapsed:.4f}s", flush=True)
        except Exception as exc:
            errors += 1
            if isinstance(exc, cf.process.BrokenProcessPool):
                logger.error("A worker process died (crash or out-of-memory). Try -j 1 or -b ast.")
            logger.exception("Execution backend failed: {}", exc)
    if args.report_json is not None:
        try:
            write_json_report(
                args.report_json,
                {
                    "version": 2,
                    "dryrun": options.dryrun,
                    "backend": options.backend,
                    "pool_method": args.pool_method,
                    "walker": "fastwalk" if args.fastwalk else "pathlib",
                    "discovered": len(paths),
                    "processed": processed,
                    "complete": processed == len(paths),
                    "unreported_files": len(paths) - processed,
                    "changed": changed,
                    "skipped": skipped,
                    "errors": errors,
                    "source_bytes_reduced": total,
                    "elapsed_seconds": perf_counter() - started,
                    "discovery_errors": [{"path": str(issue.path), "error": issue.error} for issue in discovery_issues],
                    "files": report_rows,
                },
            )
        except Exception as exc:
            errors += 1
            logger.exception("Cannot write JSON report {}: {}", args.report_json, exc)
    if args.dryrun:
        label = "Potential source bytes reduced"
    elif args.backup:
        label = "Total source bytes reduced"
    else:
        label = "Total space freed"
    print(f"\n{label}: {green(total)} bytes")
    if args.dryrun:
        print(f"Dry run: {changed} file(s) would change; no source files or backups written.")
    if skipped:
        print(f"Skipped (syntax/encoding errors): {skipped}")
    if args.backup and not args.dryrun:
        print("Backup copies consume additional space; byte totals count source files only.")
    if errors:
        print(f"Errors: {errors}; details logged to {args.error_log.name}", file=sys.stderr)
    if args.time:
        print(f"Total elapsed: {perf_counter() - started:.4f}s")
    logger.info(
        "Finished: {} changed, {} skipped, {} errors, {} source bytes reduced, {:.4f}s",
        changed,
        skipped,
        errors,
        total,
        perf_counter() - started,
    )
    if errors:
        return 1
    return 1 if args.check and changed else 0


def entrypoint() -> int:
    try:
        return main()
    except KeyboardInterrupt:
        logger.error("Interrupted; already committed source updates are not rolled back.")
        return 130
    except Exception:
        logger.exception("Fatal unhandled error")
        return 1


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(entrypoint())
'''

_SOURCES['clean'] = r'''#!/data/data/com.termux/files/usr/bin/env python

import argparse
import ast
import difflib
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import textwrap
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from multiprocessing import Pool
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
try:
    import zstandard as zstd

    _HAS_ZSTD = True
except ImportError:
    _HAS_ZSTD = False
_USE_COLOR: bool = True


def _c(code: str, text: str) -> str:
    return text if not _USE_COLOR else f"\x1b[{code}m{text}\x1b[0m"


def bold(s: str) -> str:
    return _c("1", s)


def cyan(s: str) -> str:
    return _c("36", s)


def yellow(s: str) -> str:
    return _c("33", s)


def red(s: str) -> str:
    return _c("31", s)


def green(s: str) -> str:
    return _c("32", s)


def dim(s: str) -> str:
    return _c("2", s)


def _safe_read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _display_path(p: str) -> str:
    if "::" in p:
        return p
    try:
        return str(Path(p).relative_to(Path.cwd()))
    except ValueError:
        return p


def _iter_py_files(root: Path, skip_dirs: set[str]) -> list[Path]:
    return [p for p in root.rglob("*.py") if p.is_file() and not any(part in skip_dirs for part in p.parts)]


_SKIP_DIRS: set[str] = {
    ".git",
    "__pycache__",
    "venv",
    ".venv",
    "output",
    "node_modules",
}


@dataclass
class UnusedImport:
    lineno: int
    col_offset: int
    original_stmt: str
    unused_names: list[str]


@dataclass
class _ImportReport:
    path: str
    unused: list[UnusedImport] = field(default_factory=list)
    error: str | None = None


def _collect_type_checking_lines(tree: ast.Module) -> set[int]:
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        is_tc = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )
        if is_tc:
            for sub in ast.walk(node):
                if hasattr(sub, "lineno"):
                    lines.add(sub.lineno)
    return lines


def _collect_all_exports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__all__":
                if isinstance(node.value, (ast.List, ast.Tuple)):
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            names.add(elt.value)
    return names


def _collect_used_names(tree: ast.Module) -> set[str]:
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            head = node
            while isinstance(head, ast.Attribute):
                head = head.value
            if isinstance(head, ast.Name):
                used.add(head.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for tok in re.findall(r"\b([A-Za-z_]\w*)\b", node.value):
                used.add(tok)
    return used


def _parse_import_names(node: ast.Import | ast.ImportFrom) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for alias in node.names:
        full = alias.name
        bound = alias.asname or full.split(".")[0]
        pairs.append((full, bound))
    return pairs


def _analyse_imports(
    source: str,
    path: str,
    *,
    is_init: bool = False,
    ignore_init: bool = False,
) -> _ImportReport:
    report = _ImportReport(path=path)
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        report.error = f"SyntaxError: {exc}"
        return report
    if ignore_init and is_init:
        return report
    tc_lines = _collect_type_checking_lines(tree)
    all_exports = _collect_all_exports(tree)
    used = _collect_used_names(tree)
    docstring = ""
    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        docstring = tree.body[0].value.value
    src_lines = source.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if node.lineno in tc_lines:
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if any(alias.name == "*" for alias in node.names):
            continue
        if ignore_init and isinstance(node, ast.ImportFrom) and (node.level or 0) > 0:
            continue
        unused_names: list[str] = []
        for _full, bound in _parse_import_names(node):
            if bound in used or bound in all_exports or bound in docstring:
                continue
            unused_names.append(bound)
        if not unused_names:
            continue
        start = node.lineno - 1
        end = node.end_lineno or node.lineno
        stmt_src = "\n".join(src_lines[start:end])
        report.unused.append(
            UnusedImport(
                lineno=node.lineno,
                col_offset=node.col_offset,
                original_stmt=stmt_src,
                unused_names=unused_names,
            )
        )
    return report


def _bound_name(segment: str) -> str:
    m = re.match(r"^\s*[\w.]+\s+as\s+(\w+)\s*$", segment)
    if m:
        return m.group(1)
    return segment.strip().split(".")[0].strip()


def _rewrite_simple_import(stmt: str, unused: set[str]) -> str | None:
    m = re.match(r"^(\s*import\s+)(.+)$", stmt)
    if m:
        head, body = m.group(1), m.group(2)
        kept = [seg.strip() for seg in body.split(",") if _bound_name(seg.strip()) not in unused]
        if not kept:
            return None
        return head + ", ".join(kept)
    m = re.match(r"^(\s*from\s+[\w.]+\s+import\s+)(.+)$", stmt)
    if m:
        head, body = m.group(1), m.group(2)
        body = re.sub(r"\s*#.*$", "", body).rstrip(" \\")
        kept = [seg.strip() for seg in body.split(",") if seg.strip() and _bound_name(seg.strip()) not in unused]
        if not kept:
            return None
        return head + ", ".join(kept)
    return stmt


def _rewrite_multiline_import(stmt: str, unused: set[str]) -> str | None:
    m = re.match(r"^(\s*from\s+[\w.]+\s+import\s*$)(.*?)($.*)", stmt, re.DOTALL)
    if not m:
        return None
    head, body, tail = m.group(1), m.group(2), m.group(3)
    kept: list[str] = []
    for seg in re.split(r",\s*", body):
        clean = re.sub(r"#.*", "", seg).strip()
        if not clean:
            continue
        if _bound_name(clean) not in unused:
            kept.append(clean)
    if not kept:
        return None
    if len(kept) == 1 and "\n" not in stmt:
        return f"{head[:-1]}{kept[0]}{tail[1:]}"
    joined = ",\n    ".join(kept)
    return f"{head}\n    {joined},\n{tail}"


def _rewrite_source(source: str, unused: list[UnusedImport]) -> str:
    by_line: dict[int, set[str]] = {}
    end_of: dict[int, int] = {}
    for item in unused:
        by_line.setdefault(item.lineno, set()).update(item.unused_names)
        end_of[item.lineno] = item.lineno + item.original_stmt.count("\n")
    lines = source.splitlines(keepends=True)
    out: list[str] = []
    skip_until = -1
    i = 0
    while i < len(lines):
        lineno = i + 1
        if lineno <= skip_until:
            i += 1
            continue
        if lineno not in by_line:
            out.append(lines[i])
            i += 1
            continue
        names = by_line[lineno]
        end = end_of[lineno]
        stmt = "".join(lines[i:end])
        if "(" in stmt and "\n" in stmt:
            new = _rewrite_multiline_import(stmt, names)
        else:
            plain = stmt.strip("\n")
            new = _rewrite_simple_import(plain, names)
            if new is not None:
                new += "\n" if stmt.endswith("\n") else ""
        if new is not None:
            out.append(new)
        skip_until = end
        i += 1
    return "".join(out)


def _autoflake_one(path: Path, *, autofix: bool, diff: bool) -> None:
    if not path.exists():
        print(f"Error: The file `{path}` does not exist.")
        return
    cmd = [
        "autoflake",
        "--remove-all-unused-imports",
        "--ignore-init-module-imports",
        str(path),
    ]
    cmd.append("--in-place" if autofix else "--check")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        print("Error: `autoflake` is not installed. Run `pip install autoflake`.")
        return
    if proc.returncode == 0:
        return
    if proc.returncode == 1:
        if autofix:
            print(f"Successfully removed unused imports from `{path}`.")
        elif diff:
            original = path.read_text()
            fixed = subprocess.run(
                [
                    "autoflake",
                    "--remove-all-unused-imports",
                    "--ignore-init-module-imports",
                    "-",
                ],
                input=original,
                capture_output=True,
                text=True,
            )
            if fixed.returncode == 0 and fixed.stdout != original:
                udiff = difflib.unified_diff(
                    original.splitlines(keepends=True),
                    fixed.stdout.splitlines(keepends=True),
                    fromfile=f"a/{path}",
                    tofile=f"b/{path}",
                )
                print("".join(udiff), end="")
        else:
            for line in ((proc.stdout or "") + (proc.stderr or "")).splitlines():
                if line.strip():
                    print(f"{path}: {line}")


def _scan_zip(path: Path) -> Iterator[tuple[str, str]]:
    try:
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if not name.endswith(".py"):
                    continue
                key = f"{path.name}::{name}"
                try:
                    yield key, zf.read(name).decode("utf-8", errors="replace")
                except Exception as exc:
                    yield key, f"__ERROR__:{exc}"
    except zipfile.BadZipFile as exc:
        yield f"{path.name}::?", f"__ERROR__:Bad zip: {exc}"


def _scan_tar(path: Path) -> Iterator[tuple[str, str]]:
    def _inner(tf: tarfile.TarFile) -> Iterator[tuple[str, str]]:
        for member in tf.getmembers():
            if not member.name.endswith(".py"):
                continue
            key = f"{path.name}::{member.name}"
            try:
                fobj = tf.extractfile(member)
                if fobj is None:
                    continue
                yield key, fobj.read().decode("utf-8", errors="replace")
            except Exception as exc:
                yield key, f"__ERROR__:{exc}"

    try:
        if _HAS_ZSTD:
            decomp = zstd.ZstdDecompressor()
            with open(path, "rb") as fh:
                stream = decomp.stream_reader(fh)
                with tarfile.open(fileobj=io.BytesIO(stream.read())) as tf:
                    yield from _inner(tf)
        else:
            with tarfile.open(path) as tf:
                yield from _inner(tf)
    except Exception as exc:
        yield f"{path.name}::?", f"__ERROR__:Archive error: {exc}"


_WORKER_IGNORE_INIT: bool = False


def _init_worker_imports(ignore_init: bool) -> None:
    global _WORKER_IGNORE_INIT
    _WORKER_IGNORE_INIT = ignore_init


def _worker_file(p: str) -> _ImportReport:
    path = Path(p)
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _ImportReport(path=p, error=f"OSError: {exc}")
    is_init = path.name == "__init__.py"
    return _analyse_imports(src, p, is_init=is_init, ignore_init=_WORKER_IGNORE_INIT)


def _worker_archive(item: tuple[str, str]) -> _ImportReport:
    key, src = item
    if src.startswith("__ERROR__:"):
        return _ImportReport(path=key, error=src[len("__ERROR__:") :])
    is_init = key.endswith(("/__init__.py", "\\__init__.py"))
    return _analyse_imports(src, key, is_init=is_init, ignore_init=_WORKER_IGNORE_INIT)


def _collect_import_targets(
    paths: Iterable[Path], excludes: list[re.Pattern[str]]
) -> tuple[list[str], list[tuple[str, str]]]:
    def _is_excluded(p: Path) -> bool:
        s = str(p)
        return any(pat.search(s) for pat in excludes)

    py_files: list[str] = []
    archive_members: list[tuple[str, str]] = []
    for p in paths:
        if not p.exists():
            print(red(f"warning: path does not exist: {p}"), file=sys.stderr)
            continue
        if p.is_file():
            if p.suffix == ".py":
                if not _is_excluded(p):
                    py_files.append(str(p))
            elif p.suffix == ".whl":
                if not _is_excluded(p):
                    archive_members.extend(_scan_zip(p))
            elif p.name.endswith(".tar.zst"):
                if not _is_excluded(p):
                    archive_members.extend(_scan_tar(p))
            else:
                print(yellow(f"warning: skipping unrecognised file: {p}"), file=sys.stderr)
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if _is_excluded(f) or not f.is_file():
                    continue
                if f.suffix == ".py":
                    py_files.append(str(f))
                elif f.suffix == ".whl":
                    archive_members.extend(_scan_zip(f))
                elif f.name.endswith(".tar.zst"):
                    archive_members.extend(_scan_tar(f))
    return py_files, archive_members


def _print_report(rep: _ImportReport, *, verbose: bool) -> int:
    disp = _display_path(rep.path)
    if rep.error:
        print(f"  {red('error')} {bold(disp)}: {red(rep.error)}")
        return 0
    if not rep.unused:
        if verbose:
            print(f"  {green('✓')} {dim(disp)}")
        return 0
    for item in rep.unused:
        loc = cyan(f"line {item.lineno:>4}")
        first = yellow(item.original_stmt.splitlines()[0])
        print(f"  {bold(disp)}  -->  {loc}  {first}")
        print(f"{'':>50}  [unused: {red(', '.join(item.unused_names))}]")
    return len(rep.unused)


def _cmd_imports(args: argparse.Namespace) -> int:
    if args.backend == "autoflake":
        return _cmd_imports_autoflake(args)
    paths = [Path(p) for p in (args.paths or ["."])]
    excludes: list[re.Pattern[str]] = []
    for pat in args.exclude:
        try:
            excludes.append(re.compile(pat))
        except re.error as exc:
            print(red(f"error: invalid --exclude pattern {pat!r}: {exc}"), file=sys.stderr)
            return 2
    print(bold(f"\nScanning {len(paths)} path(s) with {args.workers} worker(s) …\n"))
    py_files, archive_members = _collect_import_targets(paths, excludes)
    n_py, n_arc = len(py_files), len(archive_members)
    print(f"  {cyan(str(n_py))} .py file(s), {cyan(str(n_arc))} archive member(s) queued.\n")
    if n_py + n_arc == 0:
        print(yellow("No files to analyse."))
        return 0
    reports: list[_ImportReport] = []
    with Pool(
        processes=args.workers,
        initializer=_init_worker_imports,
        initargs=(args.ignore_init,),
    ) as pool:
        for i, rep in enumerate(pool.imap_unordered(_worker_file, py_files), 1):
            reports.append(rep)
            if args.verbose:
                print(dim(f"  [{i}/{n_py}] analysed {_display_path(rep.path)}"), end="\r")
        if args.verbose and n_py:
            print()
        for rep in pool.imap_unordered(_worker_archive, archive_members):
            reports.append(rep)
    reports.sort(key=lambda r: r.path)
    total_unused = 0
    touched: set[str] = set()
    fixed_files = 0
    for rep in reports:
        n = _print_report(rep, verbose=args.verbose)
        total_unused += n
        if n:
            touched.add(rep.path)
        if (args.autofix or args.dry_run) and n and not rep.error:
            if "::" in rep.path:
                if args.verbose:
                    print(dim(f"  [skip fix — archive member: {rep.path}]"))
                continue
            p = Path(rep.path)
            try:
                src = p.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                print(f"  {red('SKIP autofix')} {bold(rep.path)} — {exc}")
                continue
            new_src = _rewrite_source(src, rep.unused)
            try:
                ast.parse(new_src, filename=rep.path)
            except SyntaxError as exc:
                print(f"  {red('SKIP autofix')} {bold(rep.path)} — result failed to parse: {exc}")
                continue
            if args.dry_run:
                print(f"  {cyan('would fix')} {bold(rep.path)}")
            else:
                st = p.stat()
                p.write_text(new_src, encoding="utf-8")
                os.chmod(p, st.st_mode)
                print(f"  {green('fixed')} {bold(rep.path)}")
                fixed_files += 1
    print()
    if total_unused == 0:
        print(green("✓ No unused imports found."))
    else:
        print(bold(f"Found {red(str(total_unused))} unused import(s) across {red(str(len(touched)))} file(s)."))
        if args.autofix and not args.dry_run:
            print(green(f"Fixed {fixed_files} file(s)."))
    return 0 if total_unused == 0 else 1


def _cmd_imports_autoflake(args: argparse.Namespace) -> int:
    paths = [Path(p) for p in (args.paths or ["."])]
    files: list[Path] = []
    for p in paths:
        if p.is_file() and p.suffix == ".py":
            files.append(p)
        elif p.is_dir():
            files.extend(sorted(p.glob("*.py")))
    if not files:
        print(yellow("No .py files found."))
        return 0
    for f in files:
        _autoflake_one(f, autofix=args.autofix, diff=args.diff)
    return 0


@dataclass
class DefItem:
    name: str
    kind: str
    file: Path
    lineno: int
    end_lineno: int


@dataclass
class _DefScanResult:
    file: Path
    defs: list[DefItem] = field(default_factory=list)
    used_names: set[str] = field(default_factory=set)
    source: str = ""
    error: str | None = None


_NEVER_UNUSED = {"__all__", "__version__", "__author__"}


def _is_const_name(name: str) -> bool:
    return name.isupper() and not name.startswith("__")


class _DefCollector(ast.NodeVisitor):
    def __init__(self, file: Path) -> None:
        self.file = file
        self.defs: list[DefItem] = []

    def _add(self, name: str, kind: str, node: ast.AST) -> None:
        self.defs.append(
            DefItem(
                name=name,
                kind=kind,
                file=self.file,
                lineno=node.lineno,
                end_lineno=getattr(node, "end_lineno", node.lineno),
            )
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._add(node.name, "func", node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._add(node.name, "class", node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name) and _is_const_name(target.id):
                self._add(target.id, "const", node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and _is_const_name(node.target.id):
            self._add(node.target.id, "const", node)
        self.generic_visit(node)


class _UsedNameCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.used: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        self.used.add(node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.used.add(node.attr)
        self.generic_visit(node)


def _scan_one_defs(path: Path) -> _DefScanResult:
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src, filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError) as exc:
        return _DefScanResult(file=path, error=str(exc))
    collector = _DefCollector(path)
    collector.visit(tree)
    user = _UsedNameCollector()
    user.visit(tree)
    return _DefScanResult(file=path, defs=collector.defs, used_names=user.used, source=src)


def _filter_unused_global(scans: list[_DefScanResult], kind_filter: str) -> list[DefItem]:
    all_defs: list[DefItem] = []
    all_used: set[str] = set()
    for s in scans:
        if s.error:
            continue
        all_defs.extend(s.defs)
        all_used |= s.used_names
    result: list[DefItem] = []
    for d in all_defs:
        if d.name in _NEVER_UNUSED:
            continue
        if d.name.startswith("__") and d.name.endswith("__"):
            continue
        if d.name == "main":
            continue
        if kind_filter not in ("all", d.kind):
            continue
        if d.name not in all_used:
            result.append(d)
    return result


def _filter_unused_per_file(scan: _DefScanResult, kind_filter: str) -> list[DefItem]:
    if scan.error:
        return []
    result: list[DefItem] = []
    for d in scan.defs:
        if d.name.startswith("_"):
            continue
        if kind_filter not in ("all", d.kind):
            continue
        if d.name not in scan.used_names:
            result.append(d)
    return result


def _remove_defs_from_source(source: str, names: set[str], only_top_level: bool) -> str:
    tree = ast.parse(source)
    if only_top_level:
        new_body = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names:
                continue
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in node.targets):
                continue
            new_body.append(node)
        tree.body = new_body
        return ast.unparse(tree)

    class _Remover(ast.NodeTransformer):
        def _maybe_drop(self, body: list[ast.stmt]) -> list[ast.stmt]:
            out: list[ast.stmt] = []
            for n in body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in names:
                    continue
                out.append(self.visit(n))
            return out

        def visit_Module(self, node: ast.Module) -> ast.Module:
            node.body = self._maybe_drop(node.body)
            return node

        def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
            node.body = self._maybe_drop(node.body)
            return node

    return ast.unparse(_Remover().visit(tree))


_EXTRACT_DIRS = {"func": "func", "class": "classes", "const": "const"}


def _extract_one(item: DefItem, base: Path) -> str:
    target = base / _EXTRACT_DIRS[item.kind]
    target.mkdir(parents=True, exist_ok=True)
    src_lines = item.file.read_text(encoding="utf-8", errors="replace").splitlines()
    snippet = "\n".join(src_lines[item.lineno - 1 : item.end_lineno])
    stem = item.file.stem.replace(".", "_")
    out = target / f"{stem}__{item.name}.py"
    header = (
        f"# Extracted: {item.kind} '{item.name}'\n# Source: {item.file}\n# Lines: {item.lineno}-{item.end_lineno}\n\n"
    )
    out.write_text(header + snippet + "\n", encoding="utf-8")
    return str(out)


def _cmd_defs(args: argparse.Namespace) -> int:
    root = Path(args.dir).resolve()
    files = _iter_py_files(root, _SKIP_DIRS)
    if not files:
        print(yellow("No .py files found."))
        return 0
    print(bold(f"Scanning {len(files)} file(s) with {args.workers} worker(s)..."))
    with Pool(processes=args.workers) as pool:
        scans = list(pool.imap_unordered(_scan_one_defs, files))
    for s in scans:
        if s.error:
            print(yellow(f"[WARN] Failed to parse {s.file}: {s.error}"))
    if args.scope == "global":
        unused_items = _filter_unused_global(scans, args.kind)
    else:
        unused_items = []
        for s in scans:
            unused_items.extend(_filter_unused_per_file(s, args.kind))
    if not unused_items:
        print(green("\nNo unused functions/classes/constants found."))
        return 0
    unused_items.sort(key=lambda d: (str(d.file), d.lineno))
    print(bold(f"\nFound {len(unused_items)} unused object(s):\n"))
    for d in unused_items:
        try:
            rel = d.file.relative_to(root)
        except ValueError:
            rel = d.file
        print(f"  [{d.kind:5}] {d.name:30} {rel}:{d.lineno}")
    if args.extract:
        out_base = Path(args.extract_dir).resolve() if args.extract_dir else root / "output"
        print(bold(f"\nExtracting {len(unused_items)} object(s) into {out_base} ..."))
        with Pool(processes=args.workers) as pool:
            written = list(pool.starmap(_extract_one, [(d, out_base) for d in unused_items]))
        for p in written:
            print(f"  wrote {p}")
        print(green("\nExtraction complete."))
    if args.remove:
        by_file: dict[Path, set[str]] = defaultdict(set)
        for d in unused_items:
            by_file[d.file].add(d.name)
        removed_count = 0
        for path, names in by_file.items():
            src = path.read_text(encoding="utf-8")
            try:
                new_src = _remove_defs_from_source(src, names, only_top_level=True)
                ast.parse(new_src)
            except Exception as exc:
                print(red(f"[ERROR] {path}: {exc}"))
                continue
            if args.dry_run:
                print(cyan(f"[DRY-RUN] Would remove {sorted(names)} from {path}"))
            else:
                if args.backup:
                    shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
                path.write_text(new_src, encoding="utf-8")
                print(green(f"Removed {sorted(names)} from {path}") + (" (backup created)" if args.backup else ""))
                removed_count += 1
        if not args.dry_run:
            print(green(f"\nRemoved unused objects from {removed_count} file(s)."))
    return 0


_SKIPDIRS_RE = re.compile(r"^(.+?):(\d+):\s+unused variable\s+['\"]SKIP_DIRS['\"]")
_UNUSED_VAR_RE = re.compile(
    r"^(?P<path>.*?):(?P<lineno>\d+):\s*unused variable '(?P<var>[^']+)'",
    re.IGNORECASE,
)
_VULTURE_RE = re.compile(
    r"^(.+?):(\d+):\s+"
    r"(unused\s+(function|variable|class|attribute|method|import)\s+'([^']+)'"
    r"|unreachable code after '(\w+)'"
    r"|redundant if-condition"
    r"|unreachable 'else' block"
    r"|unused import '([^']+)'\s+\(\d+% confidence\))$"
)


def _parse_vulture_lines(lines: list[str], mode: str) -> dict[str, list[tuple[int, str, str]]]:
    issues: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    if mode == "skip-dirs":
        for raw in lines:
            m = _SKIPDIRS_RE.match(raw.strip())
            if m:
                issues[m.group(1)].append((int(m.group(2)), "unused_variable", "SKIP_DIRS"))
        return dict(issues)
    if mode == "comment-vars":
        for raw in lines:
            m = _UNUSED_VAR_RE.match(raw.rstrip("\n"))
            if m:
                issues[m.group("path")].append((int(m.group("lineno")), "unused_variable", m.group("var")))
        return dict(issues)
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        m = _VULTURE_RE.match(s)
        if not m:
            continue
        path, lineno = m.group(1), int(m.group(2))
        if m.group(4):
            kind = "unused_" + m.group(4)
            name = m.group(5)
        elif m.group(6):
            kind, name = "unreachable_after", m.group(6)
        elif "redundant if-condition" in s:
            kind, name = "redundant_if", ""
        elif "unreachable 'else' block" in s:
            kind, name = "unreachable_else", ""
        elif m.group(7):
            kind, name = "unused_import", m.group(7)
        else:
            kind, name = "other", ""
        issues[path].append((lineno, kind, name))
    return dict(issues)


def _comment_out(line: str, *, marker: bool = False) -> str:
    stripped = line.lstrip()
    indent = line[: len(line) - len(stripped)]
    if not stripped:
        return indent + "#\n"
    if stripped.startswith("#"):
        return line
    prefix = "# REMOVED: " if marker else "# "
    return indent + prefix + stripped


def _apply_vulture_fixes(path: str, fixes: list[tuple[int, str, str]], mode: str) -> tuple[list[str], list[str]]:
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        original = f.readlines()
    lines = list(original)
    skip_indices: set[int] = set()
    for lineno, kind, _name in sorted(fixes, key=lambda x: x[0], reverse=True):
        idx = lineno - 1
        if idx < 0 or idx >= len(lines) or idx in skip_indices:
            continue
        if mode in {"comment-vars", "skip-dirs"}:
            lines[idx] = _comment_out(lines[idx])
        elif mode == "comment-all":
            lines[idx] = _comment_out(lines[idx], marker=True)
        elif mode == "remove-all":
            lines[idx] = _comment_out(lines[idx], marker=True)
    return original, lines


def _cmd_vulture(args: argparse.Namespace) -> int:
    src_file = args.file
    if src_file:
        try:
            with open(src_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            print(red(f"Error: file not found: {src_file}"), file=sys.stderr)
            return 2
    else:
        print(dim("Reading vulture output from stdin..."))
        lines = sys.stdin.readlines()
    if not lines:
        print(yellow("No input provided."))
        return 0
    issues = _parse_vulture_lines(lines, args.mode)
    if not issues:
        print(yellow("No matching vulture entries found."))
        return 0
    print(bold(f"Found issues in {len(issues)} file(s)."))
    for path, items in issues.items():
        print(f"  {path}: {len(items)} issue(s)")
    if not args.yes and args.apply:
        try:
            ans = input("\nProceed with fixes? (y/N): ").strip().lower()
        except EOFError:
            ans = "n"
        if ans not in ("y", "yes"):
            print("Aborted.")
            return 0
    processed = 0
    modified = 0
    for path, fixes in issues.items():
        if not Path(path).exists():
            print(yellow(f"  [!] file not found: {path}, skipping"))
            continue
        orig, new = _apply_vulture_fixes(path, fixes, args.mode)
        processed += 1
        if orig == new:
            print(dim(f"  no changes for {path}"))
            continue
        modified += 1
        udiff = difflib.unified_diff(orig, new, fromfile=path, tofile=path, lineterm="")
        print("".join(line + "\n" for line in udiff))
        if args.apply:
            if args.backup:
                shutil.copy2(path, path + ".bak")
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(new)
                print(green(f"  [written] {path}"))
            except Exception as exc:
                print(red(f"  [!] failed to write {path}: {exc}"))
    if args.summary:
        print()
        print(bold(f"Summary: {processed} file(s) processed, {modified} modified."))
        if not args.apply:
            print(dim("Dry-run mode — use --apply to write changes."))
    return 0


_REPLACE_FUNC_NAME = "format_size"
_REPLACE_FUNC_NARGS = 1
_REPLACE_FUNC_BODY = textwrap.dedent("""
    def format_size(size_bytes: int) -> str:
        size = float(size_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024.0:
                return f"{size:.2f} {unit}"
            size /= 1024.0
        return f"{size:.2f} PB"
""").lstrip("\n")
_REPLACE_FUNC_AST = ast.dump(ast.parse(_REPLACE_FUNC_BODY).body[0])


def _is_target_func(node: ast.AST, inspect_only: bool) -> bool:
    if not isinstance(node, ast.FunctionDef):
        return False
    if node.name != _REPLACE_FUNC_NAME:
        return False
    if inspect_only:
        return len(node.args.args) == _REPLACE_FUNC_NARGS
    return ast.dump(node) == _REPLACE_FUNC_AST


def _replace_func_in_file(path: Path, inspect_only: bool) -> tuple[Path, bool, str]:
    if path.name == "pycleaner.py":
        return path, False, "Skipped by filename"
    try:
        src = path.read_text(encoding="utf-8")
    except Exception as exc:
        return path, False, f"Read error: {exc}"
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return path, False, "Original file has a syntax error"
    targets = [n for n in tree.body if _is_target_func(n, inspect_only)]
    if not targets:
        return path, False, "Target function not found"
    drop_lines: set[int] = set()
    for t in targets:
        start = t.lineno - 1
        if t.decorator_list:
            start = t.decorator_list[0].lineno - 1
        drop_lines.update(range(start, t.end_lineno))
    last_import = -1
    for n in tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            last_import = max(last_import, n.end_lineno - 1)
    docstring_end = 0
    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        docstring_end = tree.body[0].end_lineno
    new_lines: list[str] = []
    inserted = False
    for i, line in enumerate(src.splitlines(keepends=True)):
        if i in drop_lines:
            continue
        new_lines.append(line)
        if inserted:
            continue
        if last_import != -1 and i == last_import:
            new_lines.append(f"from dh import {_REPLACE_FUNC_NAME}\n")
            inserted = True
        elif last_import == -1 and i == docstring_end - 1:
            new_lines.append(f"from dh import {_REPLACE_FUNC_NAME}\n")
            inserted = True
    if not inserted:
        new_lines.insert(0, f"from dh import {_REPLACE_FUNC_NAME}\n")
    new_src = "".join(new_lines)
    try:
        ast.parse(new_src)
    except SyntaxError as exc:
        return path, False, f"Validation failed: {exc}"
    try:
        path.write_text(new_src, encoding="utf-8")
    except Exception as exc:
        return path, False, f"Write error: {exc}"
    return path, True, "Successfully updated"


def _cmd_replace_func(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    files = [p for p in root.rglob("*.py") if p.is_file() and p.resolve() != Path(__file__).resolve()]
    if not files:
        print(yellow("No Python files found."))
        return 0
    mode = "NAME/ARGUMENT CHECK" if args.inspect else "EXACT BODY CHECK"
    print(bold(f"Mode: {mode}"))
    print(f"Found {len(files)} Python files. Processing with {args.workers} workers...")
    print("Changes will be applied automatically.\n")
    with Pool(args.workers) as pool:
        for path, ok, msg in pool.starmap(_replace_func_in_file, [(f, args.inspect) for f in files]):
            if ok:
                print(green(f"[UPDATED] {path}: {msg}"))
            elif msg != "Target function not found":
                print(yellow(f"[SKIPPED] {path}: {msg}"))
    return 0


def _read_block(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(red(f"Failed to read {path}: {exc}"), file=sys.stderr)
        raise
    return [line.rstrip() for line in text.strip("\n").splitlines()]


def _find_block(lines: list[str], block: list[str]) -> tuple[int, int] | None:
    stripped = [line.rstrip("\n").rstrip() for line in lines]
    n, m = len(stripped), len(block)
    for i in range(n - m + 1):
        if stripped[i : i + m] == block:
            return i, i + m
    return None


def _already_has_import(tree: ast.Module, name: str) -> bool:
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "dh" and any(a.name == name for a in node.names):
            return True
        if isinstance(node, ast.Import) and any(a.name == name for a in node.names):
            return True
    return False


def _top_import_end(tree: ast.Module) -> int:
    end = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if node.end_lineno:
                end = max(end, node.end_lineno)
        else:
            break
    return end


def _replace_block_in_file(path: Path, block: list[str], import_line: str) -> None:
    if path.resolve() == Path(__file__).resolve():
        return
    try:
        src = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        print(yellow(f"Skipping {path}: {exc}"))
        return
    lines = src.splitlines(keepends=True)
    match = _find_block(lines, block)
    if match is None:
        return
    start, end = match
    if start > 0 and lines[start - 1].strip() == "":
        start -= 1
    if end < len(lines) and lines[end].strip() == "":
        end += 1
    del lines[start:end]
    new_src = "".join(lines)
    try:
        tree = ast.parse(new_src)
    except SyntaxError as exc:
        print(yellow(f"Skipping write for {path} (would break syntax): {exc}"))
        return
    import_name = import_line.strip().split()[-1]
    if _already_has_import(tree, import_name):
        if new_src != src:
            path.write_text(new_src, encoding="utf-8")
            print(green(f"Removed block: {path} (import already present)"))
        return
    body = new_src.splitlines(keepends=True)
    top = _top_import_end(tree)
    insert_at = top if top > 0 else (1 if body and body[0].startswith("#!") else 0)
    body.insert(insert_at, import_line if import_line.endswith("\n") else import_line + "\n")
    path.write_text("".join(body), encoding="utf-8")
    print(green(f"Removed block and added import: {path}"))


def _cmd_replace_block(args: argparse.Namespace) -> int:
    block_path = Path(args.block_file).expanduser()
    block = _read_block(block_path)
    if args.files:
        files = [Path(f) for f in args.files]
    else:
        files = _iter_py_files(Path.cwd(), _SKIP_DIRS)
    for f in files:
        try:
            _replace_block_in_file(f, block, args.import_line)
        except Exception as exc:
            print(red(f"Error processing {f}: {exc}"))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pycleaner",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colour output.")
    sub = parser.add_subparsers(dest="command", required=True)
    p_imp = sub.add_parser("imports", help="Detect (and optionally remove) unused imports.")
    p_imp.add_argument(
        "paths",
        nargs="*",
        metavar="PATH",
        help="Files, dirs, .whl, .tar.zst (default: current dir).",
    )
    p_imp.add_argument(
        "--backend",
        choices=["ast", "autoflake"],
        default="ast",
        help="Detection engine (default: ast).",
    )
    p_imp.add_argument("-a", "--autofix", action="store_true", help="Remove unused imports in-place.")
    p_imp.add_argument("--dry-run", action="store_true", help="Preview changes without writing.")
    p_imp.add_argument("-v", "--verbose", action="store_true")
    p_imp.add_argument("--workers", type=int, default=8, metavar="N")
    p_imp.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Regex to exclude files (repeatable).",
    )
    p_imp.add_argument(
        "--ignore-init",
        action="store_true",
        help="Treat all imports in __init__.py as used.",
    )
    p_imp.add_argument(
        "--diff",
        action="store_true",
        help="(autoflake backend) show unified diff of changes.",
    )
    p_imp.set_defaults(func=_cmd_imports)
    p_def = sub.add_parser("defs", help="Detect/remove/extract unused functions, classes, constants.")
    p_def.add_argument("--dir", default=".", help="Root directory to scan (default: current dir).")
    p_def.add_argument(
        "--scope",
        choices=["global", "file"],
        default="global",
        help="'global' (cross-file, detect_unused.py) or 'file' (per-file, rmunused*). Default: global.",
    )
    p_def.add_argument(
        "--kind",
        choices=["all", "func", "class", "const"],
        default="all",
        help="Which symbol kinds to report/remove.",
    )
    p_def.add_argument(
        "--remove",
        action="store_true",
        help="Remove unused top-level functions/classes in-place.",
    )
    p_def.add_argument("--backup", action="store_true", help="Create .bak copies when removing.")
    p_def.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed (implies --remove).",
    )
    p_def.add_argument(
        "--extract",
        action="store_true",
        help="Extract unused objects into ./output/{func,classes,const}/.",
    )
    p_def.add_argument(
        "--extract-dir",
        default=None,
        help="Override extraction root dir (default: <dir>/output).",
    )
    p_def.add_argument("--workers", type=int, default=8)
    p_def.set_defaults(func=_cmd_defs)
    p_vul = sub.add_parser("vulture", help="Process vulture output file and fix/annotate findings.")
    p_vul.add_argument("file", nargs="?", help="Vulture output file (default: stdin).")
    p_vul.add_argument(
        "--mode",
        choices=["comment-vars", "comment-all", "remove-all", "skip-dirs"],
        default="comment-vars",
        help="Which kind of fix to apply.",
    )
    p_vul.add_argument(
        "--apply",
        action="store_true",
        help="Actually write changes (default: show diff only).",
    )
    p_vul.add_argument("--backup", action="store_true", help="Save .bak copies before writing.")
    p_vul.add_argument("--yes", action="store_true", help="Do not prompt for confirmation.")
    p_vul.add_argument("--summary", action="store_true", help="Print summary at the end.")
    p_vul.set_defaults(func=_cmd_vulture)
    p_rep = sub.add_parser("replace", help="Replace/patch functions or code blocks across files.")
    rep_sub = p_rep.add_subparsers(dest="replace_mode", required=True)
    p_rf = rep_sub.add_parser("func", help="Replace `format_size` function with an import.")
    p_rf.add_argument("--dir", default=".")
    p_rf.add_argument(
        "-i",
        "--inspect",
        action="store_true",
        help="Match by name+argcount only (skip full body check).",
    )
    p_rf.add_argument("--workers", type=int, default=8)
    p_rf.set_defaults(func=_cmd_replace_func)
    p_rb = rep_sub.add_parser("block", help="Remove a fixed block of code from many files.")
    p_rb.add_argument("files", nargs="*", help="Target .py files (default: cwd rglob).")
    p_rb.add_argument(
        "--block-file",
        default=str(Path.home() / "lic"),
        help="Path to file containing the block to remove (default: ~/lic).",
    )
    p_rb.add_argument(
        "--import-line",
        default="from dh import cprint",
        help="Import line to ensure exists after removal (default: 'from dh import cprint').",
    )
    p_rb.set_defaults(func=_cmd_replace_block)
    return parser


def main(argv: list[str] | None = None) -> int:
    global _USE_COLOR
    parser = _build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "no_color", False) or not sys.stdout.isatty():
        _USE_COLOR = False
    return args.func(args)


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
