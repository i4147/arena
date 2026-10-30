#!/usr/bin/env python3
"""rmc.py — strip comments, docstrings and type annotations from Python code.

Consolidates rmc/'s twenty-nine scripts into one program with three engines:

    tree-sitter  byte precise spans, fastest, needs tree_sitter_python
    libcst       CST round-trip: exact whitespace, understands docstring owners
    ast          stdlib only; --ast-mode splice keeps layout, unparse rewrites it

Every engine is followed by the same gate: the result must compile, and once the
removed items are erased from both ASTs the two must be identical.  A file that
fails the gate is left untouched and reported as ``rejected``.

    rmc.py strip -c -d .            remove comments and docstrings
    rmc.py strip -a -b libcst src/  remove everything, via libcst
    rmc.py check .                  report only
    rmc.py bench .                  time the three engines
    rmc.py doctor                   which engines are usable here

Exit codes: 0 ok · 1 errors or --check found work · 2 usage · 130 interrupted
"""

from __future__ import annotations

import argparse
import ast
import copy
import difflib
import fnmatch
import importlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import tokenize
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

__version__ = "1.0.0"
PROG = "rmc.py"
MIN_PY = (3, 10)

DEFAULT_SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", ".idea", ".vscode", ".venv", "venv", "env",
        "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", ".tox", ".nox", ".cache", "build", "dist", "target",
        "site-packages", ".eggs",
    }
)

DIRECTIVE_RE = re.compile(
    r"#\s*(?:type|fmt|coding|encoding)\s*:|noqa|nosec|nolint|noqa:"
    r"|pylint:|pyright:|mypy:|pragma:|isort:|shebang|-\*-|spdx-|copyright",
    re.IGNORECASE,
)
TODO_RE = re.compile(r"#\s*(?:TODO|FIXME|XXX|HACK)\b", re.IGNORECASE)
DOC_PREFIXES = ("///", "//!", "/**", "/*!")


# --------------------------------------------------------------------------- #
# optional engines                                                            #
# --------------------------------------------------------------------------- #

_PROBE: dict[str, object | None] = {}


def probe(name: str) -> object | None:
    if name not in _PROBE:
        module: object | None = None
        try:
            if importlib.util.find_spec(name) is not None:
                module = importlib.import_module(name)
        except Exception:  # noqa: BLE001 - a broken optional dep must not kill us
            module = None
        _PROBE[name] = module
    return _PROBE[name]


def ts_parser():
    runtime = probe("tree_sitter")
    grammar_module = probe("tree_sitter_python")
    if runtime is None or grammar_module is None:
        return None
    try:
        return runtime.Parser(runtime.Language(grammar_module.language()))
    except Exception:  # noqa: BLE001 - runtime/grammar version skew
        return None


_PARSER = "unset"


def python_parser():
    global _PARSER
    if _PARSER == "unset":
        _PARSER = ts_parser()
    return _PARSER


# --------------------------------------------------------------------------- #
# policy                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Policy:
    comments: bool = False
    docstrings: bool = False
    annotations: bool = False
    keep_shebang: bool = True
    keep_module_docstring: bool = True
    keep_directives: bool = True
    keep_todo: bool = False
    keep_prefix: tuple[str, ...] = ()
    preserve_lines: bool = False
    eat_trailing_newline: bool = False
    remove_blank_lines: bool = False
    verify: bool = True
    fallback_encoding: str = ""

    @property
    def anything(self) -> bool:
        return self.comments or self.docstrings or self.annotations

    def protected(self, text: str, is_first: bool) -> bool:
        if is_first and self.keep_shebang and text.startswith("#!"):
            return True
        if self.keep_directives and DIRECTIVE_RE.search(text):
            return True
        if self.keep_todo and TODO_RE.search(text):
            return True
        return any(re.search(pattern, text) for pattern in self.keep_prefix)


@dataclass(frozen=True, slots=True)
class Output:
    dry_run: bool = False
    diff: bool = False
    backup: bool = False
    backup_dir: str | None = None


@dataclass(slots=True)
class Result:
    path: str
    status: str
    backend: str = ""
    before: int = 0
    after: int = 0
    comments: int = 0
    docstrings: int = 0
    annotations: int = 0
    detail: str = ""
    diff: str = ""
    elapsed: float = 0.0

    @property
    def removed(self) -> int:
        return self.comments + self.docstrings + self.annotations


# --------------------------------------------------------------------------- #
# source io                                                                   #
# --------------------------------------------------------------------------- #


def decode(raw: bytes, fallback: str = "") -> tuple[str, str]:
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    for encoding in (fallback, "utf-8"):
        if not encoding:
            continue
        try:
            return raw.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1"), "latin-1"


def atomic_write(path: Path, data: bytes) -> None:
    handle_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(data)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def make_backup(path: Path, out: Output) -> None:
    if out.backup_dir:
        root = Path(out.backup_dir).expanduser()
        target = root / path.name
        counter = 1
        while target.exists():
            target = root / f"{path.name}.{counter}"
            counter += 1
        target.parent.mkdir(parents=True, exist_ok=True)
    else:
        target = path.with_name(path.name + ".bak")
    if not target.exists():
        shutil.copy2(path, target)


