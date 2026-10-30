#!/usr/bin/env python3
"""comments.py — remove comments from source files, in 22 languages, safely.

Consolidates g001's nineteen scripts (clean-*.py, clean*.py, cccpp.py,
cleants*.py, uncomment.py) into one program with pluggable backends:

    tree-sitter   precise, syntax aware, language agnostic (needs grammars)
    regex         built-in mini-lexer, no dependencies, best effort
    libcst        Python only; also understands docstrings
    minify        rjsmin/jsmin, csscompressor, htmlmin: drops whitespace too

Every write is gated: the result is re-parsed and its leaf-token signature is
compared with the input's (comments ignored, removed docstrings ignored,
docstrings replaced by ``pass`` normalised).  If they differ the file is left
untouched and reported as ``rejected``.

Exit codes: 0 ok · 1 errors or --check found work · 2 usage · 130 interrupted
"""

from __future__ import annotations

import argparse
import ast
import difflib
import fnmatch
import importlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

__version__ = "1.0.0"
PROG = "comments.py"

# --------------------------------------------------------------------------- #
# optional dependencies                                                       #
# --------------------------------------------------------------------------- #

_PROBE_CACHE: dict[str, object | None] = {}

INSTALL_HINTS: dict[str, str] = {
    "tree_sitter": "pip install tree-sitter",
    "libcst": "pip install libcst",
    "rjsmin": "pip install rjsmin",
    "jsmin": "pip install jsmin",
    "csscompressor": "pip install csscompressor",
    "htmlmin": "pip install htmlmin",
}


def probe(name: str) -> object | None:
    """Import ``name`` once; return None when it is absent or broken."""
    if name not in _PROBE_CACHE:
        module: object | None = None
        try:
            if importlib.util.find_spec(name) is not None:
                module = importlib.import_module(name)
        except Exception:  # noqa: BLE001 - a broken optional dep must not kill us
            module = None
        _PROBE_CACHE[name] = module
    return _PROBE_CACHE[name]


DEFAULT_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git", ".hg", ".svn", ".idea", ".vscode", ".venv", "venv", "env",
        "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", ".tox", ".nox", ".cache", "target", "dist", "build",
        "site-packages", ".eggs",
    }
)

# --------------------------------------------------------------------------- #
# preservation policy                                                         #
# --------------------------------------------------------------------------- #

# Union of the heuristics found across the nineteen originals (cleants.VITAL_RE,
# uncomment._COMMON/_C_FAMILY/_JS_FAMILY/_SHELL_FAMILY, cleanpy's # fmt/# type,
# clean-rs' SAFETY:, cccpp's #pragma, ...).  Comments matching these are kept
# unless --all is passed, because deleting them changes tool or legal behaviour.
VITAL_RE = re.compile(
    rb"copyright|\(c\)\s*\d{4}|spdx-|all rights reserved|permission is hereby granted"
    rb"|licen[cs]ed (?:under|to)|@license|@preserve|@generated|code generated"
    rb"|auto-?generated|do not (?:remove|delete|edit|modify)"
    rb"|noqa|nosec|nolint|nosonar|pyright:|pylint:|mypy:|type:\s*ignore"
    rb"|#\s*(?:type|fmt):|coding[:=]\s*[-.\w]+|-\\*-"
    rb"|clang-format|clang-tidy|coverity|fallthrough|-Wno-"
    rb"|eslint|tslint|jshint|biome-ignore|prettier-ignore|istanbul|c8 ignore"
    rb"|v8 ignore|sourceMappingURL|sourceURL|@cc_on|@ts-|@jsx|@flow"
    rb"|webpack(?:Chunk|Mode|Prefetch|Preload)|[@#]__(?:PURE|NO_SIDE_EFFECTS)__"
    rb"|\A\s*/\*!|\A#!|shellcheck|shfmt|//go:|go:build|\+build|\bpragma\b|SAFETY\s*:"
    rb"|vim?:|\[if\b|(?<![A-Za-z])region\b|(?<![A-Za-z])endregion\b",
    re.IGNORECASE,
)

DOC_PREFIXES: tuple[bytes, ...] = (b"/**", b"///", b"//!", b"/*!")


@dataclass(frozen=True, slots=True)
class Policy:
    """What may be removed, and what must survive."""

    keep_shebang: bool = True
    keep_vital: bool = True
    keep_doc: bool = False
    keep_regex: tuple[str, ...] = ()
    docstrings: bool = False
    module_docstring: bool = False
    preserve_lines: bool = False
    collapse_blank_lines: bool = False
    verify: str = "tokens"  # none | parse | tokens

    def compiler(self) -> tuple[re.Pattern[bytes], ...]:
        return tuple(re.compile(p.encode()) for p in self.keep_regex)


def is_protected(
    text: bytes,
    policy: Policy,
    extra: Sequence[re.Pattern[bytes]],
    first: bool,
) -> bool:
    if first and policy.keep_shebang and text.startswith(b"#!"):
        return True
    if policy.keep_doc and text.startswith(DOC_PREFIXES) and text not in (b"/**/", b"/*!*/"):
        return True
    if policy.keep_vital and VITAL_RE.search(text):
        return True
    return any(pattern.search(text) for pattern in extra)


# --------------------------------------------------------------------------- #
# language registry                                                           #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Quote:
    """A string literal form, for the dependency-free lexer."""

    start: re.Pattern[bytes]
    opener: bytes
    closer: bytes
    multiline: bool = False
    escape: bool = True
    prefixes: bytes = b""


def _quote(open_text: str, multiline: bool = False, escape: bool = True) -> Quote:
    return Quote(re.compile(re.escape(open_text).encode()), open_text.encode(), open_text.encode(), multiline, escape)


PY_STRING = re.compile(rb"(?<![A-Za-z0-9_])(?P<pre>[rRbBuUfF]{0,2})(?P<o>'''|\"\"\"|'|\")")

PY_QUOTES = (
    Quote(PY_STRING, b'"""', b'"""', True, True, b"rRbBuUfF"),
    Quote(PY_STRING, b"'''", b"'''", True, True, b"rRbBuUfF"),
    Quote(PY_STRING, b'"', b'"', False, True, b"rRbBuUfF"),
    Quote(PY_STRING, b"'", b"'", False, True, b"rRbBuUfF"),
)

