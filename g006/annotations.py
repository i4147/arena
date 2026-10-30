#!/usr/bin/env python3
"""annotations.py — put type annotations on, or take them off, in one tool.

g006 held four scripts and two half-broken ideas:

    annotate_types.py   add, from a .pyi stub produced by mypy.stubgen (libcst)
    add_hints.py        add, from heuristics: literal types, name table, return scan
    annotate.py         both directions, but `--remove` needed tree-sitter and a
                        stub was mandatory for `add`
    clean_type_annotations.py   remove, but the file does not parse (line 153)

They are the same program seen from three sides, so this file keeps the union:

    annotations.py add     [PATH…] [-s STUB] [--source auto|stub|heuristic]
                           [--generate-stub] [--no-overwrite] [--future-annotations]
                           [--no-returns] [--no-name-heuristics] [--validate compile|mypy|none]
    annotations.py remove  [PATH…] [-b auto|libcst|tree-sitter|ast] [--keep-type-comments]
    annotations.py check   [PATH…] [--strict]
    annotations.py doctor

Both directions are gated the same way: the result must parse, and its AST with
annotations stripped must equal the original's AST with annotations stripped.
Anything else is rejected and the file is left byte-identical — the one thing a
type-annotation tool must never do is quietly change your code.

Exit codes: 0 ok · 1 errors / --check found work · 2 usage · 130 interrupted
"""

from __future__ import annotations

import argparse
import ast
import difflib
import fnmatch
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tokenize
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "1.0.0"
PROG = "annotations.py"

SKIP_DIRS = {
    ".git", ".hg", ".svn", ".idea", ".vscode", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".cache", "target", "dist", "build",
}
TYPE_COMMENT_RE = re.compile(r"\s*#\s*type\s*:", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# optional backends                                                            #
# --------------------------------------------------------------------------- #


def probe(name: str):
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 - optional dependency
        return None


def find_spec(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


_CST = None
_TS_PARSER = None


def _cst():
    global _CST
    if _CST is None:
        _CST = probe("libcst")
    return _CST


def ts_parser():
    """A tree-sitter parser for Python, or None when the wheels are missing."""
    global _TS_PARSER
    if _TS_PARSER is None:
        runtime = probe("tree_sitter")
        grammar = probe("tree_sitter_python")
        if runtime is None or grammar is None:
            return None
        try:
            _TS_PARSER = runtime.Parser(runtime.Language(grammar.language()))
        except Exception:  # noqa: BLE001 - version skew between runtime and grammar
            try:
                _TS_PARSER = runtime.Parser(grammar.language())
            except Exception:  # noqa: BLE001
                return None
    return _TS_PARSER


def have_stubgen() -> bool:
    return find_spec("mypy") or shutil.which("stubgen") is not None


def have_mypy() -> bool:
    return shutil.which("mypy") is not None or find_spec("mypy")


# --------------------------------------------------------------------------- #
# run context                                                                  #
# --------------------------------------------------------------------------- #


@dataclass
class Ctx:
    command: str = ""
    dry_run: bool = False
    backup: bool = False
    check: bool = False
    quiet: bool = False
    verbose: bool = False
    json: bool = False
    started: float = field(default_factory=time.perf_counter)
    files: list[dict] = field(default_factory=list)
    failures: int = 0
    changed: int = 0


CTX = Ctx()


def say(message: str) -> None:
    if not CTX.quiet and not CTX.json:
        print(message)


def note(message: str) -> None:
    if CTX.verbose:
        print(message, file=sys.stderr)


def collect_targets(paths: Sequence[Path], *, recursive: bool, exclude: Sequence[str],
                    suffixes: Sequence[str] = (".py",)) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for raw in paths or [Path.cwd()]:
        path = raw.expanduser()
        if not path.exists():
            print(f"{path}: no such file or directory", file=sys.stderr)
            continue
        if path.is_file():
            candidates: list[Path] = [path]
        else:
            iterator = path.rglob("*") if recursive else path.glob("*")
            candidates = sorted(item for item in iterator if item.is_file())
        for candidate in candidates:
            if candidate.is_symlink():
                continue
            if path.is_dir():
                relative = candidate.relative_to(path).parts[:-1]
                if any(part in SKIP_DIRS for part in relative):
                    continue
            if any(fnmatch.fnmatch(candidate.name, pattern) or fnmatch.fnmatch(str(candidate), pattern)
                   for pattern in exclude):
                continue
            if suffixes and candidate.suffix not in suffixes:
                continue
            key = candidate.resolve()
            if key not in seen:
                seen.add(key)
                found.append(candidate)
    return found


def show_diff(path: Path, before: str, after: str) -> None:
    sys.stdout.writelines(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}",
    ))