# --------------------------------------------------------------------------- #
# text surgery shared by the tree-sitter and ast engines                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Span:
    start: int
    end: int
    kind: str = "comment"


def merge(spans: Iterable[Span]) -> list[Span]:
    merged: list[Span] = []
    for span in sorted(spans, key=lambda s: (s.start, s.end)):
        if merged and span.start <= merged[-1].end:
            prev = merged[-1]
            if span.end > prev.end:
                merged[-1] = Span(prev.start, span.end, prev.kind)
        else:
            merged.append(span)
    return merged


def apply_spans(text: str, spans: Sequence[Span], policy: Policy) -> str:
    """Cut spans out of the text, tidying the empty lines they leave behind."""
    pieces: list[str] = []
    pos = 0
    for span in merge(spans):
        if span.start < pos:
            continue
        chunk = text[span.start : span.end]
        if span.kind == "docstring-only":
            pieces.append(text[pos : span.start])
            pieces.append("pass")
            newline = text.find("\n", span.end)
            if newline == -1:
                pos = len(text)
            else:
                pos = newline + 1 if policy.eat_trailing_newline else newline
        elif span.kind == "docstring":
            line_start = text.rfind("\n", 0, span.start) + 1
            newline = text.find("\n", span.end)
            line_end = len(text) if newline == -1 else newline + 1
            if policy.preserve_lines:
                pieces.append(text[pos : span.start])
                pieces.append("\n" * chunk.count("\n"))
                pos = span.end
            else:
                pieces.append(text[pos:line_start])
                pos = line_end
        elif span.kind == "annotation":
            pieces.append(text[pos : span.start])
            pos = span.end
        else:  # comment
            line_start = text.rfind("\n", 0, span.start) + 1
            newline = text.find("\n", span.end)
            line_end = len(text) if newline == -1 else newline
            before = text[line_start : span.start]
            after = text[span.end : line_end]
            if policy.preserve_lines:
                pieces.append(text[pos : span.start])
                pieces.append(re.sub(r"[^\n]", " ", chunk))
                pos = span.end
            elif not before.strip() and not after.strip():
                pieces.append(text[pos:line_start])
                pos = min(line_end + 1, len(text))
            else:
                begin = span.start
                while begin > line_start and text[begin - 1] in " \t":
                    begin -= 1
                finish = span.end
                while finish < line_end and text[finish] in " \t":
                    finish += 1
                pieces.append(text[pos:begin])
                pos = finish
    pieces.append(text[pos:])
    result = "".join(pieces)
    if policy.remove_blank_lines:
        result = re.sub(r"\n{3,}", "\n\n", result)
        result = re.sub(r"[ \t]+$", "", result, flags=re.MULTILINE)
    return result


# --------------------------------------------------------------------------- #
# engine 1: tree-sitter                                                       #
# --------------------------------------------------------------------------- #

COMMENT_TYPES = frozenset({"comment"})
BODY_OWNERS = frozenset({"module", "class_definition", "function_definition"})


def ts_walk(node: object) -> Iterator[object]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        if getattr(current, "is_missing", False):
            continue
        stack.extend(reversed(getattr(current, "children", [])))


def ts_has_error(node: object) -> bool:
    if getattr(node, "is_error", False) or getattr(node, "is_missing", False):
        return True
    return any(ts_has_error(child) for child in getattr(node, "children", []))


def ts_docstring_spans(root: object) -> list[Span]:
    spans: list[Span] = []
    for node in ts_walk(root):
        if getattr(node, "type", "") not in BODY_OWNERS:
            continue
        body = node if node.type == "module" else node.child_by_field_name("body")
        if body is None:
            continue
        statements = [child for child in body.named_children if child.type != "comment"]
        if not statements:
            continue
        first = statements[0]
        if first.type != "expression_statement" or first.named_child_count != 1:
            continue
        if first.named_children[0].type not in ("string", "concatenated_string"):
            continue
        spans.append(Span(first.start_byte, first.end_byte, "docstring-only" if len(statements) == 1 else "docstring"))
    return spans


def _annotation_span(src: bytes, node: object, arrow: bool) -> Span | None:
    """Span covering ``: T`` (or ``-> T``) including its indicator."""
    start, end = node.start_byte, node.end_byte
    index = start - 1
    while index >= 0 and src[index : index + 1] in b" \t\n":
        index -= 1
    if arrow:
        if src[index - 1 : index + 1] != b"->":
            return None
        begin = index - 1
    else:
        if src[index : index + 1] != b":":
            return None
        begin = index
    while begin > 0 and src[begin - 1 : begin] in b" \t":
        begin -= 1
    while end < len(src) and src[end : end + 1] in b" \t":
        end += 1
    return Span(begin, end, "annotation")