C_QUOTES = (_quote('"'), _quote("'"))
JS_QUOTES = (_quote('"'), _quote("'"), _quote("`", multiline=True))


@dataclass(frozen=True, slots=True)
class Language:
    name: str
    suffixes: tuple[str, ...]
    ts_module: str | None = None
    ts_attr: str = "language"
    comment_types: frozenset[str] = frozenset({"comment"})
    # regex fallback
    line: tuple[str, ...] = ()
    line_needs_space: bool = False  # '#' only starts a comment after whitespace
    block: tuple[tuple[str, str], ...] = ()
    nested_blocks: bool = False
    quotes: tuple[Quote, ...] = ()
    lexable: bool = True
    # extras
    docstrings: bool = False
    interpreters: tuple[str, ...] = ()


LANGUAGES: tuple[Language, ...] = (
    Language(
        "python", (".py", ".pyw", ".pyi"), "tree_sitter_python",
        line=("#",), quotes=PY_QUOTES, docstrings=True, interpreters=("python",),
    ),
    Language(
        "c", (".c", ".h"), "tree_sitter_c",
        line=("//",), block=(("/*", "*/"),), quotes=C_QUOTES,
    ),
    Language(
        "cpp", (".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx", ".ino"), "tree_sitter_cpp",
        line=("//",), block=(("/*", "*/"),), quotes=C_QUOTES,
    ),
    Language(
        "bash", (".sh", ".bash", ".zsh", ".ksh", ".bats"), "tree_sitter_bash",
        line=("#",), line_needs_space=True, quotes=C_QUOTES,
        interpreters=("bash", "sh", "zsh", "dash", "ksh", "fish"),
    ),
    Language(
        "css", (".css", ".scss", ".sass", ".less"), "tree_sitter_css",
        block=(("/*", "*/"),), quotes=C_QUOTES,
    ),
    Language(
        "html", (".html", ".htm", ".xhtml"), "tree_sitter_html",
        block=(("<!--", "-->"),), quotes=C_QUOTES,
    ),
    Language(
        "javascript", (".js", ".mjs", ".cjs", ".jsx"), "tree_sitter_javascript",
        line=("//",), block=(("/*", "*/"),), quotes=JS_QUOTES,
        interpreters=("node", "nodejs", "deno", "bun"),
    ),
    Language(
        "typescript", (".ts", ".mts", ".cts"), "tree_sitter_typescript",
        ts_attr="language_typescript",
        line=("//",), block=(("/*", "*/"),), quotes=JS_QUOTES, interpreters=("ts-node",),
    ),
    Language(
        "tsx", (".tsx",), "tree_sitter_typescript", ts_attr="language_tsx",
        line=("//",), block=(("/*", "*/"),), quotes=JS_QUOTES, lexable=False,
    ),
    Language(
        "lua", (".lua",), "tree_sitter_lua",
        line=("--",), block=(("--[[", "]]"),), quotes=C_QUOTES,
        interpreters=("lua", "luajit"),
    ),
    Language(
        "rust", (".rs",), "tree_sitter_rust",
        comment_types=frozenset({"line_comment", "block_comment"}),
        line=("//",), block=(("/*", "*/"),), nested_blocks=True, quotes=C_QUOTES,
        lexable=False,  # char literals vs. lifetimes ('a) are ambiguous without a parser
    ),
    Language(
        "toml", (".toml",), "tree_sitter_toml",
        line=("#",), quotes=C_QUOTES,
    ),
    Language(
        "vim", (".vim",), "tree_sitter_vim",
        line=('"',), quotes=C_QUOTES, lexable=False,
    ),
    Language(
        "go", (".go",), "tree_sitter_go",
        line=("//",), block=(("/*", "*/"),), quotes=JS_QUOTES,
    ),
    Language(
        "java", (".java",), "tree_sitter_java",
        comment_types=frozenset({"line_comment", "block_comment"}),
        line=("//",), block=(("/*", "*/"),), quotes=C_QUOTES,
    ),
    Language(
        "ruby", (".rb", ".rake", ".gemspec"), "tree_sitter_ruby",
        line=("#",), quotes=C_QUOTES, interpreters=("ruby",),
    ),
    Language(
        "perl", (".pl", ".pm"), "tree_sitter_perl",
        line=("#",), quotes=C_QUOTES, interpreters=("perl",),
    ),
    Language(
        "php", (".php",), "tree_sitter_php", ts_attr="language_php",
        line=("//", "#"), block=(("/*", "*/"),), quotes=C_QUOTES, interpreters=("php",),
    ),
    Language(
        "sql", (".sql",), "tree_sitter_sql",
        line=("--",), block=(("/*", "*/"),), quotes=C_QUOTES,
    ),
    Language(
        "yaml", (".yaml", ".yml"), "tree_sitter_yaml",
        line=("#",), line_needs_space=True, quotes=C_QUOTES,
    ),
    Language(
        "xml", (".xml", ".svg", ".xsl", ".xsd", ".plist"), "tree_sitter_xml",
        ts_attr="language_xml",
        comment_types=frozenset({"Comment", "comment"}),
        block=(("<!--", "-->"),), quotes=C_QUOTES,
    ),
    Language(
        "latex", (".tex", ".sty", ".cls", ".bib"), "tree_sitter_latex",
        line=("%",),
    ),
)

BY_NAME: dict[str, Language] = {lang.name: lang for lang in LANGUAGES}
BY_SUFFIX: dict[str, Language] = {suffix: lang for lang in LANGUAGES for suffix in lang.suffixes}

_STARTERS: dict[str, frozenset[int]] = {}


def starters_for(lang: Language) -> frozenset[int]:
    cached = _STARTERS.get(lang.name)
    if cached is None:
        chars: set[int] = set()
        for prefix in lang.line:
            chars.update(prefix.encode())
        for opener, _ in lang.block:
            chars.update(opener.encode())
        for quote in lang.quotes:
            chars.update(quote.opener)
            chars.update(quote.prefixes)
        cached = frozenset(chars)
        _STARTERS[lang.name] = cached
    return cached


