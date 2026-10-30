#!/data/data/com.termux/files/usr/bin/env python
import argparse
import ast
import contextlib
import functools
import itertools
import keyword
import os
import re
import shutil
import tempfile
import textwrap
from collections import deque
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from typing import NamedTuple, TypeVar

import libcst as cst
from dh import SKIP_DIRS
from libcst.metadata import MetadataWrapper, PositionProvider
from rich.console import Console
from rich.text import Text

DIRECTIVE = re.compile(
    r"(?:^|#)\s*(?:noqa\b|type\s*:|pylint:|pyright:|pyre-|pytype:|mypy:|ruff:|flake8:|"
    r"isort:|fmt:|yapf:|black:|autopep8:|pragma\b|nosec\b|nocover\b|skipcq|noinspection|"
    r"coding[:=]|-\*-|vim?:|ex:|region\b|endregion\b|cython:|lint:|ty:)",
    re.I,
)
PUNCT = re.compile(r"[=()\[\]{}:]")
WORD = re.compile(r"\w+")
COMMENT_LINE = re.compile(r"^[ \t]*#")
HASH_PREFIX = re.compile(r"^[ \t]*#[ ]?")
D = TypeVar("D", cst.FunctionDef, cst.ClassDef)


@dataclass(frozen=True)
class Options:
    full: bool = False
    docs: bool = False
    all: bool = False
    diff: bool = False


class Result(NamedTuple):
    path: str
    delta: int
    error: str | None = None
    diff: str = ""


def is_trivial_expr(expr: ast.expr) -> bool:
    match expr:
        case ast.Name() | ast.Constant() | ast.Attribute():
            return True
        case ast.Tuple(elts=elts) | ast.List(elts=elts):
            return all(is_trivial_expr(e) for e in elts)
        case ast.UnaryOp(operand=operand):
            return is_trivial_expr(operand)
        case ast.BoolOp(values=values):
            return all(is_trivial_expr(v) for v in values)
        case _:
            return False


def is_trivial(stmt: ast.stmt) -> bool:
    match stmt:
        case ast.Expr(value=value):
            return is_trivial_expr(value)
        case ast.AnnAssign(value=None):
            return True
        case _:
            return False


def candidates(body: str, first: str) -> list[str]:
    if body.startswith("@"):
        return [body, body + "\ndef _(): pass"]
    if not body.endswith(":"):
        return [body]
    pad = body + "\n    pass"
    wrap = {
        "else": "if 1:\n    pass\n" + pad,
        "elif": "if 1:\n    pass\n" + pad,
        "except": "try:\n    pass\n" + pad,
        "finally": "try:\n    pass\n" + pad,
        "try": pad + "\nfinally:\n    pass",
    }
    return [body, pad, wrap.get(first, body)]


@functools.cache
def is_code(body: str) -> bool:
    word = WORD.match(body)
    first = word.group() if word else ""
    if not (PUNCT.search(body) or body.startswith("@") or keyword.iskeyword(first)):
        return False
    for cand in candidates(body, first):
        try:
            tree = ast.parse(cand)
        except (SyntaxError, ValueError):
            continue
        return any(not is_trivial(s) for s in tree.body)
    return False


def code_block_rows(src: bytes) -> frozenset[int]:
    lines = [line.rstrip("\r") for line in src.decode("utf-8", "replace").split("\n")]
    rows: set[int] = set()
    groups = itertools.groupby(range(len(lines)), key=lambda i: bool(COMMENT_LINE.match(lines[i])))
    for is_comment, group in groups:
        idx = list(group)
        if is_comment and len(idx) > 1:
            text = "\n".join(HASH_PREFIX.sub("", lines[i]) for i in idx)
            if is_code(textwrap.dedent(text).strip("\n")):
                rows.update(idx)
    return frozenset(rows)


def is_text(node: cst.BaseExpression) -> bool:
    match node:
        case cst.SimpleString():
            return "b" not in node.prefix.lower()
        case cst.ConcatenatedString():
            return is_text(node.left) and is_text(node.right)
        case _:
            return False


def is_docstring(stmt: cst.BaseSmallStatement) -> bool:
    return isinstance(stmt, cst.Expr) and is_text(stmt.value)


