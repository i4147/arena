#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import ast
import builtins
import keyword
import string
import subprocess
import sys
from collections import Counter
from itertools import count, product
from pathlib import Path

TEXT1 = "Return full type-annotated code in one markdown block, no docstrings & no comments"
USAGE = """\
Usage: {prog} <path> [start [end]] [-m|--mangle | -x]
       {prog} <path> -s <line> [<line> ...] [-m|--mangle | -x]
  start/end  1-based inclusive line range (end defaults to last line)
  -s         copy only the listed lines, in the order given
  -m,--mangle
             compress Python code (strip docstrings/comments/annotations,
             shorten local names) and append a usage prompt.
  -x         strip comments, docstrings, shebang, and __future__ imports
             from a Python file using libcst (formatting preserved).
             Requires libcst; only applies to Python files.
Without -m/-x, content is copied as-is."""
RESERVED = set(dir(builtins)) | set(keyword.kwlist)
SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
BLOCK_NODES = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.ExceptHandler,
)


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def send_to_clipboard(text: str) -> bool:
    try:
        proc = subprocess.run(
            ["termux-clipboard-set"],
            input=text.encode("utf-8"),
            capture_output=True,
        )
    except FileNotFoundError:
        print(
            "Warning: 'termux-clipboard-set' not found; skipping clipboard copy. "
            "Install the Termux:API app and run: pkg install termux-api",
            file=sys.stderr,
        )
        return False
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").strip()
        print(f"Warning: failed to copy to clipboard: {err}", file=sys.stderr)
        return False
    return True