def detect_language(path: Path, head: bytes) -> Language | None:
    lang = BY_SUFFIX.get(path.suffix.lower())
    if lang is not None:
        return lang
    if not head.startswith(b"#!"):
        return None
    first = head.split(b"\n", 1)[0].decode("utf-8", "replace").rstrip("\r")
    try:
        argv = shlex.split(first[2:])
    except ValueError:
        argv = first[2:].split()
    if not argv:
        return None
    interpreter = PurePosixPath(argv[0]).name
    if interpreter == "env":
        rest = [a for a in argv[1:] if not a.startswith("-") and "=" not in a]
        if not rest:
            return None
        interpreter = PurePosixPath(rest[0]).name
    interpreter = re.sub(r"[\d.]+$", "", interpreter)
    for lang in LANGUAGES:
        if interpreter in lang.interpreters:
            return lang
    return None


# --------------------------------------------------------------------------- #
# spans and edits                                                             #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Span:
    start: int
    end: int
    kind: str = "comment"  # comment | docstring | docstring-only


@dataclass(frozen=True, slots=True)
class Edit:
    start: int
    end: int
    repl: bytes = b""


def merge_spans(spans: Iterable[Span]) -> list[Span]:
    ordered = sorted(spans, key=lambda s: (s.start, s.end))
    merged: list[Span] = []
    for span in ordered:
        if merged and span.start <= merged[-1].end:
            previous = merged[-1]
            if span.end > previous.end:
                merged[-1] = Span(previous.start, span.end, previous.kind)
        else:
            merged.append(span)
    return merged


def apply_edits(src: bytes, edits: Sequence[Edit]) -> bytes:
    out = bytearray()
    pos = 0
    for edit in edits:
        if edit.start < pos:
            raise ValueError("overlapping edits")
        out += src[pos : edit.start]
        out += edit.repl
        pos = edit.end
    out += src[pos:]
    return bytes(out)


def line_aware_edits(src: bytes, spans: Sequence[Span], policy: Policy) -> list[Edit]:
    """Turn spans into edits, deleting now-empty lines entirely."""
    edits: list[Edit] = []
    for span in merge_spans(spans):
        if span.kind.startswith("docstring"):
            if span.kind == "docstring-only":
                edits.append(Edit(span.start, span.end, b"pass"))
            else:
                line_start = src.rfind(b"\n", 0, span.start) + 1
                newline = src.find(b"\n", span.end)
                line_end = len(src) if newline == -1 else newline + 1
                edits.append(Edit(line_start, line_end, b""))
            continue
        line_start = src.rfind(b"\n", 0, span.start) + 1
        newline = src.find(b"\n", span.end)
        line_end = len(src) if newline == -1 else newline
        before = src[line_start : span.start]
        after = src[span.end : line_end]
        if policy.preserve_lines:
            edits.append(Edit(span.start, span.end, re.sub(rb"[^\n]", b" ", src[span.start : span.end])))
        elif not before.strip() and not after.strip():
            edits.append(Edit(line_start, min(line_end + 1, len(src)), b""))
        else:
            begin = span.start
            while begin > line_start and src[begin - 1 : begin] in (b" ", b"\t"):
                begin -= 1
            cut = span.end
            while cut < line_end and src[cut : cut + 1] in (b" ", b"\t"):
                cut += 1
            edits.append(Edit(begin, cut, b""))
    return sorted(edits, key=lambda edit: edit.start)


def collapse_blanks(src: bytes) -> bytes:
    src = re.sub(rb"[ \t]+$", b"", src, flags=re.MULTILINE)
    return re.sub(rb"\n{3,}", b"\n\n", src)


# --------------------------------------------------------------------------- #
# tree-sitter helpers                                                         #
# --------------------------------------------------------------------------- #

_PARSERS: dict[str, object | None] = {}
_TS_COMMENT_TYPES = frozenset({"comment", "line_comment", "block_comment", "Comment"})
SKIP_PARSE = "source does not parse: left untouched"


def ts_language(lang: Language) -> object | None:
    if lang.ts_module is None:
        return None
    runtime = probe("tree_sitter")
    module = probe(lang.ts_module)
    if runtime is None or module is None:
        return None
    factory = getattr(module, lang.ts_attr, None)
    if factory is None:
        return None
    try:
        return getattr(runtime, "Language")(factory())
    except Exception:  # noqa: BLE001 - version skew between grammar and runtime
        return None


def ts_parser(lang: Language) -> object | None:
    if lang.name not in _PARSERS:
        grammar = ts_language(lang)
        _PARSERS[lang.name] = None if grammar is None else getattr(probe("tree_sitter"), "Parser")(grammar)
    return _PARSERS[lang.name]


def walk(node: object) -> Iterator[object]:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        if getattr(current, "is_missing", False):
            continue
        if getattr(current, "type", "") in _TS_COMMENT_TYPES:
            continue
        stack.extend(reversed(getattr(current, "children", [])))


def has_error(node: object) -> bool:
    if getattr(node, "is_error", False) or getattr(node, "is_missing", False):
        return True
    return any(has_error(child) for child in getattr(node, "children", []))


def python_docstring_spans(root: object) -> list[Span]:
    """First-statement strings in module / class / function bodies."""
    spans: list[Span] = []
    for node in walk(root):
        node_type = getattr(node, "type", "")
        if node_type == "module":
            body = node
        elif node_type in ("class_definition", "function_definition"):
            body = node.child_by_field_name("body")
        else:
            continue
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


def is_module_docstring(root: object, span: Span) -> bool:
    for child in getattr(root, "named_children", ()):
        if child.start_byte > span.start:
            break
        if child.type == "expression_statement" and child.start_byte == span.start:
            return True
    return False


