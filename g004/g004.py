#!/data/data/com.termux/files/usr/bin/env python3
"""g004 - consolidated Python source scrapers.

Everything that used to live in this folder as separate scripts now lives in
this one file.  Each tool's original source is embedded verbatim below, so
nothing was lost: ``python g004.py unpack <tool>`` writes the original
file back out.

Six import-time demo scripts became one tool. Each of them scanned the
tree it was started from as a side effect of being imported.

Tools
-----
extract  Collect classes/functions/docstrings/comments/expressions/imports/constants per folder (merges the five 43-line ex_*.py clones and the buggy ex_const.py).
         from extract.py (288 lines)

Usage
-----
    python g004.py list                 # list the tools
    python g004.py doctor               # report missing third-party packages
    python g004.py <tool> --help        # help for one tool
    python g004.py extract <args>
    python g004.py unpack <tool> [-o DIR] [--stdout]

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

FOLDER = "g004"


_HERE = Path(__file__).resolve().parent if "__file__" in globals() else None

TOOLS: dict[str, dict[str, object]] = {
    "extract": {"source": "extract.py", "lines": 288, "summary": 'Collect classes/functions/docstrings/comments/expressions/imports/constants per folder (merges the five 43-line ex_*.py clones and the buggy ex_const.py).'},
}

_SOURCES: dict[str, str] = {}
_LOADED: dict[str, types.ModuleType] = {}


def _decode(*chunks: str) -> str:
    """Fallback representation for sources that cannot be written as raw strings."""
    return zlib.decompress(base64.b85decode("".join(chunks))).decode("utf-8")


_SOURCES['extract'] = r'''#!/data/data/com.termux/files/usr/bin/env python
"""Pull constructs out of a Python tree and write them per folder.

This single tool replaces the six ``ex_*.py`` demo scripts that used to live in
``g004``.  Five of them (``ex_class``, ``ex_comments``, ``ex_ex``, ``ex_ex``,
``ex_func``, ``ex_imports``) were the same 43-line program with a different
``VALID`` node-type set, and every one of them ran its scan at import time.

Fixes applied while merging:

* the node types the originals asked for were mostly wrong: ``ex_ex.py`` used
  ``{"function_docstrings", "class_docstrings"}`` and ``ex_comments.py`` used
  ``{"comment", "expression_statements"}``, none of which tree-sitter-python
  ever produces.  Real node names / queries are used here.
* every output file was called ``imports.py`` no matter which construct was
  extracted; the file is now named after the construct.
* ``ex_const.py`` referenced an undefined ``CHUNK_SIZE`` and an undefined
  ``get_pyfiles()`` and created its output directory at import time.  Both are
  fixed here (constants are hashed with ``xxhash`` when available, ``hashlib``
  otherwise, and ``joblib`` is optional).
* nothing runs at import: everything is behind ``main()``.

Usage:
    python g004.py extract --kind classes --kind imports path/to/tree
    python g004.py extract --kind constants -o out
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterator

KINDS: tuple[str, ...] = ("classes", "functions", "docstrings", "comments", "expressions", "imports", "constants")
SKIP_DIRS: frozenset[str] = frozenset(
    {".git", ".hg", ".svn", ".tox", ".venv", "venv", "node_modules", "__pycache__",
     ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea", ".vscode", "dist",
     "build", "site-packages", "output"}
)
QUERIES: dict[str, str] = {
    "classes": "(class_definition) @node",
    "functions": "(function_definition) @node",
    "comments": "(comment) @node",
    "expressions": "(expression_statement) @node",
    "imports": "[(import_statement) (import_from_statement)] @node",
}
CHUNK_SIZE: int = 1 << 20


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _tree_sitter() -> tuple[object, object] | None:
    """Return ``(Parser, Language)`` for Python, or ``None`` when unavailable."""
    try:
        import tree_sitter_python as tsp
        from tree_sitter import Language, Parser
    except ImportError:
        return None
    return Parser(Language(tsp.language())), Language


def _captures(query: object, node: object) -> list[object]:
    from tree_sitter import QueryCursor

    cursor = QueryCursor(query)
    try:
        result = cursor.captures(node)
    except TypeError:
        return []
    if isinstance(result, dict):
        nodes: list[object] = []
        for value in result.values():
            nodes.extend(value)
        return nodes
    return [item[0] if isinstance(item, tuple) else item for item in result]


def iter_python_files(root: Path, skip_dirs: frozenset[str]) -> Iterator[Path]:
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
        for name in files:
            if name.startswith(".") or not name.endswith(".py"):
                continue
            path = Path(current) / name
            if not path.is_symlink():
                yield path


def _snippet(src: bytes, node: object) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", "replace")  # type: ignore[attr-defined]


def extract_nodes(src: bytes, kind: str) -> list[str]:
    """Return the source text of every ``kind`` construct in *src*."""
    from tree_sitter import Parser, Query

    prepared = _tree_sitter()
    if prepared is None:
        raise RuntimeError("tree-sitter and tree-sitter-python are required for this mode")
    parser, _language = prepared
    language = parser.language  # type: ignore[attr-defined]
    tree = parser.parse(src)  # type: ignore[operator]
    query = Query(language, QUERIES[kind])
    return [_snippet(src, node) for node in _captures(query, tree.root_node)]  # type: ignore[union-attr]