def atomic_write(path: Path, text: str, *, backup: bool) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        shutil.copymode(path, tmp)
        if backup:
            spare = path.with_suffix(path.suffix + ".bak")
            if not spare.exists():
                shutil.copy2(path, spare)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# the safety gate                                                              #
# --------------------------------------------------------------------------- #


class _StripAnnotations(ast.NodeTransformer):
    """Erase annotations so two trees can be compared for structure only."""

    def visit_arg(self, node: ast.arg) -> ast.arg:
        node.annotation = None
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self.generic_visit(node)
        node.returns = None
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST:
        self.generic_visit(node)
        if node.value is None:
            # `x: int` and `x` are the same statement once annotations are gone
            try:
                node.target.ctx = ast.Load()
            except AttributeError:
                pass
            return ast.Expr(value=node.target)
        # `y: List[str] = []` and `y = []` are the same statement too
        return ast.Assign(targets=[node.target], value=node.value)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST | None:
        # typing imports exist only to spell annotations
        return None if node.module in ("typing", "typing_extensions", "_typeshed") else node


def structure(source: str, filename: str = "<string>") -> str:
    tree = ast.parse(source, filename=filename)
    tree = _StripAnnotations().visit(tree)
    # `# type:` comments live in the AST as type_comment / type_ignores
    tree.type_ignores = []
    for node in ast.walk(tree):
        if hasattr(node, "type_comment"):
            node.type_comment = None
    ast.fix_missing_locations(tree)
    return ast.dump(tree, include_attributes=False)


def gate(before: str, after: str, filename: str) -> None:
    """Raise ValueError unless ``after`` differs from ``before`` only in annotations."""
    try:
        ast.parse(after, filename=filename)
    except SyntaxError as exc:
        raise ValueError(f"generated code does not parse: {exc}") from exc
    try:
        left = structure(before, filename)
        right = structure(after, filename)
    except SyntaxError as exc:
        raise ValueError(f"cannot compare trees: {exc}") from exc
    if left != right:
        raise ValueError("generated code differs from the original beyond annotations")