def signature(
    root: object,
    src: bytes,
    comment_types: frozenset[str],
    ignore: frozenset[tuple[int, int]] = frozenset(),
    substitute: frozenset[tuple[int, int]] = frozenset(),
) -> list[tuple[str, bytes]]:
    """Leaf-token fingerprint: comments dropped, docstrings normalised."""
    out: list[tuple[str, bytes]] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if getattr(node, "is_missing", False):
            continue
        node_type = node.type
        if node_type in comment_types:
            continue
        key = (node.start_byte, node.end_byte)
        if key in ignore:
            continue
        if node_type == "expression_statement" and key in substitute:
            out.append(("pass", b"pass"))
            continue
        if node.child_count == 0:
            out.append((node_type, src[node.start_byte : node.end_byte]))
            continue
        stack.extend(reversed(node.children))
    return out


def verify(
    old_tree: object,
    src: bytes,
    new_src: bytes,
    parser: object,
    lang: Language,
    policy: Policy,
    spans: Sequence[Span],
) -> str | None:
    if policy.verify == "none":
        return None
    new_tree = parser.parse(new_src)
    if has_error(new_tree.root_node) and not has_error(old_tree.root_node):
        return "output introduces parse errors"
    if policy.verify != "tokens":
        return None
    removed_docstrings = frozenset((s.start, s.end) for s in spans if s.kind == "docstring")
    substituted = frozenset((s.start, s.end) for s in spans if s.kind == "docstring-only")
    before = signature(old_tree.root_node, src, lang.comment_types, removed_docstrings, substituted)
    after = signature(new_tree.root_node, new_src, lang.comment_types)
    if before != after:
        return "token stream changed beyond the intended edits"
    return None


# --------------------------------------------------------------------------- #
# backend: tree-sitter                                                        #
# --------------------------------------------------------------------------- #


def strip_tree_sitter(src: bytes, lang: Language, policy: Policy) -> tuple[bytes, int, str | None]:
    parser = ts_parser(lang)
    if parser is None:
        return src, 0, f"grammar {lang.ts_module} not installed"
    tree = parser.parse(src)
    if has_error(tree.root_node):
        return src, 0, SKIP_PARSE
    extra = policy.compiler()
    spans: list[Span] = []
    for node in walk(tree.root_node):
        if getattr(node, "type", "") in lang.comment_types:
            text = src[node.start_byte : node.end_byte]
            if not is_protected(text, policy, extra, node.start_byte == 0):
                spans.append(Span(node.start_byte, node.end_byte))
    if lang.docstrings and policy.docstrings:
        for span in python_docstring_spans(tree.root_node):
            if is_module_docstring(tree.root_node, span) and not policy.module_docstring:
                continue
            spans.append(span)
    if not spans:
        return src, 0, None
    new_src = apply_edits(src, line_aware_edits(src, spans, policy))
    if policy.collapse_blank_lines:
        new_src = collapse_blanks(new_src)
    problem = verify(tree, src, new_src, parser, lang, policy, spans)
    if problem:
        return src, 0, problem
    return new_src, len(spans), None


# --------------------------------------------------------------------------- #
# backend: regex (dependency-free mini lexer)                                 #
# --------------------------------------------------------------------------- #


def _skip_string(src: bytes, pos: int, quote: Quote) -> int:
    """Index just past the closing delimiter (or EOF when unterminated)."""
    total = len(src)
    index = pos
    while index < total:
        if quote.escape and src[index : index + 1] == b"\\":
            index += 2
            continue
        if src.startswith(quote.closer, index):
            return index + len(quote.closer)
        if not quote.multiline and src[index : index + 1] == b"\n":
            return index
        index += 1
    return total


def _block_end(src: bytes, pos: int, closer: bytes, nested: bool) -> int:
    total = len(src)
    if not nested:
        found = src.find(closer, pos)
        return total if found == -1 else found + len(closer)
    depth = 1
    index = pos
    while index < total and depth:
        if src.startswith(closer, index):
            depth -= 1
            index += len(closer)
        else:
            index += 1
    return index


def lex_spans(src: bytes, lang: Language) -> list[Span]:
    spans: list[Span] = []
    total = len(src)
    starters = starters_for(lang)
    index = 0
    while index < total:
        if src[index] not in starters:
            index += 1
            continue
        matched = False
        for quote in lang.quotes:
            found = quote.start.match(src, index)
            if found is not None and len(found.group(0)) >= len(quote.opener):
                index = _skip_string(src, found.end(), quote)
                matched = True
                break
        if matched:
            continue
        for opener, closer in lang.block:
            encoded = opener.encode()
            if src.startswith(encoded, index):
                start = index
                index = _block_end(src, index + len(encoded), closer.encode(), lang.nested_blocks)
                spans.append(Span(start, index))
                matched = True
                break
        if matched:
            continue
        for prefix in lang.line:
            encoded = prefix.encode()
            if not src.startswith(encoded, index):
                continue
            if lang.line_needs_space and index > 0 and src[index - 1 : index] not in (b" ", b"\t", b"\n", b"\r"):
                continue
            end = src.find(b"\n", index)
            if end == -1:
                end = total
            while end > index and src[end - 1 : end] == b"\r":
                end -= 1
            spans.append(Span(index, end))
            index = end
            matched = True
            break
        if not matched:
            index += 1
    return spans


def strip_regex(src: bytes, lang: Language, policy: Policy) -> tuple[bytes, int, str | None]:
    if not lang.lexable:
        return src, 0, f"{lang.name} needs tree-sitter: its quoting is not lexable without a parser"
    extra = policy.compiler()
    raw = lex_spans(src, lang)
    spans = [span for span in raw if not is_protected(src[span.start : span.end], policy, extra, span.start == 0)]
    if lang.docstrings and policy.docstrings:
        for span, is_module in ast_docstring_spans(src):
            if is_module and not policy.module_docstring:
                continue
            spans.append(span)
    if not spans:
        return src, 0, None
    new_src = apply_edits(src, line_aware_edits(src, spans, policy))
    if policy.collapse_blank_lines:
        new_src = collapse_blanks(new_src)
    if policy.verify != "none" and lang.name == "python":
        try:
            ast.parse(new_src)
        except SyntaxError as exc:
            return src, 0, f"output does not parse: {exc.msg} (line {exc.lineno})"
    return new_src, len(spans), None


