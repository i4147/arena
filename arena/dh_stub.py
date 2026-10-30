#!/usr/bin/env python3
"""arena/dh_stub.py — stand-in for the author's private `dh` module. TEST HARNESS ONLY.

`dh` is not in this repository and is not on PyPI.  Fifteen of the original 96
scripts imported it (`from dh import SKIP_DIRS, CSTStripper, TSStripper,
atomic_write`) and so does the delivered `rmc_ts.py`, so nothing could be
tested end-to-end without one.  This module reproduces the interface that
your `rmc_termux.py` relies on, using the real py-tree-sitter grammar:

    from dh import SKIP_DIRS, TSStripper, atomic_write
    TSStripper(**config).strip(source: str) -> str

It is *not* a reimplementation of your stripper: it preserves shebangs, encoding
lines, tool directives and the module docstring, which is enough to drive the
CLI, the worker pool, the compile gate and the encoding handling.  Line-level
output will differ from yours in the corner cases your module was written for.

To use it, make it importable under the name `dh` (never import it from the repo):

    mkdir -p /tmp/dhstub && cp arena/dh_stub.py /tmp/dhstub/dh.py
    PYTHONPATH=/tmp/dhstub .venv/bin/python rmc_ts.py src/ -n

`arena/smoke_rmc.sh` does exactly that, and `arena/make_dh_stubs.py` derives the
older/newer signature variants that `rmc_ts.py`'s kwarg filter is there to absorb.
"""

from __future__ import annotations

import ast
import io
import os
import re
import stat
import tempfile
from pathlib import Path

try:
    import tree_sitter
    import tree_sitter_python
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "dh_stub needs the tree-sitter grammar: pip install tree_sitter tree_sitter_python"
    ) from exc

SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".idea", ".vscode", ".venv", "venv", "node_modules",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".cache",
    "target", "dist", "build",
})

_PARSER = tree_sitter.Parser(tree_sitter.Language(tree_sitter_python.language()))

#: comments a human would be annoyed to lose
_KEEP = re.compile(
    r"#\s*(type:|noqa|pylint:|pragma|fmt:|isort:|shellcheck|!|TODO|FIXME|XXX)"
    r"|SPDX-|Copyright|@license|coding[:=]",
    re.I,
)
#: tool directives, if `preserve_directives` is on
_DIRECTIVE = re.compile(r"#\s*(type:|noqa|pylint:|pragma|fmt:|isort:)", re.I)


class TSStripper:
    """The subset of the dh API that `rmc_ts.py` and `rmc_termux.py` touch."""

    def __init__(
        self,
        strip_comments: bool = True,
        strip_docstrings: bool = True,
        preserve_shebangs: bool = True,
        preserve_module_docstring: bool = True,
        preserve_directives: bool = True,
        preserve_commented_code: bool = True,
        validate: bool = True,
        remove_blank_lines: bool = False,
        **unknown: object,  # tolerate anything a caller passes
    ) -> None:
        self.strip_comments = strip_comments
        self.strip_docstrings = strip_docstrings
        self.preserve_shebangs = preserve_shebangs
        self.preserve_module_docstring = preserve_module_docstring
        self.preserve_directives = preserve_directives
        self.preserve_commented_code = preserve_commented_code
        self.validate = validate
        self.remove_blank_lines = remove_blank_lines

    # -- comments ----------------------------------------------------------
    def _comment_spans(self, src: bytes, first_line_end: int) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        stack = [_PARSER.parse(src).root_node]
        while stack:
            node = stack.pop()
            if node.type == "comment":
                text = src[node.start_byte:node.end_byte].decode("utf-8", "replace")
                keep = bool(_KEEP.search(text))
                if self.preserve_shebangs and node.start_byte < first_line_end:
                    keep = True
                if self.preserve_directives and _DIRECTIVE.search(text):
                    keep = True
                if not keep:
                    start = node.start_byte
                    line_start = src.rfind(b"\n", 0, start) + 1
                    end = src.find(b"\n", node.end_byte)
                    end = len(src) if end == -1 else end + 1
                    if not src[line_start:start].strip():
                        spans.append((line_start, end))            # whole-line comment
                    else:
                        while start > line_start and src[start - 1:start] in b" \t":
                            start -= 1
                        spans.append((start, node.end_byte))       # trailing comment
                continue
            stack.extend(node.children)
        return spans

    # -- docstrings --------------------------------------------------------
    def _docstring_spans(self, src: bytes) -> list[tuple[int, int, bytes]]:
        tree = ast.parse(src)
        lines = io.BytesIO(src).readlines()
        offsets = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))
        edits: list[tuple[int, int, bytes]] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.Module) and self.preserve_module_docstring:
                continue
            if not node.body:
                continue
            first = node.body[0]
            if not (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                continue
            start = offsets[first.lineno - 1] + first.col_offset
            end = offsets[first.end_lineno - 1] + first.end_col_offset
            line_start = offsets[first.lineno - 1]
            line_end = offsets[first.end_lineno - 1] + len(lines[first.end_lineno - 1])
            alone = not src[line_start:start].strip() and not src[end:line_end].strip()
            if alone and len(node.body) > 1:
                edits.append((line_start, line_end, b""))
            else:
                edits.append((start, end, b"pass"))
        return edits

    # -- the one method the tools call -------------------------------------
    def strip(self, source: str) -> str:
        src = source.encode("utf-8")
        edits: list[tuple[int, int, bytes]] = []
        if self.strip_docstrings:
            edits.extend(self._docstring_spans(src))
        if self.strip_comments and b"#" in src:
            first_line_end = src.find(b"\n")
            first_line_end = len(src) if first_line_end == -1 else first_line_end
            edits.extend((start, end, b"") for start, end in self._comment_spans(src, first_line_end))

        out = bytearray()
        cursor = 0
        for start, end, repl in sorted(edits):
            if start < cursor:          # overlapping spans: first one wins
                continue
            out.extend(src[cursor:start])
            out.extend(repl)
            cursor = end
        out.extend(src[cursor:])
        result = out.decode("utf-8")

        if self.remove_blank_lines:
            result = "\n".join(line for line in result.splitlines() if line.strip()) + "\n"
        if self.validate and result != source:
            ast.parse(result)
        return result


def atomic_write(path: Path, data: bytes) -> None:
    """tmp in the same directory -> copy the mode -> os.replace."""
    path = Path(path)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, stat.S_IMODE(path.stat().st_mode))
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


__all__ = ["SKIP_DIRS", "TSStripper", "atomic_write"]
