#!/data/data/com.termux/files/usr/bin/env python3
"""g006 - consolidated type annotation tooling.

Everything that used to live in this folder as separate scripts now lives in
this one file.  Each tool's original source is embedded verbatim below, so
nothing was lost: ``python g006.py unpack <tool>`` writes the original
file back out.

Three generations of the same typeshed-stub annotator plus the annotation remover.

Tools
-----
hints       Infer and add literal/return annotations with name heuristics, validated by re-parsing.
            from add_hints.py (361 lines)
annotate    Apply typeshed stub annotations in parallel, merging and validating the result.
            from annotate.py (639 lines)
unannotate  Remove type annotations (params, returns, AnnAssign, PEP 695 type params) via libcst.
            from clean_type_annotations.py (230 lines)

Usage
-----
    python g006.py list                 # list the tools
    python g006.py doctor               # report missing third-party packages
    python g006.py <tool> --help        # help for one tool
    python g006.py hints <args>
    python g006.py annotate <args>
    python g006.py unannotate <args>
    python g006.py unpack <tool> [-o DIR] [--stdout]

Requires Python 3.10+.  Individual tools need their own third-party packages;
run ``doctor`` to see which are missing.
"""

from __future__ import annotations

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

FOLDER = "g006"


_HERE = Path(__file__).resolve().parent if "__file__" in globals() else None

TOOLS: dict[str, dict[str, object]] = {
    "hints": {"source": "add_hints.py", "lines": 361, "summary": 'Infer and add literal/return annotations with name heuristics, validated by re-parsing.'},
    "annotate": {"source": "annotate.py", "lines": 639, "summary": 'Apply typeshed stub annotations in parallel, merging and validating the result.'},
    "unannotate": {"source": "clean_type_annotations.py", "lines": 230, "summary": 'Remove type annotations (params, returns, AnnAssign, PEP 695 type params) via libcst.'},
}

_SOURCES: dict[str, str] = {}
_LOADED: dict[str, types.ModuleType] = {}


def _decode(*chunks: str) -> str:
    """Fallback representation for sources that cannot be written as raw strings."""
    return zlib.decompress(base64.b85decode("".join(chunks))).decode("utf-8")