def ast_docstring_spans(src: bytes) -> list[tuple[Span, bool]]:
    """Docstring spans via the stdlib parser (needs no grammar package).

    ``ast`` column offsets are UTF-8 byte offsets, so they map straight onto the
    byte spans tree-sitter would report.  The boolean marks module docstrings.
    """
    try:
        module = ast.parse(src)
    except (SyntaxError, ValueError):
        return []
    line_starts = [0]
    for line in src.split(b"\n")[:-1]:
        line_starts.append(line_starts[-1] + len(line) + 1)

    def offset(lineno: int, col: int) -> int:
        return line_starts[lineno - 1] + col

    targets: list[tuple[object, bool]] = [(module, True)]
    for node in ast.walk(module):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            targets.append((node, False))

    found: list[tuple[Span, bool]] = []
    for node, is_module in targets:
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if not isinstance(first, ast.Expr) or not isinstance(first.value, ast.Constant):
            continue
        if not isinstance(first.value.value, str):
            continue
        start = offset(first.lineno, first.col_offset)
        end = offset(first.end_lineno or first.lineno, first.end_col_offset or 0)
        found.append((Span(start, end, "docstring-only" if len(body) == 1 else "docstring"), is_module))
    return found


# --------------------------------------------------------------------------- #
# backend: libcst (python)                                                    #
# --------------------------------------------------------------------------- #


def _libcst_strip_first_docstring(cst: object, statements: Sequence[object]) -> list[object] | None:
    """Return the replacement statement list, or None when there is no docstring."""
    if not statements:
        return None
    first = statements[0]
    if not isinstance(first, getattr(cst, "SimpleStatementLine")):
        return None
    body = getattr(first, "body", None)
    if not body or not isinstance(body[0], getattr(cst, "Expr")):
        return None
    if not isinstance(body[0].value, getattr(cst, "SimpleString")):
        return None
    rest = list(statements[1:])
    return rest or [getattr(cst, "SimpleStatementLine")(body=[getattr(cst, "Pass")()])]


def strip_libcst(src: bytes, lang: Language, policy: Policy) -> tuple[bytes, int, str | None]:
    cst = probe("libcst")
    if cst is None:
        return src, 0, "libcst not installed"
    if lang.name != "python":
        return src, 0, "the libcst backend only handles python"
    try:
        text = src.decode("utf-8")
    except UnicodeDecodeError:
        return src, 0, "not valid UTF-8"
    extra = policy.compiler()
    counter = {"comments": 0, "docstrings": 0}
    strip_docstrings = policy.docstrings

    class Transformer(cst.CSTTransformer):  # type: ignore[misc]
        def leave_Comment(self, original_node, updated_node):  # noqa: N802, ANN001
            text_value = original_node.value
            is_shebang = text_value.startswith("#!")
            if is_protected(text_value.encode(), policy, extra, is_shebang):
                return updated_node
            counter["comments"] += 1
            return cst.RemoveFromParent()

        def leave_TrailingWhitespace(self, original_node, updated_node):  # noqa: N802, ANN001
            # a removed comment leaves its separating spaces behind
            if original_node.comment is not None and updated_node.comment is None:
                return updated_node.with_changes(whitespace=cst.SimpleWhitespace(""))
            return updated_node

        def leave_EmptyLine(self, original_node, updated_node):  # noqa: N802, ANN001
            if original_node.comment is not None and updated_node.comment is None:
                return updated_node.with_changes(whitespace=cst.SimpleWhitespace(""))
            return updated_node

        def leave_Module(self, original_node, updated_node):  # noqa: N802, ANN001
            if not (strip_docstrings and policy.module_docstring):
                return updated_node
            replacement = _libcst_strip_first_docstring(cst, updated_node.body)
            if replacement is None:
                return updated_node
            counter["docstrings"] += 1
            return updated_node.with_changes(body=replacement)

        def _maybe_strip(self, updated_node):
            if not strip_docstrings:
                return updated_node
            body = getattr(updated_node, "body", None)
            if not isinstance(body, getattr(cst, "IndentedBlock")):
                return updated_node
            replacement = _libcst_strip_first_docstring(cst, body.body)
            if replacement is None:
                return updated_node
            counter["docstrings"] += 1
            return updated_node.with_changes(body=body.with_changes(body=replacement))

        def leave_FunctionDef(self, original_node, updated_node):  # noqa: N802, ANN001
            return self._maybe_strip(updated_node)

        def leave_ClassDef(self, original_node, updated_node):  # noqa: N802, ANN001
            return self._maybe_strip(updated_node)

    try:
        new_src = cst.parse_module(text).visit(Transformer()).code.encode("utf-8")
    except Exception as exc:  # noqa: BLE001 - libcst rejects some valid Python
        return src, 0, f"libcst: {type(exc).__name__}: {exc}"
    if new_src == src:
        return src, 0, None
    if policy.verify != "none":
        try:
            ast.parse(new_src)
        except SyntaxError as exc:
            return src, 0, f"output does not parse: {exc.msg} (line {exc.lineno})"
    return new_src, counter["comments"] + counter["docstrings"], None


# --------------------------------------------------------------------------- #
# backend: minify (third-party aggressive strippers)                          #
# --------------------------------------------------------------------------- #


def strip_minify(src: bytes, lang: Language, policy: Policy) -> tuple[bytes, int, str | None]:
    del policy
    try:
        text = src.decode("utf-8")
    except UnicodeDecodeError:
        return src, 0, "not valid UTF-8"
    try:
        if lang.name in ("javascript", "tsx", "typescript"):
            module = probe("rjsmin") or probe("jsmin")
            if module is None:
                return src, 0, "rjsmin/jsmin not installed"
            out = module.jsmin(text)
        elif lang.name == "css":
            module = probe("csscompressor")
            if module is None:
                return src, 0, "csscompressor not installed"
            out = module.compress(text)
        elif lang.name == "html":
            module = probe("htmlmin")
            if module is None:
                return src, 0, "htmlmin not installed"
            out = module.minify(text, remove_comments=True)
        else:
            return src, 0, f"no minifier for {lang.name}"
    except Exception as exc:  # noqa: BLE001
        return src, 0, f"minifier failed: {type(exc).__name__}: {exc}"
    return out.encode("utf-8"), 1, None