def ts_annotation_spans(src: bytes, root: object) -> list[Span]:
    spans: list[Span] = []
    for node in ts_walk(root):
        node_type = getattr(node, "type", "")
        if node_type == "function_definition":
            returned = node.child_by_field_name("return_type")
            if returned is not None:
                span = _annotation_span(src, returned, arrow=True)
                if span is not None:
                    spans.append(span)
        elif node_type in ("typed_parameter", "typed_default_parameter"):
            annotated = node.child_by_field_name("type")
            if annotated is not None:
                span = _annotation_span(src, annotated, arrow=False)
                if span is not None:
                    spans.append(span)
        elif node_type == "assignment":
            annotated = node.child_by_field_name("type")
            if annotated is None:
                continue
            span = _annotation_span(src, annotated, arrow=False)
            if span is None:
                continue
            right = node.child_by_field_name("right")
            if right is None:  # "x: int" on its own: drop the whole line
                line_start = src.rfind(b"\n", 0, node.start_byte) + 1
                newline = src.find(b"\n", node.end_byte)
                spans.append(Span(line_start, len(src) if newline == -1 else newline + 1, "docstring"))
            else:
                spans.append(span)
    return spans


def strip_tree_sitter(text: str, policy: Policy) -> tuple[str, dict[str, int], str | None]:
    parser = python_parser()
    if parser is None:
        return text, {}, "tree-sitter grammar unavailable"
    src = text.encode("utf-8")
    tree = parser.parse(src)
    if ts_has_error(tree.root_node):
        return text, {}, "source does not parse"
    spans: list[Span] = []
    counts = {"comments": 0, "docstrings": 0, "annotations": 0}
    if policy.comments:
        for node in ts_walk(tree.root_node):
            if node.type not in COMMENT_TYPES:
                continue
            raw = text.encode("utf-8")[node.start_byte : node.end_byte].decode("utf-8", "replace")
            if policy.protected(raw, node.start_byte == 0):
                continue
            spans.append(Span(node.start_byte, node.end_byte))
            counts["comments"] += 1
    if policy.docstrings:
        module_span = None
        for span in ts_docstring_spans(tree.root_node):
            if module_span is None and span.start < len(src) and _is_module_docstring(tree.root_node, span):
                module_span = span
                if policy.keep_module_docstring:
                    continue
            spans.append(span)
            counts["docstrings"] += 1
    if policy.annotations:
        found = ts_annotation_spans(src, tree.root_node)
        spans.extend(found)
        counts["annotations"] = len(found)
    if not spans:
        return text, counts, None
    new_text = apply_spans(text, spans, policy)
    problem = verify(text, new_text, policy)
    if problem:
        return text, counts, problem
    return new_text, counts, None


def _is_module_docstring(root: object, span: Span) -> bool:
    for child in getattr(root, "named_children", ()):
        if child.start_byte > span.start:
            break
        if child.type == "expression_statement" and child.start_byte == span.start:
            return True
    return False


# --------------------------------------------------------------------------- #
# engine 2: libcst                                                            #
# --------------------------------------------------------------------------- #


def strip_libcst(text: str, policy: Policy) -> tuple[str, dict[str, int], str | None]:
    cst = probe("libcst")
    if cst is None:
        return text, {}, "libcst not installed"
    counts = {"comments": 0, "docstrings": 0, "annotations": 0}
    no_space = cst.AssignEqual(cst.SimpleWhitespace(""), cst.SimpleWhitespace(""))

    def docstring_body(statements: Sequence[object]) -> list[object] | None:
        if not statements:
            return None
        first = statements[0]
        if not isinstance(first, cst.SimpleStatementLine):
            return None
        body = getattr(first, "body", None)
        if not body or not isinstance(body[0], cst.Expr):
            return None
        if not isinstance(body[0].value, cst.SimpleString):
            return None
        rest = list(statements[1:])
        return rest or [cst.SimpleStatementLine(body=[cst.Pass()])]

    class Transformer(cst.CSTTransformer):  # type: ignore[misc]
        def leave_Comment(self, original_node, updated_node):  # noqa: N802, ANN001
            if not policy.comments:
                return updated_node
            if policy.protected(original_node.value, original_node.value.startswith("#!")):
                return updated_node
            counts["comments"] += 1
            return cst.RemoveFromParent()

        def leave_TrailingWhitespace(self, original_node, updated_node):  # noqa: N802, ANN001
            if policy.comments and original_node.comment is not None and updated_node.comment is None:
                return updated_node.with_changes(whitespace=cst.SimpleWhitespace(""))
            return updated_node

        def leave_EmptyLine(self, original_node, updated_node):  # noqa: N802, ANN001
            if policy.comments and original_node.comment is not None and updated_node.comment is None:
                return updated_node.with_changes(whitespace=cst.SimpleWhitespace(""))
            return updated_node

        def leave_Module(self, original_node, updated_node):  # noqa: N802, ANN001
            if not policy.docstrings or policy.keep_module_docstring:
                return updated_node
            replacement = docstring_body(updated_node.body)
            if replacement is None:
                return updated_node
            counts["docstrings"] += 1
            return updated_node.with_changes(body=replacement)

        def _strip_body(self, updated_node):
            if not policy.docstrings:
                return updated_node
            body = getattr(updated_node, "body", None)
            if not isinstance(body, cst.IndentedBlock):
                return updated_node
            replacement = docstring_body(body.body)
            if replacement is None:
                return updated_node
            counts["docstrings"] += 1
            return updated_node.with_changes(body=body.with_changes(body=replacement))

        def leave_FunctionDef(self, original_node, updated_node):  # noqa: N802, ANN001
            node = self._strip_body(updated_node)
            if not policy.annotations:
                return node
            changes: dict[str, object] = {}
            if node.returns is not None:
                changes["returns"] = None
                counts["annotations"] += 1
            if getattr(node, "type_parameters", None) is not None:
                changes["type_parameters"] = None
            return node.with_changes(**changes) if changes else node

        def leave_ClassDef(self, original_node, updated_node):  # noqa: N802, ANN001
            node = self._strip_body(updated_node)
            if policy.annotations and getattr(node, "type_parameters", None) is not None:
                return node.with_changes(type_parameters=None)
            return node

        def leave_Param(self, original_node, updated_node):  # noqa: N802, ANN001
            if not policy.annotations or updated_node.annotation is None:
                return updated_node
            counts["annotations"] += 1
            changes: dict[str, object] = {"annotation": None}
            if updated_node.equal is not None and updated_node.default is not None:
                changes["equal"] = no_space
            return updated_node.with_changes(**changes)

        def leave_AnnAssign(self, original_node, updated_node):  # noqa: N802, ANN001
            if not policy.annotations:
                return updated_node
            counts["annotations"] += 1
            if updated_node.value is None:
                return cst.RemoveFromParent()
            return cst.Assign(
                targets=[cst.AssignTarget(target=updated_node.target)],
                value=updated_node.value,
                semicolon=updated_node.semicolon,
            )

        def leave_SimpleStatementLine(self, original_node, updated_node):  # noqa: N802, ANN001
            return updated_node if updated_node.body else cst.RemoveFromParent()

        def leave_IndentedBlock(self, original_node, updated_node):  # noqa: N802, ANN001
            if updated_node.body:
                return updated_node
            return updated_node.with_changes(body=[cst.SimpleStatementLine(body=[cst.Pass()])])

    try:
        new_text = cst.parse_module(text).visit(Transformer()).code
    except Exception as exc:  # noqa: BLE001 - libcst rejects some valid Python
        return text, counts, f"libcst: {type(exc).__name__}: {exc}"
    if policy.remove_blank_lines:
        new_text = re.sub(r"(?m)^[ \t]+$", "", new_text)
        new_text = re.sub(r"\n{3,}", "\n\n", new_text)
    if new_text == text:
        return text, counts, None
    problem = verify(text, new_text, policy)
    if problem:
        return text, counts, problem
    return new_text, counts, None