_SOURCES['hints'] = r'''#!/data/data/com.termux/files/usr/bin/env python
"""(No content in stream)

---"""

from __future__ import annotations

import argparse
import ast
import difflib
import shutil
import sys
from pathlib import Path
from typing import Optional

import libcst as cst
from libcst.codemod import CodemodContext
from libcst.codemod.visitors import AddImportsVisitor

NAME_HINTS: dict[str, str] = {
    "name": "str",
    "path": "str",
    "text": "str",
    "msg": "str",
    "message": "str",
    "url": "str",
    "filename": "str",
    "prefix": "str",
    "suffix": "str",
    "key": "str",
    "count": "int",
    "n": "int",
    "i": "int",
    "index": "int",
    "idx": "int",
    "size": "int",
    "limit": "int",
    "offset": "int",
    "port": "int",
    "verbose": "bool",
    "debug": "bool",
    "force": "bool",
    "enabled": "bool",
    "flag": "bool",
    "dry_run": "bool",
    "ratio": "float",
    "threshold": "float",
    "items": "list",
    "values": "list",
    "options": "dict",
    "kwargs": "dict",
}


def infer_literal_type(node: cst.BaseExpression) -> Optional[str]:
    if isinstance(node, cst.Integer):
        return "int"
    if isinstance(node, cst.Float):
        return "float"
    if isinstance(node, cst.Imaginary):
        return "complex"
    if isinstance(node, cst.SimpleString):
        prefix = node.prefix.lower()
        return "bytes" if "b" in prefix else "str"
    if isinstance(node, (cst.ConcatenatedString, cst.FormattedString)):
        return "str"
    if isinstance(node, cst.Name):
        if node.value in ("True", "False"):
            return "bool"
        if node.value == "None":
            return "None"
        return None
    if isinstance(node, cst.List):
        return "list"
    if isinstance(node, cst.Tuple):
        return "tuple"
    if isinstance(node, cst.Dict):
        return "dict"
    if isinstance(node, cst.Set):
        return "set"
    if isinstance(node, (cst.ListComp,)):
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
    if (
        isinstance(node, cst.Call)
        and isinstance(node.func, cst.Name)
        and node.func.value
        in {
            "int",
            "float",
            "str",
            "bool",
            "bytes",
            "list",
            "dict",
            "set",
            "tuple",
            "len",
            "repr",
        }
    ):
        return {"len": "int", "repr": "str"}.get(node.func.value, node.func.value)
    return None


class _ReturnCollector(cst.CSTVisitor):
    def __init__(self) -> None:
        self.types: set[str] = set()
        self.has_yield = False
        self._depth = 0

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._depth += 1

    def leave_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._depth -= 1

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        self._depth += 1

    def leave_ClassDef(self, node: cst.ClassDef) -> None:
        self._depth -= 1

    def visit_Yield(self, node: cst.Yield) -> None:
        if self._depth == 0:
            self.has_yield = True

    def visit_Return(self, node: cst.Return) -> None:
        if self._depth:
            return
        if node.value is None:
            self.types.add("None")
        else:
            self.types.add(infer_literal_type(node.value) or "?")


def infer_return_type(func: cst.FunctionDef) -> Optional[str]:
    collector = _ReturnCollector()
    func.body.visit(collector)
    if collector.has_yield:
        return None
    if func.name.value in {"__init__", "__init_subclass__", "__set_name__"}:
        return "None"
    if not collector.types:
        return "None"
    if "?" in collector.types:
        return None
    if collector.types == {"None"}:
        return "None"
    non_none = collector.types - {"None"}
    if len(non_none) != 1:
        return None
    (only,) = non_none
    if "None" in collector.types:
        return f"Optional[{only}]"
    return only


class AnnotateTransformer(cst.CSTTransformer):
    def __init__(
        self,
        context: CodemodContext,
        *,
        add_returns: bool = True,
        name_heuristics: bool = False,
    ) -> None:
        super().__init__()
        self.context = context
        self.add_returns = add_returns
        self.name_heuristics = name_heuristics
        self.stats = {"params": 0, "returns": 0}
        self._scope: list[str] = []

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        self._scope.append("class")

    def leave_ClassDef(self, original: cst.ClassDef, updated: cst.ClassDef) -> cst.ClassDef:
        self._scope.pop()
        return updated

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._scope.append("func")

    def leave_FunctionDef(self, original: cst.FunctionDef, updated: cst.FunctionDef) -> cst.FunctionDef:
        self._scope.pop()
        is_method = bool(self._scope) and self._scope[-1] == "class"
        is_static = any(
            isinstance(d.decorator, cst.Name) and d.decorator.value == "staticmethod" for d in updated.decorators
        )
        params = updated.params
        new_posonly = self._annotate_params(params.posonly_params, is_method and not is_static, first_group=True)
        new_params = self._annotate_params(
            params.params,
            is_method and not is_static and not params.posonly_params,
            first_group=True,
        )
        new_kwonly = self._annotate_params(params.kwonly_params, False)
        updated = updated.with_changes(
            params=params.with_changes(posonly_params=new_posonly, params=new_params, kwonly_params=new_kwonly)
        )
        if self.add_returns and updated.returns is None:
            ret = infer_return_type(updated)
            if ret is not None:
                if ret.startswith("Optional["):
                    AddImportsVisitor.add_needed_import(self.context, "typing", "Optional")
                updated = updated.with_changes(returns=cst.Annotation(annotation=cst.parse_expression(ret)))
                self.stats["returns"] += 1
        return updated

    def _annotate_params(self, params, skip_self: bool, first_group: bool = False):
        out = []
        for i, p in enumerate(params):
            if p.annotation is None and not (skip_self and i == 0 and p.name.value in ("self", "cls")):
                t = self._infer_param(p)
                if t is not None:
                    if t.startswith("Optional["):
                        AddImportsVisitor.add_needed_import(self.context, "typing", "Optional")
                    p = p.with_changes(
                        annotation=cst.Annotation(annotation=cst.parse_expression(t)),
                        equal=(cst.AssignEqual() if p.default is not None else cst.MaybeSentinel.DEFAULT),
                    )
                    self.stats["params"] += 1
            out.append(p)
        return out

    def _infer_param(self, p: cst.Param) -> Optional[str]:
        if p.default is not None:
            t = infer_literal_type(p.default)
            if t == "None":
                base = NAME_HINTS.get(p.name.value) if self.name_heuristics else None
                return f"Optional[{base}]" if base else None
            return t
        if self.name_heuristics:
            return NAME_HINTS.get(p.name.value)
        return None


class _StripAnnotations(ast.NodeTransformer):
    def visit_arg(self, node: ast.arg) -> ast.arg:
        node.annotation = None
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self.generic_visit(node)
        node.returns = None
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Optional[ast.AST]:
        return None if node.module == "typing" else node


def _normalized_dump(tree: ast.AST) -> str:
    tree = _StripAnnotations().visit(tree)
    ast.fix_missing_locations(tree)
    return ast.dump(tree, include_attributes=False)


def validate(original: str, new: str, filename: str) -> None:
    try:
        new_tree = ast.parse(new, filename=filename)
    except SyntaxError as exc:
        msg = f"generated code does not parse: {exc}"
        raise ValueError(msg) from exc
    old_tree = ast.parse(original, filename=filename)
    if _normalized_dump(old_tree) != _normalized_dump(new_tree):
        msg = "generated code differs from the original beyond annotations"
        raise ValueError(msg)


def process_file(
    path: Path,
    *,
    dry_run: bool,
    show_diff: bool,
    backup: bool,
    add_returns: bool,
    name_heuristics: bool,
) -> int:
    source = path.read_text(encoding="utf-8")
    try:
        module = cst.parse_module(source)
    except cst.ParserSyntaxError as exc:
        print(f"[skip] {path}: cannot parse with libcst: {exc}", file=sys.stderr)
        return 1
    context = CodemodContext(filename=str(path))
    transformer = AnnotateTransformer(context, add_returns=add_returns, name_heuristics=name_heuristics)
    new_module = module.visit(transformer)
    new_module = AddImportsVisitor(context).transform_module(new_module)
    new_source = new_module.code
    if new_source == source:
        print(f"[ok]   {path}: nothing to do")
        return 0
    try:
        validate(source, new_source, str(path))
    except ValueError as exc:
        print(f"[FAIL] {path}: {exc} — file NOT modified", file=sys.stderr)
        return 1
    if show_diff:
        sys.stdout.writelines(
            difflib.unified_diff(
                source.splitlines(keepends=True),
                new_source.splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
    s = transformer.stats
    if dry_run:
        print(f"[dry]  {path}: would add {s['params']} param + {s['returns']} return hints")
        return 0
    if backup:
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(new_source, encoding="utf-8")
    print(f"[done] {path}: added {s['params']} param + {s['returns']} return hints")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Add simple type hints to .py files in place (libcst).")
    ap.add_argument("files", nargs="+", type=Path, help=".py file(s) to annotate")
    ap.add_argument("--dry-run", action="store_true", help="don't write, just report")
    ap.add_argument("--diff", action="store_true", help="print a unified diff")
    ap.add_argument("--backup", action="store_true", help="write FILE.py.bak before overwriting")
    ap.add_argument("--no-returns", action="store_true", help="only annotate parameters")
    ap.add_argument(
        "--name-heuristics",
        action="store_true",
        help="also guess types from parameter names (name→str, count→int, ...)",
    )
    args = ap.parse_args(argv)
    rc = 0
    for f in args.files:
        if not f.is_file() or f.suffix != ".py":
            print(f"[skip] {f}: not a .py file", file=sys.stderr)
            rc = 1
            continue
        rc |= process_file(
            f,
            dry_run=args.dry_run,
            show_diff=args.diff,
            backup=args.backup,
            add_returns=not args.no_returns,
            name_heuristics=args.name_heuristics,
        )
    return rc


if __name__ == "__main__":
    sys.exit(main())
'''