BACKENDS = {
    "tree-sitter": strip_tree_sitter,
    "regex": strip_regex,
    "libcst": strip_libcst,
    "minify": strip_minify,
}


def choose_backend(lang: Language, requested: str) -> str:
    if requested != "auto":
        return requested
    if ts_parser(lang) is not None:
        return "tree-sitter"
    if lang.name == "python" and probe("libcst") is not None:
        return "libcst"
    if lang.lexable:
        return "regex"
    return "tree-sitter"


def transform(src: bytes, lang: Language, backend: str, policy: Policy) -> tuple[bytes, int, str | None]:
    if backend not in BACKENDS:
        return src, 0, f"unknown backend {backend!r}"
    return BACKENDS[backend](src, lang, policy)


# --------------------------------------------------------------------------- #
# per-file pipeline                                                           #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Config:
    policy: Policy = field(default_factory=Policy)
    backend: str = "auto"
    language: str = "auto"
    max_size: int = 8 * 1024 * 1024
    dry_run: bool = False
    backup: bool = False
    backup_suffix: str = ".bak"
    diff: bool = False


@dataclass(slots=True)
class Result:
    path: str
    status: str
    lang: str = ""
    backend: str = ""
    before: int = 0
    after: int = 0
    removed: int = 0
    detail: str = ""
    diff: str = ""

    @property
    def saved(self) -> int:
        return max(0, self.before - self.after)