# --------------------------------------------------------------------------- #
# engine 3: ast                                                               #
# --------------------------------------------------------------------------- #


def ast_comment_spans(text: str, policy: Policy) -> list[Span]:
    spans: list[Span] = []
    line_bytes = [len(line.encode("utf-8")) for line in text.splitlines(keepends=True)]
    offsets = [0]
    for size in line_bytes:
        offsets.append(offsets[-1] + size)
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type != tokenize.COMMENT:
                continue
            if policy.protected(token.string, token.start[0] == 1):
                continue
            start = offsets[token.start[0] - 1] + token.start[1]
            end = offsets[token.end[0] - 1] + token.end[1]
            spans.append(Span(start, end))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return spans


def ast_line_table(text: str) -> list[int]:
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line.encode("utf-8")))
    return offsets


def ast_docstring_spans(text: str, tree: ast.Module, policy: Policy) -> list[Span]:
    offsets = ast_line_table(text)
    encoded = text.encode("utf-8")

    def pos(lineno: int, col: int) -> int:
        return offsets[lineno - 1] + col

    spans: list[Span] = []
    for node in ast.walk(tree):
        is_module = isinstance(node, ast.Module)
        if not is_module and not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if not isinstance(first, ast.Expr) or not isinstance(first.value, ast.Constant):
            continue
        if not isinstance(first.value.value, str):
            continue
        if is_module and policy.keep_module_docstring:
            continue
        start = pos(first.lineno, first.col_offset)
        end = pos(first.end_lineno or first.lineno, first.end_col_offset or 0)
        # expand over the whole line so the removal does not leave blank lines
        line_start = encoded.rfind(b"\n", 0, start) + 1
        nl = encoded.find(b"\n", end)
        line_end = len(encoded) if nl == -1 else nl
        one_line = b"\n" not in encoded[start:end]
        whole_line = not encoded[line_start:start].strip() and not encoded[end:line_end].strip()
        if one_line and whole_line:
            span_start = line_start
        else:
            span_start = start
        spans.append(
            Span(span_start, end, "docstring-only" if len(body) == 1 else "docstring")
        )
    return spans