def strip_type_comments(source: str) -> tuple[str, int]:
    """Remove ``# type:`` comments by token position, never touching strings."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError):
        return source, 0
    lines = source.splitlines(keepends=True)
    removals: dict[int, list[tuple[int, int]]] = {}
    count = 0
    for token in tokens:
        if token.type != tokenize.COMMENT or not TYPE_COMMENT_RE.match(token.string):
            continue
        row, start = token.start
        _, end = token.end
        removals.setdefault(row, []).append((start, end))
        count += 1
    for row, spans in removals.items():
        if row - 1 >= len(lines):
            continue
        text = lines[row - 1]
        ending = text[len(text.rstrip("\r\n")):] if text.endswith(("\r\n", "\n")) else ""
        body = text[: len(text) - len(ending)] if ending else text
        for start, end in sorted(spans, reverse=True):
            body = body[:start] + body[end:]
        lines[row - 1] = body.rstrip() + ending
    return "".join(lines), count


# --------------------------------------------------------------------------- #
# remove: three backends                                                       #
# --------------------------------------------------------------------------- #


def remove_libcst(source: str) -> tuple[str, list[str]]:
    cst = _cst()
    if cst is None:
        raise RuntimeError("libcst is not installed: pip install libcst")

    class _Remover(cst.CSTTransformer):
        def leave_Param(self, original_node, updated_node):
            if updated_node.annotation is None:
                return updated_node
            return updated_node.with_changes(annotation=None)

        def leave_FunctionDef(self, original_node, updated_node):
            if updated_node.returns is None:
                return updated_node
            return updated_node.with_changes(returns=None)

        leave_AsyncFunctionDef = leave_FunctionDef

        def leave_AnnAssign(self, original_node, updated_node):
            target = updated_node.target
            if updated_node.value is not None:
                return cst.Assign(targets=[cst.AssignTarget(target)], value=updated_node.value)
            return cst.Expr(value=target)

    module = cst.parse_module(source)
    return module.visit(_Remover()).code, []


def remove_ast(source: str) -> tuple[str, list[str]]:
    class _Remover(ast.NodeTransformer):
        def visit_arg(self, node: ast.arg) -> ast.arg:
            node.annotation = None
            return node

        def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
            self.generic_visit(node)
            node.returns = None
            return node

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AST | None:
            if node.value is None:
                return ast.Expr(value=node.target)
            return ast.Assign(targets=[node.target], value=node.value)

    tree = ast.parse(source)
    tree = _Remover().visit(tree)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n", []


def _ts_walk(node) -> Iterator:
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(getattr(current, "children", []))


def _ts_span(raw: bytes, node, arrow: bool) -> tuple[int, int] | None:
    """Span covering ``: T`` or ``-> T``, trimmed of the surrounding spaces."""
    start, end = node.start_byte, node.end_byte
    index = start - 1
    while index >= 0 and raw[index:index + 1] in b" \t\n":
        index -= 1
    if arrow:
        if raw[index - 1:index + 1] != b"->":
            return None
        begin = index - 1
    else:
        if raw[index:index + 1] != b":":
            return None
        begin = index
    while begin > 0 and raw[begin - 1:begin] in b" \t":
        begin -= 1
    trimmed = end
    while trimmed < len(raw) and raw[trimmed:trimmed + 1] in b" \t":
        trimmed += 1
    if raw[trimmed:trimmed + 1] != b"=":
        end = trimmed  # keep the space in `x: int = 1` so it becomes `x = 1`
    return begin, end


def remove_tree_sitter(source: str) -> tuple[str, list[str]]:
    """Byte-range splicing: formatting survives, unlike the ast backend."""
    parser = ts_parser()
    if parser is None:
        raise RuntimeError("tree-sitter is not installed: pip install tree-sitter tree-sitter-python")
    raw = source.encode("utf-8")
    tree = parser.parse(raw)
    spans: list[tuple[int, int]] = []
    warnings: list[str] = []
    for node in _ts_walk(tree.root_node):
        kind = getattr(node, "type", "")
        target = None
        arrow = False
        if kind == "function_definition":
            target = node.child_by_field_name("return_type")
            arrow = True
        elif kind in ("typed_parameter", "typed_default_parameter", "assignment"):
            target = node.child_by_field_name("type")
        if target is None:
            continue
        span = _ts_span(raw, target, arrow)
        if span is None:
            row = target.start_point[0] + 1
            warnings.append(f"left an annotation it could not locate on line {row}")
            continue
        spans.append(span)
    if not spans:
        return source, warnings
    spans.sort()
    merged: list[list[int]] = [[spans[0][0], spans[0][1]]]
    for start_byte, end_byte in spans[1:]:
        if start_byte <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end_byte)
        else:
            merged.append([start_byte, end_byte])
    out = bytearray(raw)
    for start_byte, end_byte in reversed(merged):
        del out[start_byte:end_byte]
    return out.decode("utf-8", errors="replace"), warnings


REMOVE_BACKENDS = {
    "libcst": remove_libcst,
    "tree-sitter": remove_tree_sitter,
    "ast": remove_ast,
}


def pick_remove_backend(preferred: str) -> str:
    if preferred != "auto":
        return preferred
    if ts_parser() is not None:
        return "tree-sitter"
    if _cst() is not None:
        return "libcst"
    return "ast"


# --------------------------------------------------------------------------- #
# add: heuristics                                                              #
# --------------------------------------------------------------------------- #

NAME_HINTS: dict[str, str] = {
    "name": "str", "path": "str", "text": "str", "msg": "str", "message": "str",
    "url": "str", "filename": "str", "prefix": "str", "suffix": "str", "key": "str",
    "count": "int", "n": "int", "index": "int", "idx": "int", "size": "int",
    "limit": "int", "offset": "int", "port": "int",
    "verbose": "bool", "debug": "bool", "force": "bool", "enabled": "bool",
    "flag": "bool", "dry_run": "bool",
    "ratio": "float", "threshold": "float",
    "items": "list", "values": "list", "options": "dict", "kwargs": "dict",
}


def infer_literal_type(node) -> str | None:
    cst = _cst()
    if cst is None:
        return None
    if isinstance(node, cst.Integer):
        return "int"
    if isinstance(node, cst.Float):
        return "float"
    if isinstance(node, cst.Imaginary):
        return "complex"
    if isinstance(node, cst.SimpleString):
        return "bytes" if "b" in node.prefix.lower() else "str"
    if isinstance(node, (cst.ConcatenatedString, cst.FormattedString)):
        return "str"
    if isinstance(node, cst.Name):
        return {"True": "bool", "False": "bool", "None": "None"}.get(node.value)
    if isinstance(node, cst.List):
        return "list"
    if isinstance(node, cst.Tuple):
        return "tuple"
    if isinstance(node, cst.Dict):
        return "dict"
    if isinstance(node, cst.Set):
        return "set"
    if isinstance(node, cst.ListComp):
        return "list"
    if isinstance(node, cst.DictComp):
        return "dict"
    if isinstance(node, cst.SetComp):
        return "set"
    if isinstance(node, cst.Comparison):
        return "bool"
    if isinstance(node, cst.UnaryOperation):
        if isinstance(node.operator, cst.Not):
            return "bool"
        if isinstance(node.operand, (cst.Integer, cst.Float)):
            return infer_literal_type(node.operand)
    if isinstance(node, cst.Call) and isinstance(node.func, cst.Name):
        name = node.func.value
        if name in {"int", "float", "str", "bool", "bytes", "list", "dict", "set", "tuple"}:
            return name
        if name == "len":
            return "int"
        if name == "repr":
            return "str"
    return None


def infer_return_type(func) -> str | None:
    """Return type from the ``return`` statements, ignoring nested scopes."""
    cst = _cst()
    if func.name.value in {"__init__", "__init_subclass__", "__set_name__"}:
        return "None"
    types: set[str] = set()
    yields = [False]

    def walk(node) -> None:
        for child in getattr(node, "children", []):
            if isinstance(child, cst.Yield):
                yields[0] = True
            if isinstance(child, cst.Return):
                if child.value is None:
                    types.add("None")
                else:
                    types.add(infer_literal_type(child.value) or "?")
            if isinstance(child, (cst.Lambda, cst.ClassDef, cst.FunctionDef)) and child is not func:
                continue
            walk(child)

    walk(func)
    if yields[0] or not types or "?" in types:
        return None
    non_none = types - {"None"}
    if not non_none:
        return "None"
    if len(non_none) == 1:
        only = next(iter(non_none))
        return only if "None" not in types else f"Optional[{only}]"
    return None


def add_heuristic(source: str, *, add_returns: bool, name_heuristics: bool) -> tuple[str, dict]:
    cst = _cst()
    if cst is None:
        raise RuntimeError("adding annotations needs libcst: pip install libcst")
    from libcst.codemod import CodemodContext
    from libcst.codemod.visitors import AddImportsVisitor

    class _Annotate(cst.CSTTransformer):
        def __init__(self) -> None:
            self.stats = {"params": 0, "returns": 0}
            self.context = CodemodContext()
            self.class_depth = 0

        def visit_ClassDef(self, node):
            self.class_depth += 1
            return True

        def leave_ClassDef(self, original_node, updated_node):
            self.class_depth -= 1
            return updated_node

        def leave_FunctionDef(self, original_node, updated_node):
            params = updated_node.params
            is_static = any(
                isinstance(decorator.decorator, cst.Name) and decorator.decorator.value == "staticmethod"
                for decorator in getattr(updated_node, "decorators", [])
            )
            is_method = self.class_depth > 0 and not is_static
            updated_node = updated_node.with_changes(params=params.with_changes(
                params=self._annotate(params.params, is_method),
                posonly_params=self._annotate(params.posonly_params, is_method),
                kwonly_params=self._annotate(params.kwonly_params, False),
            ))
            if add_returns and updated_node.returns is None:
                guessed = infer_return_type(updated_node)
                if guessed:
                    if guessed.startswith("Optional["):
                        AddImportsVisitor.add_needed_import(self.context, "typing", "Optional")
                    updated_node = updated_node.with_changes(
                        returns=cst.Annotation(annotation=cst.parse_expression(guessed)))
                    self.stats["returns"] += 1
            return updated_node

        leave_AsyncFunctionDef = leave_FunctionDef

        def _annotate(self, params, is_method: bool):
            out = []
            for index, param in enumerate(params):
                skip_self = is_method and index == 0 and param.name.value in ("self", "cls")
                if param.annotation is None and not skip_self and param.star not in ("*", "**"):
                    guessed = self._guess(param)
                    if guessed:
                        if guessed.startswith("Optional["):
                            AddImportsVisitor.add_needed_import(self.context, "typing", "Optional")
                        param = param.with_changes(
                            annotation=cst.Annotation(annotation=cst.parse_expression(guessed)),
                            equal=(cst.AssignEqual() if param.default is not None else getattr(cst, "MaybeSentinel").DEFAULT),
                        )
                        self.stats["params"] += 1
                out.append(param)
            return tuple(out)

        def _guess(self, param) -> str | None:
            if param.default is not None:
                literal = infer_literal_type(param.default)
                if literal == "None":
                    base = NAME_HINTS.get(param.name.value) if name_heuristics else None
                    return f"Optional[{base}]" if base else None
                if literal:
                    return literal
            if name_heuristics:
                return NAME_HINTS.get(param.name.value)
            return None

    module = cst.parse_module(source)
    transformer = _Annotate()
    tree = module.visit(transformer)
    tree = AddImportsVisitor(transformer.context).transform_module(tree)
    return tree.code, transformer.stats


def add_from_stub(source: str, stub_code: str, *, overwrite: bool, future: bool) -> tuple[str, dict]:
    cst = _cst()
    if cst is None:
        raise RuntimeError("adding annotations needs libcst: pip install libcst")
    from libcst.codemod import CodemodContext
    from libcst.codemod.visitors import ApplyTypeAnnotationsVisitor

    class _Typeshed(cst.CSTTransformer):
        def leave_ImportFrom(self, original_node, updated_node):
            if original_node.module is not None:
                name = cst.helpers.get_full_name_for_node(original_node.module)
                if name == "_typeshed":
                    return cst.ImportFrom(module=cst.Name("typing"),
                                          names=[cst.ImportAlias(name=cst.Name("Any"))])
            return updated_node

        def leave_Attribute(self, original_node, updated_node):
            if (isinstance(original_node.value, cst.Name) and original_node.value.value == "_typeshed"
                    and original_node.attr.value == "Incomplete"):
                return cst.Attribute(value=cst.Name("typing"), attr=cst.Name("Any"))
            return updated_node

        def leave_Name(self, original_node, updated_node):
            return cst.Name("Any") if original_node.value == "Incomplete" else updated_node

        def leave_Subscript(self, original_node, updated_node):
            # `Incomplete | str` becomes `Any | str` through leave_Name; nothing else to do
            return updated_node

    module = cst.parse_module(source)
    stub = cst.parse_module(stub_code).visit(_Typeshed())
    context = CodemodContext()
    ApplyTypeAnnotationsVisitor.store_stub_in_context(
        context=context, stub=stub, overwrite_existing_annotations=overwrite,
        use_future_annotations=future,
    )
    tree = ApplyTypeAnnotationsVisitor(context).transform_module(module)
    return tree.code, {"stub": True}


def generate_stub(path: Path) -> Path | None:
    """Run mypy.stubgen for one file into a temporary directory."""
    target = Path(tempfile.mkdtemp(prefix="annotations-stub-"))
    try:
        argv = ([sys.executable, "-m", "mypy.stubgen"] if find_spec("mypy")
                else ["stubgen"])
        subprocess.run([*argv, "-o", str(target), str(path)], check=True, capture_output=True)
    except Exception as exc:  # noqa: BLE001 - the hint matters more than the traceback
        print(f"{path}: cannot generate a stub ({exc}); pip install mypy", file=sys.stderr)
        return None
    for candidate in target.rglob("*.pyi"):
        return candidate
    return None


# --------------------------------------------------------------------------- #
# coverage check                                                               #
# --------------------------------------------------------------------------- #


@dataclass
class Coverage:
    path: str
    functions: int = 0
    annotated_returns: int = 0
    params: int = 0
    annotated_params: int = 0
    missing: list[str] = field(default_factory=list)


def analyse_coverage(path: Path, source: str) -> Coverage:
    report = Coverage(str(path))
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        report.functions += 1
        if node.returns is not None:
            report.annotated_returns += 1
        else:
            report.missing.append(f"{node.name}(): missing return type (line {node.lineno})")
        args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        for arg in args:
            if arg.arg in ("self", "cls"):
                continue
            report.params += 1
            if arg.annotation is not None:
                report.annotated_params += 1
            else:
                report.missing.append(f"{node.name}({arg.arg}): missing parameter type (line {node.lineno})")
    return report


# --------------------------------------------------------------------------- #
# commands                                                                     #
# --------------------------------------------------------------------------- #


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def cmd_remove(args: argparse.Namespace) -> int:
    targets = collect_targets(args.paths, recursive=args.recursive, exclude=args.exclude)
    if not targets:
        print("no Python files found", file=sys.stderr)
        return 1
    backend = pick_remove_backend(args.backend)
    handler = REMOVE_BACKENDS[backend]
    if backend == "libcst" and _cst() is None:
        print("libcst is not installed; falling back to ast", file=sys.stderr)
        backend, handler = "ast", remove_ast

    def work(path: Path) -> tuple[Path, str]:
        source = _read(path)
        try:
            new_source, warnings = handler(source)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
            raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc
        for warning in warnings:
            note(f"{path}: {warning}")
        if not args.keep_type_comments:
            new_source, removed = strip_type_comments(new_source)
            if removed:
                note(f"{path}: dropped {removed} type comment(s)")
        if new_source == source:
            return path, "unchanged", {}
        gate(source, new_source, str(path))
        if getattr(args, "diff", False):
            show_diff(path, source, new_source)
        if CTX.dry_run or CTX.check:
            return path, "would-change", {}
        atomic_write(path, new_source, backup=CTX.backup)
        return path, "changed", {}

    results = _run_parallel(work, targets, args.jobs)
    for path, status, _extra in results:
        if status == "error":
            continue
        CTX.files.append({"path": str(path), "status": status, "backend": backend})
        if status == "would-change":
            CTX.changed += 1
            say(f"[{'check' if CTX.check else 'dry'}] {path}: annotations would be removed")
        elif status == "changed":
            CTX.changed += 1
            say(f"[done] {path}: annotations removed ({backend})")
        else:
            note(f"{path}: nothing to remove")
    return _finish("remove")


def cmd_add(args: argparse.Namespace) -> int:
    targets = collect_targets(args.paths, recursive=args.recursive, exclude=args.exclude)
    if not targets:
        print("no Python files found", file=sys.stderr)
        return 1
    if args.source == "stub" and args.stub_file and len(targets) != 1:
        print("-s/--stub-file takes exactly one input file", file=sys.stderr)
        return 2

    def work(path: Path) -> tuple[Path, str, dict]:
        source = _read(path)
        source_kind = args.source
        if args.stub_file:
            stub_path = Path(args.stub_file)
        elif args.stub_dir:
            stub_path = Path(args.stub_dir) / (path.stem + ".pyi")
        else:
            stub_path = path.with_suffix(".pyi")
        if source_kind == "auto":
            source_kind = "stub" if stub_path.is_file() else "heuristic"
        if source_kind == "stub":
            if not stub_path.is_file():
                if not args.generate_stub:
                    raise RuntimeError(f"no stub for {path} (looked for {stub_path.name}); "
                                       f"pass --generate-stub or --source heuristic")
                generated = generate_stub(path)
                if generated is None:
                    raise RuntimeError("stubgen produced no .pyi")
                stub_path = generated
            new_source, stats = add_from_stub(source, _read(stub_path),
                                              overwrite=not args.no_overwrite,
                                              future=args.future_annotations)
        else:
            new_source, stats = add_heuristic(source, add_returns=not args.no_returns,
                                              name_heuristics=not args.no_name_heuristics)
        if new_source == source:
            return path, "unchanged", stats
        gate(source, new_source, str(path))
        if args.validate == "compile":
            compile(new_source, str(path), "exec")
        if getattr(args, "diff", False):
            show_diff(path, source, new_source)
        if CTX.dry_run or CTX.check:
            return path, "would-change", stats
        atomic_write(path, new_source, backup=CTX.backup)
        if args.validate == "mypy":
            _run_mypy(path)
        return path, "changed", stats

    results = _run_parallel(work, targets, args.jobs)
    for path, status, stats in results:
        if status == "error":
            continue
        detail = (f"{stats.get('params', 0)} param + {stats.get('returns', 0)} return hint(s)"
                  if not stats.get("stub") else "annotations from stub")
        CTX.files.append({"path": str(path), "status": status,
                          "params": stats.get("params", 0), "returns": stats.get("returns", 0),
                          "source": "stub" if stats.get("stub") else "heuristic"})
        if status == "would-change":
            CTX.changed += 1
            say(f"[{'check' if CTX.check else 'dry'}] {path}: would add {detail}")
        elif status == "changed":
            CTX.changed += 1
            say(f"[done] {path}: added {detail}")
        else:
            note(f"{path}: nothing to add")
    return _finish("add")


def _run_mypy(path: Path) -> None:
    if not have_mypy():
        note("mypy not installed; skipping --validate mypy")
        return
    argv = ["mypy"] if shutil.which("mypy") else [sys.executable, "-m", "mypy"]
    result = subprocess.run([*argv, "--no-error-summary", str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout.strip() or result.stderr.strip(), file=sys.stderr)


def cmd_check(args: argparse.Namespace) -> int:
    targets = collect_targets(args.paths, recursive=args.recursive, exclude=args.exclude)
    if not targets:
        print("no Python files found", file=sys.stderr)
        return 1
    reports: list[Coverage] = []
    failures = 0
    for path in targets:
        try:
            report = analyse_coverage(path, _read(path))
        except SyntaxError as exc:
            failures += 1
            print(f"{path}: syntax error: {exc}", file=sys.stderr)
            continue
        reports.append(report)
    total_functions = sum(report.functions for report in reports)
    total_params = sum(report.params for report in reports)
    annotated_params = sum(report.annotated_params for report in reports)
    annotated_returns = sum(report.annotated_returns for report in reports)
    for report in reports:
        CTX.files.append({
            "path": report.path, "functions": report.functions,
            "returns": report.annotated_returns, "params": report.params,
            "annotated_params": report.annotated_params, "missing": len(report.missing),
        })
        if report.missing and not CTX.json:
            say(f"{report.path}: {len(report.missing)} missing annotation(s)")
            if CTX.verbose:
                for item in report.missing:
                    say(f"    {item}")
    pending = total_functions - annotated_returns + total_params - annotated_params
    if not CTX.json:
        ratio = (annotated_params + annotated_returns) / max(total_params + total_functions, 1)
        say(f"\n{len(reports)} file(s) · {total_functions} function(s) · "
            f"{annotated_returns}/{total_functions} returns · "
            f"{annotated_params}/{total_params} params · {ratio:.0%} annotated")
    if args.strict and pending:
        return 1
    return 1 if failures else 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    rows = (
        ("libcst", _cst() is not None, "pip install libcst", "add (heuristic + stub), remove -b libcst"),
        ("tree_sitter", ts_parser() is not None, "pip install tree-sitter tree-sitter-python",
         "remove -b tree-sitter (formatting-preserving)"),
        ("mypy", have_stubgen(), "pip install mypy", "--generate-stub / --validate mypy"),
        ("ast", True, "", "remove -b ast (always available, reformats)"),
    )
    print(f"{PROG} {__version__}  python {sys.version.split()[0]}  ({os.cpu_count()} cpu)\n")
    print(f"{'backend':<14}{'status':<9}{'enables':<44}hint")
    for name, ok, hint, enables in rows:
        print(f"{name:<14}{'ok' if ok else 'missing':<9}{enables:<44}{'' if ok else hint}")
    if _cst() is None:
        print("\nwithout libcst: `remove -b ast` still works, `add` does not.")
    return 0


def _run_parallel(worker, targets: Sequence[Path], jobs: int) -> list:
    """Run ``worker`` over targets, converting exceptions into per-file failures."""

    def guard(path: Path):
        try:
            return worker(path)
        except Exception as exc:  # noqa: BLE001 - reported per file, batch continues
            CTX.failures += 1
            print(f"{path}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return (path, "error", {})

    if jobs > 1 and len(targets) > 1:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            return list(pool.map(guard, targets))
    return [guard(path) for path in targets]


def _finish(command: str) -> int:
    if not CTX.json:
        if CTX.check:
            say(f"\n{CTX.changed} file(s) would change")
        return 1 if (CTX.failures or (CTX.check and CTX.changed)) else 0
    payload = {
        "tool": PROG,
        "version": __version__,
        "command": command,
        "elapsed": round(time.perf_counter() - CTX.started, 3),
        "counts": {"files": len(CTX.files), "changed": CTX.changed, "failed": CTX.failures},
        "files": CTX.files,
    }
    print(json.dumps(payload, indent=2))
    return 1 if (CTX.failures or (CTX.check and CTX.changed)) else 0


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-n", "--dry-run", action="store_true", help="report changes, write nothing")
    common.add_argument("--check", action="store_true",
                        help="write nothing; exit 1 if anything would change")
    common.add_argument("--backup", action="store_true", help="keep a .bak before overwriting")
    common.add_argument("-d", "--diff", action="store_true",
                        help="print a unified diff (implies --dry-run)")
    common.add_argument("-q", "--quiet", action="store_true")
    common.add_argument("-v", "--verbose", action="store_true")
    common.add_argument("--json", action="store_true", help="machine-readable report")
    common.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    common.add_argument("-r", "--recursive", action="store_true", help="descend into directories")
    common.add_argument("-j", "--jobs", type=int, default=1)

    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Add or remove type annotations across a Python tree.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "was: annotate_types.py add_hints.py annotate.py clean_type_annotations.py\n"
            "\n"
            "examples:\n"
            "  %(prog)s add src/ -r --source heuristic -n --diff\n"
            "  %(prog)s add pkg/mod.py --source stub --generate-stub\n"
            "  %(prog)s remove . -r -b tree-sitter --check\n"
            "  %(prog)s check . -r --strict\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subs = parser.add_subparsers(dest="command", required=True)

    def add_paths(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("paths", nargs="*", type=Path, metavar="PATH")

    add = subs.add_parser("add", parents=[common], help="add annotations")
    add_paths(add)
    add.add_argument("-s", "--stub-file", default=None, help="custom .pyi for a single file")
    add.add_argument("--stub-dir", default=None, help="directory holding <name>.pyi files")
    add.add_argument("--source", choices=("auto", "stub", "heuristic"), default="auto")
    add.add_argument("--generate-stub", action="store_true", help="run mypy.stubgen when no .pyi exists")
    add.add_argument("--no-overwrite", action="store_true", help="keep existing annotations (stub mode)")
    add.add_argument("--future-annotations", action="store_true",
                     help="emit `from __future__ import annotations`")
    add.add_argument("--no-returns", action="store_true", help="only annotate parameters")
    add.add_argument("--no-name-heuristics", action="store_true", help="do not guess from parameter names")
    add.add_argument("--validate", choices=("none", "compile", "mypy"), default="compile")

    remove = subs.add_parser("remove", parents=[common], help="remove annotations")
    add_paths(remove)
    remove.add_argument("-b", "--backend", choices=("auto", "tree-sitter", "libcst", "ast"), default="auto")
    remove.add_argument("--keep-type-comments", action="store_true",
                        help="leave `# type:` comments alone")

    check = subs.add_parser("check", parents=[common], help="annotation coverage")
    add_paths(check)
    check.add_argument("--strict", action="store_true", help="exit 1 when anything is unannotated")

    subs.add_parser("doctor", help="which backends are usable here")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    CTX.command = args.command
    for flag in ("dry_run", "backup", "quiet", "verbose", "json"):
        setattr(CTX, flag, getattr(args, flag, False))
    CTX.check = getattr(args, "check", False)
    if args.command == "doctor":
        return cmd_doctor(args)
    if CTX.dry_run and CTX.check:
        print("--dry-run and --check are the same thing; pass one", file=sys.stderr)
        return 2
    if getattr(args, "diff", False):
        CTX.dry_run = True
    try:
        if args.command == "add":
            return cmd_add(args)
        if args.command == "remove":
            return cmd_remove(args)
        return cmd_check(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
