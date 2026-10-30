#!/data/data/com.termux/files/usr/bin/env python
import argparse
import contextlib
import functools
import os
import re
import shutil
import tempfile
from collections import deque
from collections.abc import Iterable, Iterator
from concurrent.futures import Future, ProcessPoolExecutor
from dataclasses import dataclass
from difflib import unified_diff
from pathlib import Path
from typing import NamedTuple

import tree_sitter_rust as tsrs
from dh import SKIP_DIRS
from rich.console import Console
from rich.text import Text
from tree_sitter import Language, Node, Parser

Edit = tuple[int, int, bytes]
SAFETY = re.compile(r"^(?://|/\*)\s*SAFETY\s*:")
COMMENTS = {"line_comment", "block_comment"}
STRINGS = {"string_literal", "raw_string_literal", "byte_string_literal", "raw_byte_string_literal"}


@functools.cache
def ts_parser() -> Parser:
    return Parser(Language(tsrs.language()))


@dataclass(frozen=True)
class Options:
    diff: bool = False


class Result(NamedTuple):
    path: str
    delta: int
    error: str | None = None
    diff: str = ""


def is_doc(text: str) -> bool:
    return text.startswith(("///", "//!", "/**", "/*!")) and not text.startswith(("////", "/***", "/**/"))


def keep(text: str) -> bool:
    return is_doc(text) or bool(SAFETY.match(text))


def iter_nodes(root: Node) -> Iterator[Node]:
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        if node.type not in COMMENTS:
            stack.extend(node.children)


def span(node: Node) -> range:
    return range(node.start_point[0] + 1, node.end_point[0] + 1)


def blank_edits(lines: list[bytes], protected: set[int]) -> Iterator[Edit]:
    pos = 0
    last = len(lines) - 1
    for i, line in enumerate(lines):
        if i not in protected and not line.strip() and not (i == last and not line):
            yield pos, pos + len(line) + 1, b""
        pos += len(line) + 1


def apply_edits(src: bytes, edits: list[Edit]) -> bytes:
    out: list[bytes] = []
    pos = 0
    for start, end, text in sorted(edits):
        if start < pos:
            continue
        out.extend((src[pos:start], text))
        pos = end
    out.append(src[pos:])
    return b"".join(out)


def is_valid(new: bytes) -> bool:
    return not ts_parser().parse(new).root_node.has_error


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
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".rsstrip-", suffix=".tmp")
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
        while True:
            new = self.strip_once(src)
            if new == src:
                return new
            src = new

    def strip_once(self, src: bytes) -> bytes:
        root = ts_parser().parse(src).root_node
        if root.has_error:
            raise ValueError("syntax error (tree-sitter)")
        lines = src.split(b"\n")
        edits: list[Edit] = []
        protected: set[int] = set()
        for node in iter_nodes(root):
            if node.type in COMMENTS:
                self.comment_edit(src, node, edits, protected)
            elif node.type in STRINGS:
                protected.update(span(node))
        edits.extend(blank_edits(lines, protected))
        return apply_edits(src, edits)

    def comment_edit(self, src: bytes, node: Node, edits: list[Edit], protected: set[int]) -> None:
        text = src[node.start_byte : node.end_byte].decode("utf-8", "replace")
        if keep(text):
            protected.update(span(node))
            return
        start, end = node.start_byte, node.end_byte
        line_start = start - node.start_point[1]
        line_end = src.find(b"\n", end)
        if line_end < 0:
            line_end = len(src)
        before = src[line_start:start]
        after = src[end:line_end]
        if before.strip():
            edits.append((line_start + len(before.rstrip()), end, b""))
        elif after.strip():
            edits.append((start, end + len(after) - len(after.lstrip()), b""))
        else:
            edits.append((line_start, min(line_end + 1, len(src)), b""))

    def run(self, path: str) -> Result:
        real = os.path.realpath(path)
        try:
            src = Path(real).read_bytes()
            new = self.strip(src)
            if new == src:
                return Result(path, 0)
            if not is_valid(new):
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
                        if name.endswith(".rs") and not os.path.islink(path) and self.fresh(path):
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
        self.stripper = Stripper(Options(diff=args.diff))
        self.out = Console(highlight=False)
        self.err = Console(stderr=True, highlight=False)

    @staticmethod
    def parser() -> argparse.ArgumentParser:
        ap = argparse.ArgumentParser(
            description="Strip comments from Rust files in place, keeping doc and SAFETY comments."
        )
        ap.add_argument("paths", nargs="*", default=[os.getcwd()], help="files or directories")
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