def ast_annotation_spans(text: str, tree: ast.Module) -> list[Span]:
    offsets = ast_line_table(text)
    encoded = text.encode("utf-8")

    def pos(lineno: int, col: int) -> int:
        return offsets[lineno - 1] + col

    spans: list[Span] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.returns is not None:
                end = pos(node.returns.end_lineno or node.returns.lineno, node.returns.end_col_offset or 0)
                start = pos(node.returns.lineno, node.returns.col_offset)
                begin = _find_back(encoded, start, b"->")
                if begin >= 0:
                    while begin > 0 and encoded[begin - 1 : begin] in b" \t":
                        begin -= 1
                    while end < len(encoded) and encoded[end : end + 1] in b" \t":
                        end += 1
                    spans.append(Span(begin, end, "annotation"))
            arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            if node.args.vararg:
                arguments.append(node.args.vararg)
            if node.args.kwarg:
                arguments.append(node.args.kwarg)
            for argument in arguments:
                if argument.annotation is None:
                    continue
                start = pos(argument.annotation.lineno, argument.annotation.col_offset)
                end = pos(
                    argument.annotation.end_lineno or argument.annotation.lineno,
                    argument.annotation.end_col_offset or 0,
                )
                colon = _find_back(encoded, start, b":")
                if colon < 0:
                    continue
                tail = re.match(rb"[ \t]*=[ \t]*", encoded[end:])
                if tail:
                    spans.append(Span(colon, end + tail.end(), "annotation"))
                else:
                    spans.append(Span(colon, end, "annotation"))
        elif isinstance(node, ast.AnnAssign):
            target_end = pos(node.target.end_lineno or node.target.lineno, node.target.end_col_offset or 0)
            if node.value is None:
                line_start = encoded.rfind(b"\n", 0, pos(node.lineno, node.col_offset)) + 1
                nl = encoded.find(b"\n", pos(node.annotation.end_lineno or node.lineno,
                                             node.annotation.end_col_offset or 0))
                spans.append(Span(line_start, len(encoded) if nl == -1 else nl + 1, "docstring"))
            else:
                value_start = pos(node.value.lineno, node.value.col_offset)
                spans.append(Span(target_end, value_start, "annotation"))
    return spans


def _find_back(encoded: bytes, start: int, needle: bytes) -> int:
    index = start - 1
    while index >= 0 and encoded[index : index + 1] in b" \t\n\r":
        index -= 1
    for size in range(len(needle), 0, -1):
        if encoded[index - size + 1 : index + 1] == needle[:size]:
            return index - size + 1
    return -1


