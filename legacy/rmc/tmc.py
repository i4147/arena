#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

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
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from multiprocessing.pool import ThreadPool
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, Protocol, cast

from dh import atomic_write

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