def drop_docstring(stmts: Sequence[cst.BaseStatement]) -> list[cst.BaseStatement]:
    first = stmts[0] if stmts else None
    if not isinstance(first, cst.SimpleStatementLine) or not is_docstring(first.body[0]):
        return list(stmts)
    rest = list(stmts[1:])
    if len(first.body) > 1:
        return [first.with_changes(body=first.body[1:]), *rest]
    if rest:
        rest[0] = rest[0].with_changes(leading_lines=[*first.leading_lines, *rest[0].leading_lines])
    return rest


class Cleaner(cst.CSTTransformer):
    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, opts: Options, blocks: frozenset[int]) -> None:
        super().__init__()
        self.opts = opts
        self.blocks = blocks

    def keep(self, text: str, row: int) -> bool:
        if self.opts.all:
            return False
        if (row == 0 and text.startswith("#!")) or row in self.blocks:
            return True
        return bool(DIRECTIVE.search(text)) or is_code(text.lstrip("#").strip())

    def row_of(self, node: cst.CSTNode) -> int:
        return self.get_metadata(PositionProvider, node).start.line - 1

    def leave_TrailingWhitespace(
        self, original: cst.TrailingWhitespace, updated: cst.TrailingWhitespace
    ) -> cst.TrailingWhitespace:
        comment = updated.comment
        if comment is None or self.keep(comment.value, self.row_of(original)):
            return updated
        return updated.with_changes(whitespace=cst.SimpleWhitespace(""), comment=None)

    def leave_EmptyLine(self, original: cst.EmptyLine, updated: cst.EmptyLine) -> cst.EmptyLine | cst.RemovalSentinel:
        if updated.comment is None:
            return cst.RemoveFromParent()
        if not self.opts.full or self.keep(updated.comment.value, self.row_of(original)):
            return updated
        return cst.RemoveFromParent()

    def undoc(self, node: D) -> D:
        if not self.opts.docs:
            return node
        body = node.body
        if isinstance(body, cst.IndentedBlock):
            stmts = drop_docstring(body.body) or [cst.SimpleStatementLine([cst.Pass()])]
            return node.with_changes(body=body.with_changes(body=stmts))
        if body.body and is_docstring(body.body[0]):
            return node.with_changes(body=body.with_changes(body=list(body.body[1:]) or [cst.Pass()]))
        return node

    def leave_FunctionDef(self, original: cst.FunctionDef, updated: cst.FunctionDef) -> cst.FunctionDef:
        return self.undoc(updated)

    def leave_ClassDef(self, original: cst.ClassDef, updated: cst.ClassDef) -> cst.ClassDef:
        return self.undoc(updated)

    def leave_Module(self, original: cst.Module, updated: cst.Module) -> cst.Module:
        if not self.opts.all:
            return updated
        return updated.with_changes(body=drop_docstring(updated.body))


def parses(src: bytes) -> bool:
    try:
        ast.parse(src)
    except (SyntaxError, ValueError):
        return False
    return True


def is_valid(new: bytes, old: bytes) -> bool:
    return parses(new) or not parses(old)


def diff_text(path: str, old: bytes, new: bytes) -> str:
    return "\n".join(
        unified_diff(
            old.decode("utf-8", "replace").splitlines(),
            new.decode("utf-8", "replace").splitlines(),
            fromfile=path,
            tofile=path,
            lineterm="",
        )
    )


def diff_style(line: str) -> str:
    for prefix, style in (("+++", "bold"), ("---", "bold"), ("@@", "cyan"), ("+", "green"), ("-", "red")):
        if line.startswith(prefix):
            return style
    return ""