class _Unparser(ast.NodeTransformer):
    """Drop docstrings and annotations for --ast-mode unparse."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def _clean_body(self, body: list[ast.stmt]) -> list[ast.stmt]:
        out: list[ast.stmt] = []
        for index, statement in enumerate(body):
            if (
                index == 0
                and self.policy.docstrings
                and isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            ):
                continue
            out.append(statement)
        return out or [ast.Pass()]

    def visit_Module(self, node: ast.Module) -> ast.Module:
        if self.policy.docstrings and not self.policy.keep_module_docstring:
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]
        return self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        node.body = self._clean_body(node.body) if self.policy.docstrings else node.body
        if self.policy.annotations:
            node.returns = None
            for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                argument.annotation = None
            for argument in (node.args.vararg, node.args.kwarg):
                if argument is not None:
                    argument.annotation = None
        return node

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef:
        node.body = self._clean_body(node.body) if self.policy.docstrings else node.body
        return node

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.stmt:
        if not self.policy.annotations:
            return node
        if node.value is None:
            return ast.Pass()
        return ast.Assign(targets=[node.target], value=node.value, lineno=node.lineno)


def strip_ast(text: str, policy: Policy, mode: str) -> tuple[str, dict[str, int], str | None]:
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return text, {}, f"syntax error: {exc.msg} (line {exc.lineno})"
    counts = {"comments": 0, "docstrings": 0, "annotations": 0}
    if mode == "unparse":
        if policy.comments:
            counts["comments"] = len(ast_comment_spans(text, policy))
            if not policy.docstrings and not policy.annotations:
                return strip_ast_splice(text, tree, policy, counts)
        if policy.docstrings:
            counts["docstrings"] = len(ast_docstring_spans(text, tree, policy))
        if policy.annotations:
            counts["annotations"] = len(ast_annotation_spans(text, tree))
        transformed = _Unparser(policy).visit(copy.deepcopy(tree))
        ast.fix_missing_locations(transformed)
        try:
            new_text = ast.unparse(transformed) + "\n"
        except Exception as exc:  # noqa: BLE001
            return text, counts, f"ast.unparse failed: {type(exc).__name__}: {exc}"
        return new_text, counts, None
    return strip_ast_splice(text, tree, policy, counts)


def strip_ast_splice(text: str, tree: ast.Module, policy: Policy, counts: dict[str, int]) -> tuple[str, dict[str, int], str | None]:
    spans: list[Span] = []
    if policy.comments:
        found = ast_comment_spans(text, policy)
        counts["comments"] = len(found)
        spans.extend(found)
    if policy.docstrings:
        found = ast_docstring_spans(text, tree, policy)
        counts["docstrings"] = len(found)
        spans.extend(found)
    if policy.annotations:
        found = ast_annotation_spans(text, tree)
        counts["annotations"] = len(found)
        spans.extend(found)
    if not spans:
        return text, counts, None
    new_text = apply_spans(text, spans, policy)
    problem = verify(text, new_text, policy)
    if problem:
        return text, counts, problem
    return new_text, counts, None


# --------------------------------------------------------------------------- #
# verification                                                                #
# --------------------------------------------------------------------------- #


def blank_annotations(tree: ast.AST) -> ast.AST:
    class Blanker(ast.NodeTransformer):
        def visit_FunctionDef(self, node):
            node.returns = None
            for argument in (
                *node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                node.args.vararg, node.args.kwarg,
            ):
                if argument is not None:
                    argument.annotation = None
            return self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_arg(self, node):
            node.annotation = None
            return node

        def visit_AnnAssign(self, node):
            if node.value is None:
                return None
            return ast.copy_location(
                ast.Assign(targets=[node.target], value=self.visit(node.value)), node
            )

    clone = Blanker().visit(copy.deepcopy(tree))
    return ast.fix_missing_locations(clone)


def blank_docstrings(tree: ast.AST, skip_module: bool) -> ast.AST:
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        if skip_module and isinstance(node, ast.Module):
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            if len(body) == 1:
                body[0] = ast.Pass()
            else:
                del body[0]
    return tree


def canonical(text: str, policy: Policy) -> str:
    tree = ast.parse(text)
    if policy.annotations:
        tree = blank_annotations(tree)
    if policy.docstrings:
        tree = blank_docstrings(tree, policy.keep_module_docstring)
    return ast.dump(ast.fix_missing_locations(tree), include_attributes=False)


def verify(old: str, new: str, policy: Policy) -> str | None:
    if not policy.verify:
        return None
    try:
        compile(new, "<rmc>", "exec")
    except SyntaxError as exc:
        return f"output does not compile: {exc.msg} (line {exc.lineno})"
    try:
        if canonical(old, policy) != canonical(new, policy):
            return "structure changed beyond the requested removals"
    except SyntaxError:
        return None
    return None


# --------------------------------------------------------------------------- #
# driver                                                                      #
# --------------------------------------------------------------------------- #

ENGINES = {
    "tree-sitter": lambda text, policy, mode: strip_tree_sitter(text, policy),
    "libcst": lambda text, policy, mode: strip_libcst(text, policy),
    "ast": lambda text, policy, mode: strip_ast(text, policy, mode),
}


def choose_engine(requested: str) -> str:
    if requested != "auto":
        return requested
    if python_parser() is not None:
        return "tree-sitter"
    if probe("libcst") is not None:
        return "libcst"
    return "ast"


def process_file(raw_path: str, policy: Policy, engine: str, mode: str, out: Output) -> Result:
    started = time.perf_counter()
    path = Path(raw_path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return Result(raw_path, "error", detail=str(exc))
    if b"\x00" in raw[:4096]:
        return Result(raw_path, "skipped", detail="binary")
    text, encoding = decode(raw, policy.fallback_encoding)
    try:
        compile(text, str(path), "exec")
    except SyntaxError as exc:
        return Result(raw_path, "skipped", detail=f"does not parse: line {exc.lineno}")
    chosen = choose_engine(engine)
    new_text, counts, problem = ENGINES[chosen](text, policy, mode)
    if problem:
        status = "rejected" if problem.startswith(("output", "structure")) else "error"
        if problem == "source does not parse":
            status = "skipped"
        return Result(raw_path, status, chosen, len(raw), len(raw), detail=problem)
    result = Result(
        raw_path, "unchanged", chosen, len(raw), len(raw),
        comments=counts.get("comments", 0), docstrings=counts.get("docstrings", 0),
        annotations=counts.get("annotations", 0),
    )
    if new_text == text:
        result.comments = result.docstrings = result.annotations = 0
        result.elapsed = time.perf_counter() - started
        return result
    encoded = new_text.encode(encoding if encoding != "utf-8-sig" else "utf-8")
    if encoding == "utf-8-sig":
        encoded = b"\xef\xbb\xbf" + encoded
    result.after = len(encoded)
    result.status = "changed"
    if out.diff:
        result.diff = "".join(
            difflib.unified_diff(
                text.splitlines(keepends=True), new_text.splitlines(keepends=True),
                fromfile=f"a/{raw_path}", tofile=f"b/{raw_path}",
            )
        )
    if not out.dry_run:
        if out.backup:
            make_backup(path, out)
        atomic_write(path, encoded)
    result.elapsed = time.perf_counter() - started
    return result


def _worker(args: tuple[str, Policy, str, str, Output]) -> Result:
    return process_file(*args)


# --------------------------------------------------------------------------- #
# traversal                                                                   #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Walk:
    recursive: bool = True
    max_depth: int = 0
    skip_dirs: frozenset[str] = DEFAULT_SKIP_DIRS
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_bytes: int = 0
    max_lines: int = 0


def collect_targets(paths: Sequence[Path], walk: Walk) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []

    def wanted(path: Path) -> bool:
        if path.suffix.lower() not in (".py", ".pyi", ".pyw"):
            return False
        if walk.include and not any(fnmatch.fnmatch(path.name, pattern) for pattern in walk.include):
            return False
        if any(fnmatch.fnmatch(path.name, p) or fnmatch.fnmatch(str(path), p) for p in walk.exclude):
            return False
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if walk.max_bytes and size > walk.max_bytes:
            return False
        return True

    def add(path: Path) -> None:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key not in seen:
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
                depth = len(Path(root).relative_to(path).parts)
                if walk.max_depth and depth >= walk.max_depth:
                    dirs[:] = []
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


def add_policy_flags(parser: argparse.ArgumentParser) -> None:
    what = parser.add_argument_group("what to remove")
    what.add_argument("-c", "--comments", action="store_true", help="remove comments")
    what.add_argument("-d", "--docstrings", action="store_true", help="remove docstrings")
    what.add_argument("-t", "--annotations", action="store_true", help="remove type annotations")
    what.add_argument("-a", "--all", action="store_true", help="comments + docstrings + annotations")
    what.add_argument("--keep-shebang", action=argparse.BooleanOptionalAction, default=True)
    what.add_argument("--keep-module-docstring", action=argparse.BooleanOptionalAction, default=True)
    what.add_argument("--keep-directives", action=argparse.BooleanOptionalAction, default=True,
                      help="keep # noqa / # type: / # fmt: / coding cookies (default: on)")
    what.add_argument("--keep-todo", action="store_true", help="keep TODO/FIXME/XXX comments")
    what.add_argument("--keep-prefix", action="append", default=[], metavar="REGEX")
    what.add_argument("--remove-module-docstring", action="store_true",
                      help="shorthand for --no-keep-module-docstring")
    what.add_argument("--preserve-lines", "--blank", action="store_true", dest="preserve_lines",
                      help="blank out removed text instead of deleting it")
    what.add_argument("--eat-trailing-newline", action="store_true")
    what.add_argument("--remove-blank-lines", action="store_true")
    what.add_argument("--fallback-encoding", default="",
                      help="try this encoding when the file is not valid UTF-8")


def add_common_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("paths", nargs="*", type=Path, help="files/directories (default: .)")
    parser.add_argument("-b", "--backend", choices=("auto", "tree-sitter", "libcst", "ast"), default="auto")
    parser.add_argument("--ast-mode", choices=("splice", "unparse"), default="splice",
                        help="ast engine: splice keeps layout, unparse rewrites the file")
    parser.add_argument("-n", "--dry-run", "--dryrun", action="store_true", dest="dry_run")
    parser.add_argument("--diff", action="store_true", help="print a unified diff (no write)")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 if work is pending")
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--backup-dir", default=None)
    parser.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 1))
    parser.add_argument("--chunksize", type=int, default=8)
    parser.add_argument("--include", action="append", default=[], metavar="GLOB")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    parser.add_argument("--skip-dir", action="append", default=[], metavar="NAME")
    parser.add_argument("--no-recursive", action="store_true")
    parser.add_argument("--max-depth", type=int, default=0)
    parser.add_argument("--max-bytes", type=int, default=0)
    parser.add_argument("--max-lines", type=int, default=0)
    parser.add_argument("--no-verify", action="store_true", help="skip the post-write safety gate")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--json", nargs="?", const="-", default=None, metavar="FILE")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Strip comments, docstrings and annotations from Python files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "was: armc brmc crmc drmc felormc frmc grmc grmcst py_clean pyclean rmc rmc-ts\n"
            "     rmc2 rmc3 rmc_termux rmca rmcco rmcomdocs rmcst rmctools rmcts srmc1 srmc2\n"
            "     tcs tmc tscleaner tsrmc benchrmc  (pycleaner.py -> deadcode.py)\n"
            "\n"
            "examples:\n"
            "  %(prog)s strip -c -d -n --diff .\n"
            "  %(prog)s strip -a -b libcst src/\n"
            "  %(prog)s strip -c --keep-todo --preserve-lines pkg/\n"
            "  %(prog)s check . && %(prog)s bench .\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subs = parser.add_subparsers(dest="command", required=True)

    strip = subs.add_parser("strip", help="remove things (default target: whole tree)")
    add_policy_flags(strip)
    add_common_flags(strip)

    check = subs.add_parser("check", help="report what would be removed")
    add_policy_flags(check)
    add_common_flags(check)

    bench = subs.add_parser("bench", help="time each engine on the same tree")
    add_policy_flags(bench)
    add_common_flags(bench)

    doctor = subs.add_parser("doctor", help="report engine availability")
    doctor.add_argument("paths", nargs="*", type=Path)
    return parser


def policy_from(args: argparse.Namespace) -> Policy:
    everything = args.all
    return Policy(
        comments=args.comments or everything,
        docstrings=args.docstrings or everything,
        annotations=args.annotations or everything,
        keep_shebang=args.keep_shebang,
        keep_module_docstring=args.keep_module_docstring and not args.remove_module_docstring,
        keep_directives=args.keep_directives,
        keep_todo=args.keep_todo,
        keep_prefix=tuple(args.keep_prefix),
        preserve_lines=args.preserve_lines,
        eat_trailing_newline=args.eat_trailing_newline,
        remove_blank_lines=args.remove_blank_lines,
        verify=not args.no_verify,
        fallback_encoding=args.fallback_encoding,
    )


def print_doctor() -> int:
    print(f"{PROG} {__version__}  python {sys.version.split()[0]}\n")
    parser = python_parser()
    print(f"{'engine':<14}{'status':<10}notes")
    print(f"{'tree-sitter':<14}{'ok' if parser else 'MISSING':<10}"
          f"{'pip install tree-sitter tree-sitter-python' if not parser else 'byte precise spans'}")
    libcst = probe("libcst")
    print(f"{'libcst':<14}{'ok' if libcst else 'MISSING':<10}"
          f"{'pip install libcst' if not libcst else 'exact whitespace round-trip'}")
    print(f"{'ast':<14}{'ok':<10}stdlib; --ast-mode splice keeps comments, unparse rewrites")
    if sys.version_info < MIN_PY:
        print(f"\nwarning: {PROG} needs Python {MIN_PY[0]}.{MIN_PY[1]}+")
    return 0


def run_check(results: Sequence[Result], quiet: bool) -> None:
    for result in results:
        if result.status in ("changed",) and not quiet:
            print(
                f"{result.path}  comments={result.comments} docstrings={result.docstrings} "
                f"annotations={result.annotations}  ({result.backend})"
            )
        elif result.status in ("error", "rejected"):
            print(f"{result.path}  {result.status}: {result.detail}", file=sys.stderr)


def run_bench(targets: Sequence[Path], policy: Policy, mode: str) -> int:
    engines = [name for name in ("tree-sitter", "libcst", "ast") if name != "tree-sitter" or python_parser()]
    if not engines:
        print("no engine available", file=sys.stderr)
        return 2
    print(f"{len(targets)} file(s), {len(engines)} engine(s)\n")
    print(f"{'engine':<14}{'ok':>6}{'rej':>6}{'s':>10}")
    for engine in engines:
        ok = rejected = 0
        started = time.perf_counter()
        for target in targets:
            result = process_file(str(target), policy, engine, mode, Output(dry_run=True))
            if result.status in ("changed", "unchanged"):
                ok += 1
            else:
                rejected += 1
        print(f"{engine:<14}{ok:>6}{rejected:>6}{time.perf_counter() - started:>10.3f}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "doctor":
        return print_doctor()

    policy = policy_from(args)
    if not policy.anything:
        parser.error("choose at least one of -c/--comments, -d/--docstrings, -t/--annotations, -a/--all")

    roots = [p.expanduser() for p in args.paths] or [Path.cwd()]
    walk = Walk(
        recursive=not args.no_recursive,
        max_depth=args.max_depth,
        skip_dirs=DEFAULT_SKIP_DIRS | frozenset(args.skip_dir),
        include=tuple(args.include),
        exclude=tuple(args.exclude),
        max_bytes=args.max_bytes,
        max_lines=args.max_lines,
    )
    targets = collect_targets(roots, walk)
    if not targets:
        if not args.quiet:
            print("no Python files found", file=sys.stderr)
        return 0

    if args.command == "bench":
        return run_bench(targets, policy, args.ast_mode)

    out = Output(
        dry_run=args.dry_run or args.diff or args.check or args.command == "check",
        diff=args.diff,
        backup=args.backup,
        backup_dir=args.backup_dir,
    )
    started = time.perf_counter()
    engine = args.backend
    results: list[Result] = []
    if args.jobs > 1 and len(targets) > 4:
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            jobs = [(str(t), policy, engine, args.ast_mode, out) for t in targets]
            results = list(pool.map(_worker, jobs, chunksize=max(1, min(args.chunksize, len(jobs)))))
    else:
        results = [process_file(str(t), policy, engine, args.ast_mode, out) for t in targets]

    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
        if args.quiet:
            continue
        if result.status == "changed":
            print(
                f"{result.path}  −{result.comments}c −{result.docstrings}d −{result.annotations}a  "
                f"{result.before}→{result.after} B  [{result.backend}]"
            )
            if result.diff:
                sys.stdout.write(result.diff)
        elif result.status in ("error", "rejected"):
            print(f"{result.path}  {result.status}: {result.detail}", file=sys.stderr)
        elif args.verbose:
            print(f"{result.path}  {result.status}" + (f"  ({result.detail})" if result.detail else ""))

    elapsed = time.perf_counter() - started
    totals = {
        key: sum(getattr(r, key) for r in results)
        for key in ("comments", "docstrings", "annotations")
    }
    saved = sum(max(0, r.before - r.after) for r in results)
    if not args.quiet:
        print(
            f"\n{counts.get('changed', 0)} changed · {counts.get('unchanged', 0)} unchanged · "
            f"{counts.get('skipped', 0)} skipped · {counts.get('rejected', 0)} rejected · "
            f"{counts.get('error', 0)} errors · "
            f"−{totals['comments']}c −{totals['docstrings']}d −{totals['annotations']}a · "
            f"{saved} B · {elapsed:.2f}s",
            file=sys.stderr,
        )
    if args.json:
        payload = {
            "tool": PROG, "version": __version__, "command": args.command,
            "elapsed": round(elapsed, 3), "counts": counts, "removed": totals, "bytes_saved": saved,
            "files": [
                {
                    "path": r.path, "status": r.status, "backend": r.backend,
                    "comments": r.comments, "docstrings": r.docstrings, "annotations": r.annotations,
                    "before": r.before, "after": r.after, "detail": r.detail,
                }
                for r in results
            ],
        }
        text = json.dumps(payload, indent=2)
        if args.json == "-":
            print(text)
        else:
            Path(args.json).write_text(text, encoding="utf-8")
    if counts.get("error"):
        return 1
    if (args.check or args.command == "check") and counts.get("changed"):
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