_SOURCES['annotate'] = r'''#!/data/data/com.termux/files/usr/bin/env python
"""(No content in stream)

---"""

from __future__ import annotations

import argparse
import ast
import contextlib
import difflib
import multiprocessing as mp
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import libcst as cst
from libcst.codemod import CodemodContext
from libcst.codemod.visitors import ApplyTypeAnnotationsVisitor


class TypeshedSanitizer(cst.CSTTransformer):
    def leave_ImportFrom(self, original_node, updated_node):
        if original_node.module and cst.helpers.get_full_name_for_node(original_node.module) == "_typeshed":
            return cst.ImportFrom(
                module=cst.Name("typing"),
                names=[cst.ImportAlias(name=cst.Name("Any"))],
            )
        return updated_node

    def leave_Import(self, original_node, updated_node):
        names = []
        for alias in original_node.names:
            if cst.helpers.get_full_name_for_node(alias.name) == "_typeshed":
                names.append(alias.with_changes(name=cst.Name("typing")))
            else:
                names.append(alias)
        return updated_node.with_changes(names=names)

    def leave_Attribute(self, original_node, updated_node):
        if (
            isinstance(original_node.value, cst.Name)
            and original_node.value.value == "_typeshed"
            and original_node.attr.value == "Incomplete"
        ):
            return cst.Attribute(value=cst.Name("typing"), attr=cst.Name("Any"))
        return updated_node

    def leave_Name(self, original_node, updated_node):
        if original_node.value == "Incomplete":
            return cst.Name("Any")
        return updated_node


def sanitize_stub_cst(stub_cst: cst.Module) -> cst.Module:
    return stub_cst.visit(TypeshedSanitizer())


def apply_type_annotations(
    source_code: str,
    stub_code: str,
    overwrite_existing: bool = True,
    use_future_annotations: bool = False,
) -> str:
    try:
        source_cst = cst.parse_module(source_code)
    except Exception as e:
        msg = f"Failed to parse source file with LibCST: {e}"
        raise ValueError(msg) from e
    try:
        stub_cst = cst.parse_module(stub_code)
    except Exception as e:
        msg = f"Failed to parse stub file with LibCST: {e}"
        raise ValueError(msg) from e
    stub_cst = sanitize_stub_cst(stub_cst)
    context = CodemodContext()
    ApplyTypeAnnotationsVisitor.store_stub_in_context(
        context=context,
        stub=stub_cst,
        overwrite_existing_annotations=overwrite_existing,
        use_future_annotations=use_future_annotations,
    )
    transformer = ApplyTypeAnnotationsVisitor(context)
    annotated_cst = transformer.transform_module(source_cst)
    return annotated_cst.code


_PY_LANGUAGE = None
_TS_PARSER_FACTORY = None


def _ensure_tree_sitter() -> None:
    """Load a Python grammar, preferring the maintained ``tree_sitter_python`` wheel.

    The original only knew about ``tree_sitter_languages``, a package that is no
    longer maintained and does not build against current tree-sitter releases;
    it is kept as a fallback so old environments keep working.
    """
    global _PY_LANGUAGE, _TS_PARSER_FACTORY
    if _PY_LANGUAGE is not None:
        return
    try:
        from tree_sitter import Language, Parser
    except Exception as exc:
        msg = "tree-sitter not installed (pip install tree-sitter)"
        raise RuntimeError(msg) from exc
    try:
        import tree_sitter_python

        _PY_LANGUAGE = Language(tree_sitter_python.language())
    except Exception:
        try:
            from tree_sitter_languages import get_language

            _PY_LANGUAGE = get_language("python")
        except Exception as exc:
            msg = (
                "no Python grammar available: install tree-sitter-python "
                "(pip install tree-sitter tree-sitter-python)"
            )
            raise RuntimeError(msg) from exc
    _TS_PARSER_FACTORY = Parser


def _make_parser():
    """Return a parser for the loaded grammar, across tree-sitter API versions."""
    _ensure_tree_sitter()
    assert _PY_LANGUAGE is not None
    try:
        return _TS_PARSER_FACTORY(_PY_LANGUAGE)
    except TypeError:  # tree-sitter < 0.22 wants the language set afterwards
        parser = _TS_PARSER_FACTORY()
        parser.set_language(_PY_LANGUAGE)
        return parser


TYPE_COMMENT_RE = re.compile(r"\s*#\s*type\s*:\s*([^\n]*)$", flags=re.IGNORECASE)


def _prev_nonspace(buf: bytes, i: int) -> int:
    j = i - 1
    while j >= 0 and buf[j] in b" \t\r":
        j -= 1
    return j


def _next_nonspace(buf: bytes, i: int) -> int:
    n = len(buf)
    j = i
    while j < n and buf[j] in b" \t\r":
        j += 1
    return min(n, j)


def _collect_annotation_nodes(root):
    stack = [root]
    ann_nodes = []
    while stack:
        node = stack.pop()
        if node.type == "annotation":
            ann_nodes.append(node)
        for c in node.children:
            stack.append(c)
    return ann_nodes


def _remove_ranges_from_bytes(src: bytes, ranges: list[tuple[int, int]]) -> bytes:
    if not ranges:
        return src
    ranges_sorted = sorted(ranges, key=lambda r: r[0])
    merged = []
    cur_s, cur_e = ranges_sorted[0]
    for s, e in ranges_sorted[1:]:
        if s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            merged.append((cur_s, cur_e))
            cur_s, cur_e = s, e
    merged.append((cur_s, cur_e))
    out = bytearray(src)
    for s, e in reversed(merged):
        del out[s:e]
    return bytes(out)


def strip_annotations_from_bytes(src_bytes: bytes, path: Path) -> tuple[bytes, list[str], Optional[str]]:
    parser = _make_parser()
    try:
        tree = parser.parse(src_bytes)
    except Exception as e:
        return src_bytes, [], f"parse error: {e}"
    root = tree.root_node
    ann_nodes = _collect_annotation_nodes(root)
    remove_ranges: list[tuple[int, int]] = []
    warnings: list[str] = []
    for node in ann_nodes:
        s = node.start_byte
        e = node.end_byte
        prev_i = _prev_nonspace(src_bytes, s)
        if prev_i >= 1 and src_bytes[prev_i - 1 : prev_i + 1] == b"->":
            removed_prefix_start = prev_i - 1
        elif prev_i >= 0 and src_bytes[prev_i] == ord(":"):
            removed_prefix_start = prev_i
        else:
            removed_prefix_start = s
        next_i = _next_nonspace(src_bytes, e)
        next_char = src_bytes[next_i : next_i + 1] if next_i < len(src_bytes) else b""
        safe_next = next_char in (b"=", b",", b")", b":")
        if src_bytes[removed_prefix_start : removed_prefix_start + 2] == b"->":
            safe = True
        else:
            safe = safe_next
        if not safe:
            line_start = src_bytes.rfind(b"\n", 0, s) + 1
            line_end = src_bytes.find(b"\n", e)
            if line_end == -1:
                line_end = len(src_bytes)
            snippet = src_bytes[line_start:line_end].decode(errors="replace").strip()
            warnings.append(f'skipped standalone annotation at {path}:{node.start_point[0] + 1}: "{snippet}"')
            continue
        remove_ranges.append((removed_prefix_start, e))
    type_comment_ranges: list[tuple[int, int]] = []
    lines = src_bytes.splitlines(keepends=True)
    offset = 0
    for ln in lines:
        try:
            text = ln.decode()
        except Exception:
            offset += len(ln)
            continue
        m = TYPE_COMMENT_RE.search(text)
        if m:
            byte_start = offset + len(text[: m.start(0)].encode())
            byte_end = offset + len(text[: m.end(0)].encode())
            type_comment_ranges.append((byte_start, byte_end))
        offset += len(ln)
    all_remove = remove_ranges + type_comment_ranges
    if not all_remove:
        return src_bytes, warnings, None
    new_bytes = _remove_ranges_from_bytes(src_bytes, all_remove)
    return new_bytes, warnings, None


def validate_python_code(code: str, filename: str) -> None:
    try:
        ast.parse(code, filename=filename)
    except SyntaxError as e:
        msg = f"Invalid Python syntax at line {e.lineno}, col {e.offset}: {e.msg}"
        raise SyntaxError(msg) from e


def compute_diff(original: str, modified: str, filename: str) -> str:
    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        modified.splitlines(keepends=True),
        fromfile=f"{filename} (original)",
        tofile=f"{filename} (modified)",
    )
    return "".join(diff)


def _run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, check=False)


@dataclass
class Options:
    dry_run: bool = False
    backup: bool = False
    show_diff: bool = False
    quiet: bool = False
    validate_compile: bool = False
    validate_mypy: bool = False
    overwrite_existing: bool = True
    use_future_annotations: bool = False
    stub_file: Optional[str] = None
    remove: bool = False


@dataclass
class Result:
    path: Path
    changed: bool = False
    error: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    diff: str = ""
    backup_path: Optional[Path] = None
    mode: str = "annotate"


def _annotate_file(path: Path, options: Options) -> Result:
    result = Result(path=path, mode="annotate")
    if options.stub_file:
        stub_path = Path(options.stub_file)
    else:
        stub_path = path.with_suffix(".pyi")
    if not stub_path.is_file():
        result.error = f"missing stub file: {stub_path}"
        return result
    try:
        original = path.read_text(encoding="utf-8")
    except Exception as e:
        result.error = f"read error: {e}"
        return result
    try:
        stub_code = stub_path.read_text(encoding="utf-8")
    except Exception as e:
        result.error = f"stub read error: {e}"
        return result
    try:
        validate_python_code(original, str(path))
    except SyntaxError as e:
        result.error = f"source has invalid syntax: {e}"
        return result
    try:
        modified = apply_type_annotations(
            source_code=original,
            stub_code=stub_code,
            overwrite_existing=options.overwrite_existing,
            use_future_annotations=options.use_future_annotations,
        )
    except Exception as e:
        result.error = f"annotation failed: {e}"
        return result
    try:
        validate_python_code(modified, str(path))
    except SyntaxError as e:
        result.error = f"result has invalid syntax: {e}"
        return result
    if options.validate_compile:
        try:
            compile(modified, str(path), "exec")
        except SyntaxError as e:
            result.error = f"compile() validation failed: {e}"
            return result
    result.changed = modified != original
    result.diff = compute_diff(original, modified, str(path))
    return result


def _strip_file(path: Path, options: Options) -> Result:
    result = Result(path=path, mode="remove")
    try:
        src_bytes = path.read_bytes()
    except Exception as e:
        result.error = f"read error: {e}"
        return result
    new_bytes, warnings, err = strip_annotations_from_bytes(src_bytes, path)
    result.warnings.extend(warnings)
    if err:
        result.error = err
        return result
    if new_bytes == src_bytes:
        return result
    try:
        new_text = new_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        result.error = f"decoded result is not utf-8: {e}"
        return result
    if options.validate_compile or options.validate_mypy:
        try:
            validate_python_code(new_text, str(path))
        except SyntaxError as e:
            result.error = f"result has invalid syntax: {e}"
            return result
    if options.validate_compile:
        try:
            compile(new_text, str(path), "exec")
        except SyntaxError as e:
            result.error = f"compile() validation failed: {e}"
            return result
    result.changed = True
    if options.show_diff:
        try:
            original_text = src_bytes.decode("utf-8")
        except UnicodeDecodeError:
            original_text = src_bytes.decode("utf-8", errors="replace")
        result.diff = compute_diff(original_text, new_text, str(path))
    result._new_text = new_text
    return result


def process_file(path_str: str, options: Options) -> Result:
    path = Path(path_str)
    if not path.exists() or not path.is_file():
        return Result(
            path=path,
            error="file not found",
            mode=("remove" if options.remove else "annotate"),
        )
    if path.suffix != ".py":
        return Result(
            path=path,
            error="not a .py file",
            mode=("remove" if options.remove else "annotate"),
        )
    result = _strip_file(path, options) if options.remove else _annotate_file(path, options)
    if result.error or not result.changed or options.dry_run:
        return result
    if options.backup:
        try:
            backup_path = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(path, backup_path)
            result.backup_path = backup_path
        except Exception as e:
            result.warnings.append(f"failed to create backup: {e}")
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        if hasattr(result, "_new_text"):
            tmp_path.write_text(result._new_text, encoding="utf-8")
        else:
            original = path.read_text(encoding="utf-8")
            stub_path = Path(options.stub_file) if options.stub_file else path.with_suffix(".pyi")
            stub_code = stub_path.read_text(encoding="utf-8")
            new_text = apply_type_annotations(
                source_code=original,
                stub_code=stub_code,
                overwrite_existing=options.overwrite_existing,
                use_future_annotations=options.use_future_annotations,
            )
            tmp_path.write_text(new_text, encoding="utf-8")
        try:
            st = path.stat()
            os.chmod(tmp_path, stat.S_IMODE(st.st_mode))
        except Exception:
            pass
        tmp_path.replace(path)
    except Exception as e:
        with contextlib.suppress(Exception):
            tmp_path.unlink(missing_ok=True)
        result.error = f"write error: {e}"
        return result
    if options.validate_mypy:
        res = _run_cmd([sys.executable, "-m", "mypy", "--no-incremental", str(path)])
        if res.returncode != 0:
            result.warnings.append(f"mypy reported issues:\n{res.stdout}{res.stderr}")
    return result


def gather_py_files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    provided = list(paths) if paths else ["."]
    for p in provided:
        path = Path(p)
        if path.is_file():
            if path.suffix == ".py":
                out.append(path.resolve())
        elif path.is_dir():
            for f in path.rglob("*.py"):
                if f.is_file():
                    out.append(f.resolve())
        else:
            for f in Path().glob(p):
                if f.is_file() and f.suffix == ".py":
                    out.append(f.resolve())
    return sorted(set(out))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="batch_annotate",
        description=(
            "Add OR remove type annotations on Python (.py) files.\n\n"
            "  Default mode (annotate): uses a sibling .pyi stub (LibCST).\n"
            "                           A stub is REQUIRED for every target file.\n"
            "  --remove     mode:       strips all annotations + `# type:` comments\n"
            "                           (tree-sitter). No stub needed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="Files, directories, or globs to process (default: current directory).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "-r",
        "--remove",
        "--strip",
        dest="remove",
        action="store_true",
        help="Remove all type annotations (and `# type:` comments) instead of adding them.",
    )
    parser.add_argument(
        "-s",
        "--stub-file",
        default=None,
        help="Custom .pyi stub (annotate mode only, single input file only).",
    )
    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="(annotate) Do not overwrite existing type annotations.",
    )
    parser.add_argument(
        "--future-annotations",
        action="store_true",
        help="(annotate) Emit `from __future__ import annotations`.",
    )
    parser.add_argument(
        "-b",
        "--backup",
        action="store_true",
        help="Write a '<file>.py.bak' backup before modifying.",
    )
    parser.add_argument(
        "-d",
        "--diff",
        action="store_true",
        help="Show a unified diff for each modified file.",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Report what would change without writing anything.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress normal output (errors are still printed).",
    )
    parser.add_argument(
        "--validate-compile",
        action="store_true",
        help="Also run the built-in compile() on the modified output.",
    )
    parser.add_argument(
        "--validate-mypy",
        action="store_true",
        help="After writing, run mypy on the updated file (slow, annotate mode).",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=None,
        help="Number of parallel worker processes (default: min(8, n_files)).",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    files = gather_py_files(args.paths)
    if not files:
        print("No .py files found.", file=sys.stderr)
        return 1
    if args.stub_file and len(files) > 1:
        print(
            "Error: --stub-file is only valid with a single input file.",
            file=sys.stderr,
        )
        return 2
    if args.stub_file and args.remove:
        print("Error: --stub-file is not valid with --remove.", file=sys.stderr)
        return 2
    if args.future_annotations and args.remove:
        print("Error: --future-annotations is not valid with --remove.", file=sys.stderr)
        return 2
    if args.no_overwrite and args.remove:
        print("Error: --no-overwrite is not valid with --remove.", file=sys.stderr)
        return 2
    options = Options(
        dry_run=args.dry_run,
        backup=args.backup,
        show_diff=args.diff,
        quiet=args.quiet,
        validate_compile=args.validate_compile,
        validate_mypy=args.validate_mypy,
        overwrite_existing=not args.no_overwrite,
        use_future_annotations=args.future_annotations,
        stub_file=args.stub_file,
        remove=args.remove,
    )
    if options.remove:
        try:
            _ensure_tree_sitter()
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 3
    use_pool = len(files) > 1 and (args.jobs is None or args.jobs > 1)
    if use_pool:
        jobs = args.jobs or min(8, len(files))
        with mp.Pool(processes=jobs) as pool:
            results = pool.starmap(process_file, [(str(f), options) for f in files])
    else:
        results = [process_file(str(f), options) for f in files]
    changed = [r for r in results if r.changed and not r.error]
    unchanged = [r for r in results if not r.changed and not r.error]
    failed = [r for r in results if r.error]
    warnings = [w for r in results for w in r.warnings]
    verb = "strip" if options.remove else "annotate"
    if not args.quiet:
        for r in changed:
            if args.dry_run:
                print(f"[dry-run] would {verb}: {r.path}")
            else:
                print(f"{'stripped' if options.remove else 'updated'}: {r.path}")
                if r.backup_path is not None:
                    print(f"  backup: {r.backup_path}")
        for r in unchanged:
            print(f"no-change: {r.path}")
    for r in failed:
        print(f"error: {r.path} -> {r.error}", file=sys.stderr)
    if args.diff:
        for r in changed:
            if r.diff:
                print("\n" + "=" * 60)
                print(f"DIFF: {r.path}")
                print("=" * 60)
                print(r.diff, end="")
                print("=" * 60 + "\n")
    if warnings and not args.quiet:
        print("\nWarnings:")
        for w in warnings:
            print(f"  - {w}")
    if not args.quiet:
        print(
            f"\nSummary: mode={verb} processed={len(results)} "
            f"changed={len(changed)} "
            f"no-change={len(unchanged)} "
            f"errors={len(failed)} "
            f"warnings={len(warnings)}"
        )
    return 0 if not failed else 2


if __name__ == "__main__":
    sys.exit(main())
'''