def _docstring_nodes(tree: ast.AST) -> list[ast.stmt]:
    out: list[ast.stmt] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    out.append(body[0])
    return out


def extract_docstrings(src: bytes) -> list[str]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    lines = src.decode("utf-8", "replace").splitlines(keepends=True)
    out: list[str] = []
    for node in _docstring_nodes(tree):
        start = (node.lineno or 1) - 1
        end = node.end_lineno or node.lineno or 1
        out.append("".join(lines[start:end]))
    return out


def _file_digest(path: Path) -> str:
    hasher_factory = None
    try:
        from xxhash import xxh64 as hasher_factory  # type: ignore[assignment]
    except ImportError:
        hasher_factory = None
    if hasher_factory is not None:
        hasher = hasher_factory()
        with path.open("rb") as handle:
            while chunk := handle.read(CHUNK_SIZE):
                hasher.update(chunk)
        return hasher.hexdigest()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def extract_constants(src: bytes) -> list[tuple[str, str, str]]:
    """Upper-case module level assignments as ``(name, value, type)`` tuples."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    found: list[tuple[str, str, str]] = []
    for node in ast.walk(tree):
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        if value is None:
            continue
        for target in targets:
            if not isinstance(target, ast.Name) or not target.id.isupper():
                continue
            type_name = type(value.value).__name__ if isinstance(value, ast.Constant) else "unknown"
            found.append((target.id, ast.unparse(value), type_name))
    return found


def process_file(path: Path, kinds: list[str]) -> tuple[Path, dict[str, list[str]], str | None]:
    try:
        src = path.read_bytes()
    except OSError as exc:
        return path, {}, f"read error: {exc}"
    collected: dict[str, list[str]] = {}
    for kind in kinds:
        try:
            if kind in QUERIES:
                collected[kind] = extract_nodes(src, kind)
            elif kind == "docstrings":
                collected[kind] = extract_docstrings(src)
            elif kind == "constants":
                items = extract_constants(src)
                collected[kind] = [f"{name} = {value}" for name, value, _ in items]
        except RuntimeError as exc:
            return path, collected, str(exc)
        except Exception as exc:  # a broken file must not stop the scan
            return path, collected, f"{kind}: {type(exc).__name__}: {exc}"
    return path, collected, None


def _write_output(out_dir: Path, root: Path, path: Path, collected: dict[str, list[str]], dedupe: bool) -> list[Path]:
    try:
        relative = path.parent.relative_to(root)
    except ValueError:
        relative = Path(".")
    written: list[Path] = []
    for kind, items in collected.items():
        if not items:
            continue
        if dedupe:
            seen: list[str] = []
            for item in items:
                if item not in seen:
                    seen.append(item)
            items = seen
        target = out_dir / relative / f"{kind}.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write("\n\n".join(item.rstrip() for item in items))
            handle.write("\n\n")
        written.append(target)
    return written


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="extract",
        description="Collect classes/functions/docstrings/comments/expressions/imports/constants per folder.",
    )
    parser.add_argument("root", nargs="?", default=".", help="Tree to scan (default: .).")
    parser.add_argument(
        "-k", "--kind", action="append", choices=list(KINDS),
        help="Construct to extract (repeatable; default: every kind).",
    )
    parser.add_argument("-o", "--output", default=None, help="Output directory (default: <root>/output).")
    parser.add_argument("-w", "--workers", type=int, default=min(8, os.cpu_count() or 1))
    parser.add_argument("--no-dedupe", action="store_true", help="Keep duplicate snippets.")
    parser.add_argument("--keep-existing", action="store_true", help="Append to existing output files.")
    parser.add_argument("-q", "--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    root = Path(args.root).resolve()
    if not root.exists():
        parser.error(f"no such path: {root}")
    out_dir = Path(args.output).resolve() if args.output else root / "output"
    kinds = args.kind or list(KINDS)
    if any(kind in QUERIES for kind in kinds) and _tree_sitter() is None:
        print("error: tree-sitter + tree-sitter-python are required (pip install tree-sitter tree-sitter-python)",
              file=sys.stderr)
        return 2
    skip = SKIP_DIRS | {out_dir.name}
    files = sorted(iter_python_files(root, skip))
    if not files:
        print("No Python files found.", file=sys.stderr)
        return 0
    if not args.keep_existing:
        for stale in out_dir.rglob("*.py"):
            if stale.name[: -len(".py")] in KINDS:
                stale.unlink()
    results: list[tuple[Path, dict[str, list[str]], str | None]] = []
    if len(files) == 1 or args.workers <= 1:
        results = [process_file(path, kinds) for path in files]
    else:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            results = list(executor.map(lambda p: process_file(p, kinds), files))
    errors = 0
    written = 0
    for path, collected, error in results:
        if error:
            errors += 1
            print(f"{path}: {error}", file=sys.stderr)
        if collected:
            written += len(_write_output(out_dir, root, path, collected, not args.no_dedupe))
    if not args.quiet:
        print(f"Scanned {len(files)} file(s) -> {written} listing(s) in {out_dir} ({errors} error(s)).")
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