class Stripper(ast.NodeTransformer):
    def _drop_docstring(self, node) -> None:
        if node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                node.body = node.body[1:]

    def _scope(self, node):
        self._drop_docstring(node)
        return self.generic_visit(node)

    visit_Module = _scope
    visit_ClassDef = _scope

    def visit_FunctionDef(self, node):
        node.returns = None
        return self._scope(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_arg(self, node):
        node.annotation = None
        return node

    def visit_AnnAssign(self, node):
        if node.value is None:
            return None
        return ast.copy_location(ast.Assign(targets=[node.target], value=node.value), node)

    def visit_ImportFrom(self, node):
        if node.module == "__future__":
            return None
        return node


def _fill_empty_blocks(tree: ast.AST) -> None:
    for node in ast.walk(tree):
        if isinstance(node, BLOCK_NODES) and not node.body:
            node.body = [ast.Pass()]


def _all_identifiers(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, SCOPE_NODES):
            names.add(node.name)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.update(node.name.split("."))
            if node.asname:
                names.add(node.asname)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
    return names


def _unsafe_names(tree: ast.AST) -> set[str]:
    unsafe: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            unsafe.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            unsafe.add(node.arg)
        elif isinstance(node, ast.alias):
            unsafe.update(node.name.split("."))
            if node.asname:
                unsafe.add(node.asname)
        elif isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if isinstance(stmt, SCOPE_NODES):
                    unsafe.add(stmt.name)
                elif isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        for n in ast.walk(target):
                            if isinstance(n, ast.Name):
                                unsafe.add(n.id)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            unsafe.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            unsafe.add(node.rest)
    return unsafe


def _name_stats(tree: ast.AST) -> tuple[set[str], Counter]:
    defined: set[str] = set()
    freq: Counter = Counter()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            freq[node.id] += 1
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                defined.add(node.id)
        elif isinstance(node, ast.arg):
            freq[node.arg] += 1
            defined.add(node.arg)
        elif isinstance(node, SCOPE_NODES):
            freq[node.name] += 1
            defined.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            freq[node.name] += 1
            defined.add(node.name)
    return defined, freq


def _short_names(taken: set[str]):
    for size in count(1):
        for chars in product(string.ascii_letters, repeat=size):
            name = "".join(chars)
            if name not in taken:
                yield name


class Renamer(ast.NodeTransformer):
    def __init__(self, mapping: dict[str, str]):
        self.m = mapping

    def _r(self, name: str) -> str:
        return self.m.get(name, name)

    def visit_Name(self, node):
        node.id = self._r(node.id)
        return node

    def visit_arg(self, node):
        node.arg = self._r(node.arg)
        return self.generic_visit(node)

    def visit_FunctionDef(self, node):
        node.name = self._r(node.name)
        return self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        node.name = self._r(node.name)
        return self.generic_visit(node)

    def visit_ExceptHandler(self, node):
        if node.name:
            node.name = self._r(node.name)
        return self.generic_visit(node)

    def visit_Global(self, node):
        node.names = [self._r(n) for n in node.names]
        return node

    visit_Nonlocal = visit_Global


def rename_identifiers(tree: ast.AST) -> ast.AST:
    defined, freq = _name_stats(tree)
    unsafe = _unsafe_names(tree)
    targets = [n for n in defined if n not in unsafe and n not in RESERVED and not n.startswith("__")]
    targets.sort(key=lambda n: (-freq[n], n))
    taken = _all_identifiers(tree) | RESERVED | unsafe
    gen = _short_names(taken)
    mapping = {old: next(gen) for old in targets}
    return Renamer(mapping).visit(tree)


def _shrink_indent(code: str) -> str:
    out = []
    for line in code.splitlines():
        stripped = line.lstrip(" ")
        if stripped:
            out.append(" " * ((len(line) - len(stripped)) // 4) + stripped)
    return "\n".join(out) + "\n" if out else ""


def compress_source(source: str) -> str | None:
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    tree = Stripper().visit(tree)
    _fill_empty_blocks(tree)
    tree = rename_identifiers(tree)
    code = _shrink_indent(ast.unparse(tree))
    try:
        compile(code, "<compressed>", "exec")
    except SyntaxError:
        return None
    return code


def is_python_file(path: Path, source: str) -> bool:
    if path.suffix.lower() == ".py":
        return True
    if path.suffix == "":
        try:
            ast.parse(source)
            return True
        except (SyntaxError, ValueError):
            return False
    return False


def strip_x(source: str) -> str | None:
    try:
        import libcst as cst
        from libcst import RemovalSentinel
    except ImportError:
        return None

    def is_string_expr(expr) -> bool:
        return isinstance(expr.value, (cst.SimpleString, cst.ConcatenatedString))

    def is_docstring_stmt(stmt) -> bool:
        if not isinstance(stmt, cst.SimpleStatementLine) or len(stmt.body) != 1:
            return False
        s = stmt.body[0]
        return isinstance(s, cst.Expr) and is_string_expr(s)

    def is_docstring_small(s) -> bool:
        return isinstance(s, cst.Expr) and is_string_expr(s)

    def is_future_import_line(stmt) -> bool:
        if not isinstance(stmt, cst.SimpleStatementLine) or len(stmt.body) != 1:
            return False
        s = stmt.body[0]
        if not isinstance(s, cst.ImportFrom):
            return False
        m = s.module
        return isinstance(m, cst.Name) and m.value == "__future__"

    def strip_suite(suite):
        if isinstance(suite, cst.IndentedBlock):
            body = list(suite.body)
            if body and is_docstring_stmt(body[0]):
                body = body[1:]
            if not body:
                body = [cst.SimpleStatementLine(body=[cst.Pass()])]
            return suite.with_changes(body=body)
        if isinstance(suite, cst.SimpleStatementSuite):
            body = list(suite.body)
            if body and is_docstring_small(body[0]):
                body = body[1:]
            if not body:
                body = [cst.Pass()]
            return suite.with_changes(body=body)
        return suite

    class XStripper(cst.CSTTransformer):
        def leave_TrailingWhitespace(self, orig, updated):
            if updated.comment is not None:
                return updated.with_changes(
                    comment=None,
                    whitespace=cst.SimpleWhitespace(""),
                )
            return updated

        def leave_EmptyLine(self, orig, updated):
            return RemovalSentinel.REMOVE

        def leave_Module(self, orig, updated):
            body = list(updated.body)
            if body and is_docstring_stmt(body[0]):
                body = body[1:]
            body = [s for s in body if not is_future_import_line(s)]
            return updated.with_changes(
                body=body,
                header=[],
                footer=[],
            )

        def leave_ClassDef(self, orig, updated):
            return updated.with_changes(body=strip_suite(updated.body))

        def leave_FunctionDef(self, orig, updated):
            return updated.with_changes(body=strip_suite(updated.body))

        leave_AsyncFunctionDef = leave_FunctionDef

    try:
        module = cst.parse_module(source)
        return module.visit(XStripper()).code
    except Exception:
        return None


def build_payload(
    path: Path,
    source: str,
    ranged: bool,
    mangle: bool,
    strip: bool,
) -> str:
    if strip:
        if not is_python_file(path, source):
            print("Note: -x only applies to Python files; copied as-is.", file=sys.stderr)
            return source
        cleaned = strip_x(source)
        if cleaned is None:
            print(
                "Warning: libcst unavailable or parse failed; copied as-is. Install with: pip install libcst",
                file=sys.stderr,
            )
            return source
        return cleaned.rstrip("\n") + "\n\n" + TEXT1 + "\n"
    if not mangle:
        return source
    if ranged or is_python_file(path, source):
        compressed = compress_source(source)
        if compressed is not None:
            return compressed.rstrip("\n") + "\n\n" + TEXT1 + "\n"
        if ranged:
            print("Note: selection is not valid Python; copied as-is.", file=sys.stderr)
        else:
            print("Warning: could not parse file; copied as-is.", file=sys.stderr)
    return source


def main(argv: list[str]) -> None:
    raw = argv[1:]
    mangle = False
    strip = False
    rest: list[str] = []
    for a in raw:
        if a in ("-m", "--mangle"):
            mangle = True
        elif a == "-x":
            strip = True
        else:
            rest.append(a)
    if mangle and strip:
        die("Error: -m/--mangle and -x are mutually exclusive.")
    args = [a for a in rest if a != "-s"]
    selective = len(args) != len(rest)
    if not args:
        die(USAGE.format(prog=Path(argv[0]).name))
    path = Path(args[0])
    if not path.is_file():
        die(f"Error: File not found at '{path}'")
    try:
        nums = [int(a) for a in args[1:]]
    except ValueError:
        die("Error: line numbers must be integers.")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        die(f"Error reading '{path}': {e}")
    lines = text.splitlines(keepends=True)
    total = len(lines)
    if selective:
        if not nums:
            die("Error: -s needs at least one line number.")
        bad = [n for n in nums if not 1 <= n <= total]
        if bad:
            die(f"Error: line(s) {bad} out of range. File has {total} lines.")
        source, ranged = "".join(lines[n - 1] for n in nums), True
    elif not nums:
        source, ranged = text, False
    elif len(nums) <= 2:
        start = nums[0]
        end = nums[1] if len(nums) == 2 else total
        if not 1 <= start <= total:
            die(f"Error: start line ({start}) out of range. File has {total} lines.")
        if not start <= end <= total:
            die(f"Error: end line ({end}) must be between start line and {total}.")
        source, ranged = "".join(lines[start - 1 : end]), True
    else:
        die(USAGE.format(prog=Path(argv[0]).name))
    if not source:
        die("No content selected to copy.")
    payload = build_payload(path, source, ranged, mangle, strip)
    ok = send_to_clipboard(payload)
    status = "clipboard OK" if ok else "clipboard unavailable"
    print(
        f"Processed {len(payload)} chars (original {len(source)}); {status}.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