_SOURCES['unannotate'] = r'''#!/data/data/com.termux/files/usr/bin/env python
"""Strip type annotations from Python files (libcst based).

Fixed during consolidation:
  * ``from dh import SKIP_DIR`` pointed at a helper module that does not exist in
    this repository; the skip-list is now defined locally.
  * ``def main(...)`` had its body glued onto the signature line, which made the
    whole module a SyntaxError.
  * added ``--dry-run``/``--backup`` (the tool used to rewrite files in place
    with no way to preview the change).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import multiprocessing as mp
import os
import shutil
import sys
import tokenize
from pathlib import Path
from typing import TYPE_CHECKING

import libcst as cst

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

WORKERS = 8
SKIP_DIR: frozenset[str] = frozenset({
    ".git",
    ".hg",
    ".svn",
    ".tox",
    ".nox",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    "dist",
    "build",
    "site-packages",
})


class TypeAnnotationRemover(cst.CSTTransformer):
    def leave_Param(self, original_node, updated_node):
        if updated_node.annotation is None:
            return updated_node
        return updated_node.with_changes(annotation=None)

    def leave_FunctionDef(self, original_node, updated_node):
        changes = {}
        if updated_node.returns is not None:
            changes["returns"] = None
        if getattr(updated_node, "type_parameters", None) is not None:
            changes["type_parameters"] = None
        if not changes:
            return updated_node
        return updated_node.with_changes(**changes)

    def leave_ClassDef(self, original_node, updated_node):
        if getattr(updated_node, "type_parameters", None) is not None:
            return updated_node.with_changes(type_parameters=None)
        return updated_node

    def leave_AnnAssign(self, original_node, updated_node):
        if updated_node.value is None:
            return cst.RemoveFromParent()
        return cst.Assign(
            targets=[cst.AssignTarget(target=updated_node.target)],
            value=updated_node.value,
            semicolon=updated_node.semicolon,
        )

    def leave_SimpleStatementLine(self, original_node, updated_node):
        if not updated_node.body:
            return cst.RemoveFromParent()
        return updated_node

    def leave_SimpleStatementSuite(self, original_node, updated_node):
        if not updated_node.body:
            return updated_node.with_changes(body=[cst.Pass()])
        return updated_node

    def leave_IndentedBlock(self, original_node, updated_node):
        if not updated_node.body:
            return updated_node.with_changes(body=[cst.SimpleStatementLine(body=[cst.Pass()])])
        return updated_node


def _read_source(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
    return raw.decode(encoding), encoding


def _atomic_write(path: Path, text: str, encoding: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding=encoding)
        with contextlib.suppress(OSError):
            os.chmod(tmp, path.stat().st_mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            with contextlib.suppress(OSError):
                tmp.unlink()


def process_file(path_str: str, dry_run: bool = False, backup: bool = False) -> tuple[str, str | None, bool]:
    path = Path(path_str)
    try:
        source, encoding = _read_source(path)
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        return path_str, f"read failed: {exc}", False
    try:
        module = cst.parse_module(source)
    except cst.ParserSyntaxError as exc:
        return path_str, f"parse failed: {exc}", False
    except Exception as exc:
        return path_str, f"parse failed: {exc!r}", False
    try:
        new_code = module.visit(TypeAnnotationRemover()).code
    except Exception as exc:
        return path_str, f"transform failed: {exc!r}", False
    if new_code == source:
        return path_str, None, False
    try:
        compile(new_code, str(path), "exec")
    except (SyntaxError, ValueError) as exc:
        return path_str, f"validation failed: {exc}", False
    try:
        if backup:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        if not dry_run:
            _atomic_write(path, new_code, encoding)
    except OSError as exc:
        return path_str, f"write failed: {exc}", False
    return path_str, None, True


def _iter_python_files(root: Path) -> Iterable[Path]:
    for candidate in root.rglob("*.py"):
        if not candidate.is_file():
            continue
        if any(part in SKIP_DIR for part in candidate.parts):
            continue
        yield candidate


def collect_files(inputs: Sequence[str]) -> list[Path]:
    roots: list[Path] = [Path(p) for p in inputs] if inputs else [Path.cwd()]
    seen: set[Path] = set()
    files: list[Path] = []
    for root in roots:
        if root.is_dir():
            candidates: Iterable[Path] = _iter_python_files(root)
        elif root.is_file():
            if root.suffix != ".py":
                print(f"warning: not a .py file: {root}", file=sys.stderr)
                continue
            candidates = (root,)
        else:
            print(f"warning: path not found: {root}", file=sys.stderr)
            continue
        for path in candidates:
            try:
                key = path.resolve()
            except OSError:
                key = path
            if key in seen:
                continue
            seen.add(key)
            files.append(path)
    files.sort(key=str)
    return files


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="clean_type_annotations",
        description="Remove type annotations (params, returns, AnnAssign, type params) from Python files.",
    )
    parser.add_argument("paths", nargs="*", help="Files or directories (default: the current directory).")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Report what would change, write nothing.")
    parser.add_argument("-b", "--backup", action="store_true", help="Keep a .bak copy before rewriting.")
    namespace = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    args: list[str] = list(namespace.paths)
    files = collect_files(args)
    if not files:
        print("No Python files to process.", file=sys.stderr)
        return 0
    print(
        f"Processing {len(files)} file(s) with {WORKERS} worker(s)...",
        file=sys.stderr,
    )
    tasks = [(str(p), namespace.dry_run, namespace.backup) for p in files]
    chunksize = max(1, len(tasks) // (WORKERS * 4))
    with mp.Pool(processes=WORKERS) as pool:
        results = pool.starmap(process_file, tasks, chunksize=chunksize)
    changed = 0
    unchanged = 0
    errors = 0
    for path_str, error, was_changed in results:
        if error is not None:
            errors += 1
            print(f"ERROR {path_str}: {error}", file=sys.stderr)
        elif was_changed:
            changed += 1
        else:
            unchanged += 1
    suffix = " (dry-run: nothing written)" if namespace.dry_run else ""
    print(
        f"Done: {changed} modified, {unchanged} unchanged, {errors} error(s){suffix}.",
        file=sys.stderr,
    )
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