def atomic_write(path: str, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".pystrip-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class Stripper:
    def __init__(self, opts: Options) -> None:
        self.opts = opts

    def strip(self, src: bytes) -> bytes:
        module = cst.parse_module(src)
        blocks = code_block_rows(src) if self.opts.full else frozenset()
        return MetadataWrapper(module, unsafe_skip_copy=True).visit(Cleaner(self.opts, blocks)).bytes

    def run(self, path: str) -> Result:
        real = os.path.realpath(path)
        try:
            src = Path(real).read_bytes()
            new = self.strip(src)
            if new == src:
                return Result(path, 0)
            if not is_valid(new, src):
                return Result(path, 0, "result failed validation, file untouched")
            delta = len(src) - len(new)
            if self.opts.diff:
                return Result(path, delta, diff=diff_text(path, src, new))
            atomic_write(real, new)
            return Result(path, delta)
        except Exception as e:
            return Result(path, 0, f"{type(e).__name__}: {e}")


class Walker:
    def __init__(self, roots: list[str], err: Console) -> None:
        self.roots = roots
        self.err = err
        self.seen: set[str] = set()

    def __iter__(self) -> Iterator[str]:
        for root in self.roots:
            if os.path.isdir(root):
                for folder, dirs, names in os.walk(root):
                    dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.endswith(".dist-info"))
                    for name in sorted(names):
                        path = os.path.join(folder, name)
                        if name.endswith(".py") and not os.path.islink(path) and self.fresh(path):
                            yield path
            elif os.path.isfile(root):
                if self.fresh(root):
                    yield root
            else:
                self.err.print(Text(f"warning: {root}: not found", style="yellow"))

    def fresh(self, path: str) -> bool:
        real = os.path.realpath(path)
        if real in self.seen:
            return False
        self.seen.add(real)
        return True


class App:
    def __init__(self, argv: list[str] | None = None) -> None:
        args = self.parser().parse_args(argv)
        self.paths: list[str] = args.paths
        self.workers: int = max(1, args.j)
        self.stripper = Stripper(Options(full=args.c or args.a, docs=args.d or args.a, all=args.a, diff=args.diff))
        self.out = Console(highlight=False)
        self.err = Console(stderr=True, highlight=False)

    @staticmethod
    def parser() -> argparse.ArgumentParser:
        ap = argparse.ArgumentParser(description="Strip comments and docstrings from Python files in place.")
        ap.add_argument("paths", nargs="*", default=[os.getcwd()], help="files or directories")
        ap.add_argument("-c", action="store_true", help="strip all comments, not just inline ones")
        ap.add_argument("-d", action="store_true", help="strip all docstrings (module docstring kept)")
        ap.add_argument(
            "-a",
            action="store_true",
            help="strip everything: shebang, module docstring, directives and commented-out code",
        )
        ap.add_argument("-diff", action="store_true", help="show colored diff and leave files untouched")
        ap.add_argument("-j", type=int, default=1, help="worker processes (>1 enables ProcessPoolExecutor)")
        return ap

    def results(self) -> Iterator[Result]:
        paths = Walker(self.paths, self.err)
        if self.workers == 1:
            for path in paths:
                yield self.stripper.run(path)
        else:
            yield from self.parallel(paths)

    def parallel(self, paths: Iterable[str]) -> Iterator[Result]:
        pending: deque[Future[Result]] = deque()
        pool = ProcessPoolExecutor(max_workers=self.workers)
        try:
            for path in paths:
                pending.append(pool.submit(self.stripper.run, path))
                if len(pending) >= 4 * self.workers:
                    yield pending.popleft().result()
            while pending:
                yield pending.popleft().result()
        finally:
            pool.shutdown(cancel_futures=True)

    def row(self, idx: int, name: str, tail: str, style: str) -> Text:
        head = f"[{idx}]   "
        room = max(8, self.out.width - len(head) - len(tail) - 3)
        short = name if len(name) <= room else name[: room - 1] + "…"
        return Text.assemble(head, short, "   ", (tail, style))

    def show(self, idx: int, res: Result) -> None:
        if res.error:
            tail, style = "error", "cyan"
        else:
            tail, style = f"{res.delta:,}", "green" if res.delta else "dim"
        self.out.print(self.row(idx, Path(res.path).name, tail, style))
        for line in res.diff.splitlines():
            self.out.print(Text(line, style=diff_style(line)), soft_wrap=True)

    def run(self) -> int:
        count = changed = total = 0
        errors: list[Result] = []
        try:
            for count, res in enumerate(self.results(), 1):
                if res.error:
                    errors.append(res)
                else:
                    total += res.delta
                    changed += int(res.delta > 0)
                self.show(count, res)
        except KeyboardInterrupt:
            self.err.print(Text("warning: interrupted by user, exiting", style="yellow"))
        if not count:
            self.err.print(Text("warning: no files", style="yellow"))
            return 1
        verb = "would reduce" if self.stripper.opts.diff else "reduced"
        self.out.print(Text.assemble(f"\n{changed}/{count} files changed, ", (f"{total:,}", "green"), f" bytes {verb}"))
        for res in errors:
            self.err.print(Text(f"error: {res.path}: {res.error}", style="cyan"))
        return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(App().run())