def _atomic_write(path: Path, data: bytes) -> None:
    handle_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(data)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _unified(path: Path, before: bytes, after: bytes) -> str:
    return "".join(
        difflib.unified_diff(
            before.decode("utf-8", "replace").splitlines(keepends=True),
            after.decode("utf-8", "replace").splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def load_source(raw_path: str, cfg: Config) -> tuple[Path, bytes, Language] | Result:
    path = Path(raw_path)
    if path.is_symlink():
        return Result(raw_path, "skipped", detail="symlink")
    if not path.is_file():
        return Result(raw_path, "skipped", detail="not a regular file")
    size = path.stat().st_size
    if size > cfg.max_size:
        return Result(raw_path, "skipped", detail=f"larger than --max-size ({size} B)")
    with path.open("rb") as handle:
        head = handle.read(4096)
    if b"\x00" in head:
        return Result(raw_path, "skipped", detail="binary")
    lang = BY_NAME.get(cfg.language) if cfg.language != "auto" else detect_language(path, head)
    if lang is None:
        return Result(raw_path, "skipped", detail="unsupported type")
    return path, path.read_bytes(), lang


def process_path(raw_path: str, cfg: Config) -> Result:
    try:
        loaded = load_source(raw_path, cfg)
        if isinstance(loaded, Result):
            return loaded
        path, src, lang = loaded
        backend = choose_backend(lang, cfg.backend)
        if backend == "tree-sitter" and ts_parser(lang) is None:
            return Result(raw_path, "error", lang.name, backend,
                          detail=f"grammar {lang.ts_module} missing")
        new_src, removed, problem = transform(src, lang, backend, cfg.policy)
        if problem:
            if problem == SKIP_PARSE:
                status = "skipped"
            else:
                status = "rejected" if problem.startswith(("output ", "token ")) else "error"
            return Result(raw_path, status, lang.name, backend, len(src), len(src), 0, problem)
        if new_src == src:
            return Result(raw_path, "unchanged", lang.name, backend, len(src), len(src), 0)
        result = Result(raw_path, "changed", lang.name, backend, len(src), len(new_src), removed)
        if cfg.diff:
            result.diff = _unified(path, src, new_src)
        if not cfg.dry_run:
            if cfg.backup:
                backup_path = path.with_name(path.name + cfg.backup_suffix)
                if not backup_path.exists():
                    shutil.copy2(path, backup_path)
            _atomic_write(path, new_src)
        return result
    except OSError as exc:
        return Result(raw_path, "error", detail=f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        return Result(raw_path, "error", detail=f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# traversal                                                                   #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Walk:
    recursive: bool = True
    max_depth: int = 0  # 0 = unlimited
    skip_dirs: frozenset[str] = DEFAULT_SKIP_DIRS
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    follow_symlinks: bool = False


def collect_targets(paths: Sequence[Path], walk: Walk, language: str) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []

    def wanted(path: Path) -> bool:
        name = path.name
        if walk.include and not any(fnmatch.fnmatch(name, pattern) for pattern in walk.include):
            return False
        if any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(str(path), p) for p in walk.exclude):
            return False
        if language != "auto":
            return True
        if path.suffix.lower() in BY_SUFFIX:
            return True
        if path.suffix:
            return False
        try:  # extensionless: only interesting if it starts with a shebang
            with path.open("rb") as handle:
                return handle.read(2) == b"#!"
        except OSError:
            return False

    def add(path: Path) -> None:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            return
        seen.add(key)
        out.append(path)

    for raw in paths:
        path = raw.expanduser()
        if not path.exists():
            continue
        if path.is_symlink() and not walk.follow_symlinks:
            continue
        if path.is_file():
            if wanted(path):
                add(path)
            continue
        if not path.is_dir():
            continue
        if walk.recursive:
            for root, dirs, files in os.walk(path, followlinks=walk.follow_symlinks):
                depth = len(Path(root).relative_to(path).parts)
                if walk.max_depth and depth >= walk.max_depth:
                    dirs[:] = []
                dirs[:] = sorted(d for d in dirs if d not in walk.skip_dirs and not d.startswith("."))
                for name in sorted(files):
                    candidate = Path(root) / name
                    if candidate.is_symlink() and not walk.follow_symlinks:
                        continue
                    if wanted(candidate):
                        add(candidate)
        else:
            for candidate in sorted(path.iterdir()):
                if candidate.is_file() and wanted(candidate):
                    add(candidate)
    return out


# --------------------------------------------------------------------------- #
# interactive mode (single process)                                           #
# --------------------------------------------------------------------------- #


def interactive(path: Path, cfg: Config) -> Result:
    loaded = load_source(str(path), cfg)
    if isinstance(loaded, Result):
        return loaded
    _, src, lang = loaded
    parser = ts_parser(lang)
    if parser is None:
        return Result(str(path), "error", lang.name, "tree-sitter", detail="interactive mode needs tree-sitter")
    extra = cfg.policy.compiler()
    spans = [
        Span(node.start_byte, node.end_byte)
        for node in walk(parser.parse(src).root_node)
        if node.type in lang.comment_types
        and not is_protected(src[node.start_byte : node.end_byte], cfg.policy, extra, node.start_byte == 0)
    ]
    if not spans:
        return Result(str(path), "unchanged", lang.name, "tree-sitter", len(src), len(src), 0)
    chosen: list[Span] = []
    for index, span in enumerate(spans, 1):
        line = src.count(b"\n", 0, span.start) + 1
        print(f"\n--- {path}:{line} ({index}/{len(spans)}) ---")
        print(src[span.start : span.end].decode("utf-8", "replace"))
        answer = ""
        while answer not in ("y", "yes", "n", "no", "", "q", "quit"):
            answer = input("remove? [y]es / [n]o / [q]uit > ").strip().lower()
        if answer in ("q", "quit"):
            break
        if answer in ("y", "yes"):
            chosen.append(span)
    if not chosen:
        return Result(str(path), "unchanged", lang.name, "tree-sitter", len(src), len(src), 0)
    new_src = apply_edits(src, line_aware_edits(src, chosen, cfg.policy))
    if not cfg.dry_run:
        _atomic_write(path, new_src)
    return Result(str(path), "changed", lang.name, "tree-sitter", len(src), len(new_src), len(chosen))


# --------------------------------------------------------------------------- #
# reporting                                                                   #
# --------------------------------------------------------------------------- #


class Palette:
    def __init__(self, mode: str) -> None:
        self.enabled = (mode == "always" or (mode == "auto" and sys.stdout.isatty())) and os.environ.get("NO_COLOR") is None

    def paint(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text


def human(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(size) < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def emit(result: Result, palette: Palette, verbose: bool) -> None:
    if result.status == "changed":
        print(
            f"{palette.paint(result.path, '1')}  {palette.paint('changed', '32')}  "
            f"{result.lang}/{result.backend}  {human(result.before)} → {human(result.after)}  "
            f"−{result.removed} item(s)"
        )
        if result.diff:
            sys.stdout.write(result.diff)
    elif result.status == "unchanged":
        if verbose:
            print(f"{palette.paint(result.path, '2')}  unchanged")
    elif result.status == "skipped":
        if verbose:
            print(f"{palette.paint(result.path, '2')}  skipped: {result.detail}")
    elif result.status == "rejected":
        print(f"{palette.paint(result.path, '33')}  rejected: {result.detail}", file=sys.stderr)
    else:
        print(f"{palette.paint(result.path, '31')}  error: {result.detail}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Remove comments from source files (consolidates g001).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "backends:\n"
            "  tree-sitter  syntax aware, 22 languages, needs a grammar package\n"
            "  regex        built in, zero dependencies, best effort\n"
            "  libcst       python only, also removes docstrings\n"
            "  minify       rjsmin/jsmin, csscompressor, htmlmin (drops whitespace too)\n"
            "\n"
            "was: cccpp, clean-bash, clean-cpp, clean-css, clean-html, clean-js, clean-lua,\n"
            "     clean-rs, clean-toml, clean-ts, cleanbash, cleanjsts, cleanlua, cleanpy,\n"
            "     cleanrs, cleants, cleants2, cleanvim, uncomment\n"
            "\n"
            "examples:\n"
            "  %(prog)s -n --diff .\n"
            "  %(prog)s -b regex --docstrings -j 8 src/\n"
            "  %(prog)s -l rust --keep-doc --backup vendor/\n"
        ),
    )
    parser.add_argument("paths", nargs="*", type=Path, help="files and/or directories (default: .)")
    parser.add_argument("-l", "--language", default="auto", choices=("auto", *sorted(BY_NAME)),
                        help="force a language instead of guessing from suffix/shebang")
    parser.add_argument("-b", "--backend", default="auto", choices=("auto", *sorted(BACKENDS)),
                        help="stripping engine (default: auto)")
    parser.add_argument("--list-languages", action="store_true", help="print the language table and exit")
    parser.add_argument("--doctor", action="store_true", help="report which backends are usable")

    what = parser.add_argument_group("what to remove")
    what.add_argument("--docstrings", action="store_true", help="python: remove docstrings too")
    what.add_argument("--module-docstring", action="store_true",
                      help="python: also remove the module docstring (implies --docstrings)")
    what.add_argument("--keep-doc", action="store_true", help="keep /// //! /** /*! documentation comments")
    what.add_argument("--all", action="store_true", help="remove everything: shebang, directives, licenses")
    what.add_argument("--keep-regex", action="append", default=[], metavar="PATTERN",
                      help="keep comments matching PATTERN (repeatable)")
    what.add_argument("--keep-shebang", action=argparse.BooleanOptionalAction, default=True)
    what.add_argument("--collapse-blank-lines", action="store_true", help="squeeze leftover blank runs")
    what.add_argument("--preserve-lines", action="store_true", help="blank comments out with spaces")

    how = parser.add_argument_group("how")
    how.add_argument("--verify", choices=("none", "parse", "tokens"), default="tokens",
                     help="post-write safety gate (default: tokens)")
    how.add_argument("-i", "--interactive", action="store_true", help="ask per comment (serial)")

    out = parser.add_argument_group("output")
    out.add_argument("-n", "--dry-run", "--dryrun", action="store_true", dest="dry_run")
    out.add_argument("--diff", action="store_true", help="print a unified diff (implies --dry-run)")
    out.add_argument("--check", action="store_true", help="write nothing; exit 1 if work is pending")
    out.add_argument("--backup", action="store_true", help="copy <file><SUF> before writing")
    out.add_argument("--backup-suffix", default=".bak", metavar="SUF")
    out.add_argument("--stdout", action="store_true", help="write the result to stdout (one file)")
    out.add_argument("--json", nargs="?", const="-", default=None, metavar="FILE",
                     help="machine-readable report ('-' = stdout)")
    out.add_argument("--stats", action="store_true", help="always print the summary line")
    out.add_argument("-q", "--quiet", action="store_true")
    out.add_argument("-v", "--verbose", action="store_true")
    out.add_argument("--color", choices=("auto", "always", "never"), default="auto")

    walk = parser.add_argument_group("traversal")
    walk.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 1))
    walk.add_argument("--max-size", type=int, default=8 * 1024 * 1024, metavar="BYTES")
    walk.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    walk.add_argument("--include", action="append", default=[], metavar="GLOB")
    walk.add_argument("--skip-dir", action="append", default=[], metavar="NAME")
    walk.add_argument("--no-recursive", action="store_true")
    walk.add_argument("--max-depth", type=int, default=0)
    walk.add_argument("--follow-symlinks", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def print_languages() -> None:
    print(f"{'language':<12}{'suffixes':<44}{'tree-sitter grammar':<26}regex")
    for lang in sorted(LANGUAGES, key=lambda item: item.name):
        print(f"{lang.name:<12}{' '.join(lang.suffixes):<44}{lang.ts_module or '-':<26}{'yes' if lang.lexable else 'no'}")
    print(f"\n{len(LANGUAGES)} languages")


def print_doctor() -> None:
    print("package            status   hint")
    for name in ("tree_sitter", "libcst", "rjsmin", "jsmin", "csscompressor", "htmlmin"):
        module = probe(name)
        status = "ok" if module is not None else "missing"
        hint = getattr(module, "__version__", "") if module is not None else INSTALL_HINTS.get(name, "")
        print(f"{name:<18} {status:<8} {hint}")
    print("\nper-language tree-sitter grammars")
    for lang in sorted(LANGUAGES, key=lambda item: item.name):
        if lang.ts_module is None:
            continue
        state = "ok" if probe(lang.ts_module) is not None else "missing"
        print(f"  {lang.name:<12} {lang.ts_module:<26} {state}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_languages:
        print_languages()
        return 0
    if args.doctor:
        print_doctor()
        return 0

    palette = Palette(args.color)
    policy = Policy(
        keep_shebang=args.keep_shebang and not args.all,
        keep_vital=not args.all,
        keep_doc=args.keep_doc,
        keep_regex=tuple(args.keep_regex),
        docstrings=args.docstrings or args.module_docstring,
        module_docstring=args.module_docstring,
        preserve_lines=args.preserve_lines,
        collapse_blank_lines=args.collapse_blank_lines,
        verify=args.verify,
    )
    cfg = Config(
        policy=policy,
        backend=args.backend,
        language=args.language,
        max_size=args.max_size,
        dry_run=args.dry_run or args.diff or args.check or args.stdout,
        backup=args.backup,
        backup_suffix=args.backup_suffix,
        diff=args.diff,
    )
    walk_cfg = Walk(
        recursive=not args.no_recursive,
        max_depth=args.max_depth,
        skip_dirs=DEFAULT_SKIP_DIRS | frozenset(args.skip_dir),
        include=tuple(args.include),
        exclude=tuple(args.exclude),
        follow_symlinks=args.follow_symlinks,
    )
    roots = [Path(p) for p in args.paths] or [Path.cwd()]
    targets = collect_targets(roots, walk_cfg, args.language)
    started = time.perf_counter()

    if not targets:
        if not args.quiet:
            print("no matching files", file=sys.stderr)
        return 0

    if args.stdout:
        if len(targets) > 1:
            parser.error("--stdout takes exactly one file")
        loaded = load_source(str(targets[0]), cfg)
        if isinstance(loaded, Result):
            print(loaded.detail, file=sys.stderr)
            return 1
        _, src, lang = loaded
        backend = choose_backend(lang, cfg.backend)
        new_src, _removed, problem = transform(src, lang, backend, policy)
        if problem:
            print(problem, file=sys.stderr)
            return 1
        sys.stdout.write(new_src.decode("utf-8", "replace"))
        return 0

    results: list[Result] = []
    if args.interactive or len(targets) == 1 or args.jobs <= 1:
        for target in targets:
            result = interactive(target, cfg) if args.interactive else process_path(str(target), cfg)
            results.append(result)
            if not args.quiet and not args.json:
                emit(result, palette, args.verbose)
    else:
        with ProcessPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(process_path, str(target), cfg): target for target in targets}
            for future, target in futures.items():
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    result = Result(str(target), "error", detail=f"{type(exc).__name__}: {exc}")
                results.append(result)
                if not args.quiet and not args.json:
                    emit(result, palette, args.verbose)

    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    measured = [r for r in results if r.status in ("changed", "unchanged")]
    before = sum(r.before for r in measured)
    after = sum(r.after for r in measured)
    removed = sum(r.removed for r in results)
    elapsed = time.perf_counter() - started
    sys.stdout.flush()

    if not args.json and (args.stats or (not args.quiet and (args.verbose or counts.get("changed")))):
        print(
            f"\n{counts.get('changed', 0)} changed · {counts.get('unchanged', 0)} unchanged · "
            f"{counts.get('skipped', 0)} skipped · {counts.get('rejected', 0)} rejected · "
            f"{counts.get('error', 0)} errors · {removed} item(s) · "
            f"{human(max(0, before - after))} saved · {elapsed:.2f}s",
            file=sys.stderr,
        )

    if args.json:
        payload = {
            "tool": PROG,
            "version": __version__,
            "roots": [str(root) for root in roots],
            "elapsed": round(elapsed, 3),
            "counts": counts,
            "bytes": {"before": before, "after": after, "saved": max(0, before - after)},
            "files": [
                {
                    "path": r.path, "status": r.status, "lang": r.lang, "backend": r.backend,
                    "before": r.before, "after": r.after, "removed": r.removed, "detail": r.detail,
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
    if args.check and counts.get("changed"):
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130)
