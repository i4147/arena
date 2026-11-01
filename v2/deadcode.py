#!/usr/bin/env python3
"""deadcode.py — find and remove unused imports, definitions and boilerplate.

Split out of pycleaner.py (1,275 lines), which lived in the comment-stripping
folder but does something else entirely: it reasons about *names*, not syntax.

    deadcode.py imports [PATH…]   unused imports        (was: remove_import / rmunused)
    deadcode.py unused  [PATH…]   unused top-level defs (was: detect_unused / rmunused*)
    deadcode.py block   [PATH…]   remove a fixed block  (was: rmunused / licence strips)
    deadcode.py doctor

Everything is proven before it is written: a candidate is only removed when the
result still compiles *and* the target name is not referenced anywhere in scope.

Exit codes: 0 ok · 1 findings (with --check) or errors · 2 usage
"""


import argparse
import ast
import difflib
import fnmatch
import json
import os
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "1.0.0"
PROG = "deadcode.py"

DEFAULT_SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", ".idea", ".vscode", ".venv", "venv", "env",
        "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache",
        ".ruff_cache", ".tox", ".nox", ".cache", "build", "dist", "target",
        "site-packages", ".eggs",
    }
)

CONSTANT_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


# --------------------------------------------------------------------------- #
# model                                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Finding:
    name: str
    line: int
    kind: str  # import | function | class | constant | block
    end_line: int
    detail: str = ""
    names: tuple[str, ...] = ()

    @property
    def span(self) -> tuple[int, int]:
        return self.line, self.end_line


@dataclass(slots=True)
class Result:
    path: str
    status: str
    findings: list[Finding] = field(default_factory=list)
    detail: str = ""
    diff: str = ""


@dataclass(frozen=True, slots=True)
class Output:
    dry_run: bool = False
    diff: bool = False
    backup: bool = False


# --------------------------------------------------------------------------- #
# analysis                                                                    #
# --------------------------------------------------------------------------- #


def module_all(tree: ast.Module) -> set[str]:
    """Names listed in ``__all__`` are part of the public surface: never unused."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
                continue
            value = node.value
            if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
                for element in value.elts:
                    if isinstance(element, ast.Constant) and isinstance(element.value, str):
                        names.add(element.value)
    return names


def imported_bindings(tree: ast.Module) -> list[tuple[str, ast.stmt, str]]:
    """(bound name, statement, import text) for every module-level import."""
    bindings: list[tuple[str, ast.stmt, str]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                bindings.append((bound, node, ast.unparse(node)))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name
                bindings.append((bound, node, ast.unparse(node)))
    return bindings


def referenced_names(tree: ast.AST) -> set[str]:
    """Every name that is *read* (not merely bound)."""
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            base = node
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name):
                used.add(base.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # string annotations: "List[int]" and forward references
            for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", node.value):
                used.add(token)
    return used


def unused_imports(tree: ast.Module) -> list[Finding]:
    """One finding per import statement, listing every aliases that is unused."""
    exported = module_all(tree)
    used = referenced_names(tree)
    grouped: dict[int, list[str]] = {}
    statements: dict[int, tuple[ast.stmt, str]] = {}
    for bound, node, text in imported_bindings(tree):
        if bound in used or bound in exported:
            continue
        grouped.setdefault(id(node), []).append(bound)
        statements[id(node)] = (node, text)
    findings: list[Finding] = []
    for key, names in grouped.items():
        node, text = statements[key]
        findings.append(
            Finding(names[0], node.lineno, "import", node.end_lineno or node.lineno, text, tuple(names))
        )
    return sorted(findings, key=lambda f: (f.line, f.name))


def top_level_definitions(tree: ast.Module) -> list[Finding]:
    findings: list[Finding] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            findings.append(Finding(node.name, node.lineno, "function", node.end_lineno or node.lineno))
        elif isinstance(node, ast.ClassDef):
            findings.append(Finding(node.name, node.lineno, "class", node.end_lineno or node.lineno))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and CONSTANT_RE.match(target.id):
                    findings.append(
                        Finding(target.id, node.lineno, "constant", node.end_lineno or node.lineno)
                    )
    return findings


def unused_definitions(tree: ast.Module, external: set[str], scope: str) -> list[Finding]:
    exported = module_all(tree)
    used = referenced_names(tree)
    findings: list[Finding] = []
    for finding in top_level_definitions(tree):
        if finding.name in used or finding.name in exported:
            continue
        if finding.name.startswith("__") and finding.name.endswith("__"):
            continue
        if scope == "global" and finding.name in external:
            continue
        findings.append(finding)
    return findings


# --------------------------------------------------------------------------- #
# edits                                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Edit:
    start: int
    end: int
    repl: str = ""


def offsets_of(text: str) -> list[int]:
    table = [0]
    for line in text.splitlines(keepends=True):
        table.append(table[-1] + len(line.encode("utf-8")))
    return table


def line_span(text: str, start_line: int, end_line: int) -> tuple[int, int]:
    """Byte span covering whole lines ``start_line..end_line`` inclusive."""
    encoded = text.encode("utf-8")
    table = offsets_of(text)
    start = table[start_line - 1]
    stop = table[end_line] if end_line < len(table) else len(encoded)
    return start, stop


def apply_edits(text: str, edits: Sequence[Edit]) -> str:
    encoded = bytearray(text.encode("utf-8"))
    for edit in sorted(edits, key=lambda e: e.start, reverse=True):
        encoded[edit.start : edit.end] = edit.repl.encode("utf-8")
    return encoded.decode("utf-8", "replace")


def remove_lines(text: str, spans: Iterable[tuple[int, int]]) -> str:
    """Remove whole-line spans, collapsing the blank run they leave behind."""
    edits: list[Edit] = []
    for start_line, end_line in spans:
        start, stop = line_span(text, start_line, end_line)
        edits.append(Edit(start, stop))
    new_text = apply_edits(text, edits)
    return re.sub(r"\n{3,}", "\n\n", new_text)


def drop_import_aliases(text: str, line: int, unused: Sequence[str]) -> str:
    """Remove ``unused`` aliases from the import statement starting at ``line``."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return remove_lines(text, [(line, line)])
    for node in tree.body:
        if node.lineno != line or not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        end_line = node.end_lineno or node.lineno

        def bound_name(alias: ast.alias) -> str:
            if isinstance(node, ast.Import):
                return alias.asname or alias.name.split(".")[0]
            return alias.asname or alias.name

        keep = [alias for alias in node.names if bound_name(alias) not in unused]
        if not keep:
            return remove_lines(text, [(node.lineno, end_line)])
        if isinstance(node, ast.Import):
            replacement = "import " + ", ".join(ast.unparse(alias) for alias in keep)
        else:
            module = node.module or ""
            dots = "." * (node.level or 0)
            replacement = f"from {dots}{module} import " + ", ".join(ast.unparse(alias) for alias in keep)
        start, stop = line_span(text, node.lineno, end_line)
        return apply_edits(text, [Edit(start, stop, replacement + "\n")])
    return remove_lines(text, [(line, line)])


def validate(before: str, after: str) -> tuple[bool, str]:
    try:
        compile(after, "<deadcode>", "exec")
    except SyntaxError as exc:
        return False, f"output does not compile: {exc.msg} (line {exc.lineno})"
    return True, ""


# --------------------------------------------------------------------------- #
# per-file drivers                                                            #
# --------------------------------------------------------------------------- #


def read_source(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    return raw.decode("utf-8"), "utf-8"


def write_source(path: Path, text: str, encoding: str, out: Output) -> None:
    if out.backup:
        backup = path.with_name(path.name + ".bak")
        if not backup.exists():
            shutil.copy2(path, backup)
    data = text.encode("utf-8")
    if encoding == "utf-8-sig":
        data = b"\xef\xbb\xbf" + data
    handle_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "wb") as handle:
            handle.write(data)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def do_imports(raw: str, out: Output, ignore_init: bool, apply: bool) -> Result:
    path = Path(raw)
    try:
        text, encoding = read_source(path)
        tree = ast.parse(text)
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        return Result(raw, "error", detail=f"{type(exc).__name__}: {exc}")
    if ignore_init and path.name == "__init__.py":
        return Result(raw, "skipped", detail="__init__.py re-exports")
    findings = unused_imports(tree)
    if not findings:
        return Result(raw, "clean")
    if not apply:
        return Result(raw, "found", findings)
    new_text = text
    for finding in sorted(findings, key=lambda f: -f.line):
        candidate = drop_import_aliases(new_text, finding.line, finding.names)
        ok, problem = validate(text, candidate)
        if not ok:
            return Result(raw, "rejected", findings, problem)
        new_text = candidate
    if new_text == text:
        return Result(raw, "clean")
    result = Result(raw, "changed", findings)
    if out.diff:
        result.diff = "".join(
            difflib.unified_diff(
                text.splitlines(keepends=True), new_text.splitlines(keepends=True),
                fromfile=f"a/{raw}", tofile=f"b/{raw}",
            )
        )
    if not out.dry_run:
        write_source(path, new_text, encoding, out)
    return result


def do_unused(raw: str, out: Output, external: set[str], scope: str, kinds: set[str], apply: bool) -> Result:
    path = Path(raw)
    try:
        text, encoding = read_source(path)
        tree = ast.parse(text)
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        return Result(raw, "error", detail=f"{type(exc).__name__}: {exc}")
    findings = [f for f in unused_definitions(tree, external, scope) if f.kind in kinds]
    if not findings:
        return Result(raw, "clean")
    if not apply:
        return Result(raw, "found", findings)
    candidate = remove_lines(text, [f.span for f in findings])
    ok, problem = validate(text, candidate)
    if not ok:
        return Result(raw, "rejected", findings, problem)
    result = Result(raw, "changed", findings)
    if out.diff:
        result.diff = "".join(
            difflib.unified_diff(
                text.splitlines(keepends=True), candidate.splitlines(keepends=True),
                fromfile=f"a/{raw}", tofile=f"b/{raw}",
            )
        )
    if not out.dry_run:
        write_source(path, candidate, encoding, out)
    return result


def do_block(raw: str, block: str, out: Output, apply: bool) -> Result:
    path = Path(raw)
    try:
        text, encoding = read_source(path)
    except (OSError, UnicodeDecodeError) as exc:
        return Result(raw, "error", detail=str(exc))
    needle = block.strip("\n").splitlines()
    if not needle:
        return Result(raw, "error", detail="empty block")
    lines = text.splitlines(keepends=True)
    stripped = [line.rstrip("\n") for line in lines]
    hits: list[tuple[int, int]] = []
    index = 0
    while index <= len(stripped) - len(needle):
        window = [line.rstrip() for line in stripped[index : index + len(needle)]]
        if window == [line.rstrip() for line in needle]:
            hits.append((index + 1, index + len(needle)))
            index += len(needle)
        else:
            index += 1
    if not hits:
        return Result(raw, "clean")
    findings = [Finding("<block>", start, "block", end) for start, end in hits]
    if not apply:
        return Result(raw, "found", findings)
    candidate = remove_lines(text, hits)
    ok, problem = validate(text, candidate)
    if not ok:
        return Result(raw, "rejected", findings, problem)
    result = Result(raw, "changed", findings)
    if out.diff:
        result.diff = "".join(
            difflib.unified_diff(
                text.splitlines(keepends=True), candidate.splitlines(keepends=True),
                fromfile=f"a/{raw}", tofile=f"b/{raw}",
            )
        )
    if not out.dry_run:
        write_source(path, candidate, encoding, out)
    return result


# --------------------------------------------------------------------------- #
# whole-tree scope for --scope global                                         #
# --------------------------------------------------------------------------- #


def external_names(targets: Sequence[Path], exclude: Path | None) -> set[str]:
    """Names referenced from outside the file that defines them."""
    used: set[str] = set()
    for path in targets:
        if exclude is not None and path == exclude:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        used |= referenced_names(tree)
    return used


# --------------------------------------------------------------------------- #
# traversal                                                                   #
# --------------------------------------------------------------------------- #


def collect(paths: Sequence[Path], recursive: bool, exclude: tuple[str, ...]) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []

    def wanted(path: Path) -> bool:
        if path.suffix.lower() != ".py":
            return False
        return not any(fnmatch.fnmatch(path.name, p) or fnmatch.fnmatch(str(path), p) for p in exclude)

    for raw in paths:
        path = raw.expanduser()
        if not path.exists() or path.is_symlink():
            continue
        if path.is_file():
            if wanted(path):
                out.append(path)
            continue
        if not path.is_dir():
            continue
        if recursive:
            for root, dirs, files in os.walk(path):
                dirs[:] = sorted(d for d in dirs if d not in DEFAULT_SKIP_DIRS and not d.startswith("."))
                for name in sorted(files):
                    candidate = Path(root) / name
                    if wanted(candidate) and not candidate.is_symlink() and candidate.resolve() not in seen:
                        seen.add(candidate.resolve())
                        out.append(candidate)
        else:
            for candidate in sorted(path.iterdir()):
                if candidate.is_file() and wanted(candidate):
                    out.append(candidate)
    return out


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Remove unused imports, unused top-level definitions, and fixed blocks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "was: pycleaner.py (imports / unused / block-removal modes)\n"
            "\n"
            "examples:\n"
            "  %(prog)s imports --apply --backup src/\n"
            "  %(prog)s unused --kinds functions classes --diff src/\n"
            "  %(prog)s block --block-file license.txt --apply .\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subs = parser.add_subparsers(dest="command", required=True)

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("paths", nargs="*", type=Path, help="files/directories (default: .)")
        sub.add_argument("-n", "--dry-run", action="store_true")
        sub.add_argument("--diff", action="store_true", help="print a unified diff (implies --dry-run)")
        sub.add_argument("--apply", action="store_true", help="actually rewrite files")
        sub.add_argument("--backup", action="store_true")
        sub.add_argument("--check", action="store_true", help="exit 1 when findings exist")
        sub.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 1))
        sub.add_argument("--exclude", action="append", default=[], metavar="GLOB")
        sub.add_argument("--no-recursive", action="store_true")
        sub.add_argument("-q", "--quiet", action="store_true")
        sub.add_argument("-v", "--verbose", action="store_true")
        sub.add_argument("--json", nargs="?", const="-", default=None, metavar="FILE")

    imports = subs.add_parser("imports", help="detect (and optionally remove) unused imports")
    common(imports)
    imports.add_argument("--ignore-init", action="store_true", help="never touch __init__.py")

    unused = subs.add_parser("unused", help="detect/remove unused top-level definitions")
    common(unused)
    unused.add_argument("--scope", choices=("file", "global"), default="file",
                        help="global: a name used in any scanned file counts as used")
    unused.add_argument("--kinds", nargs="+", choices=("functions", "classes", "constants"),
                        default=("functions", "classes", "constants"))

    block = subs.add_parser("block", help="remove a fixed block of lines from many files")
    common(block)
    block.add_argument("--block-file", required=True, help="file whose contents are the block to remove")

    doctor = subs.add_parser("doctor", help="report capabilities")
    doctor.add_argument("paths", nargs="*", type=Path)
    return parser


def kind_map(kinds: Sequence[str]) -> set[str]:
    mapping = {"functions": "function", "classes": "class", "constants": "constant"}
    return {mapping[kind] for kind in kinds}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "doctor":
        print(f"{PROG} {__version__}  python {sys.version.split()[0]}")
        print("\nbackend   ast (stdlib) — always available")
        print("autoflake optional: pip install autoflake  (used by pycleaner.py, not required here)")
        return 0

    roots = [p.expanduser() for p in args.paths] or [Path.cwd()]
    targets = collect(roots, not args.no_recursive, tuple(args.exclude))
    if not targets:
        if not args.quiet:
            print("no Python files found", file=sys.stderr)
        return 0
    out = Output(dry_run=args.dry_run or args.diff or not args.apply, diff=args.diff, backup=args.backup)
    started = time.perf_counter()

    results: list[Result] = []
    if args.command == "imports":
        for target in targets:
            results.append(do_imports(str(target), out, args.ignore_init, args.apply and not out.dry_run))
    elif args.command == "unused":
        kinds = kind_map(args.kinds)
        pool_external = external_names(targets, None) if args.scope == "global" else set()
        for target in targets:
            results.append(do_unused(str(target), out, pool_external, args.scope, kinds, args.apply and not out.dry_run))
    else:
        block_text = Path(args.block_file).read_text(encoding="utf-8")
        for target in targets:
            results.append(do_block(str(target), block_text, out, args.apply and not out.dry_run))

    found = 0
    for result in results:
        if result.findings:
            found += len(result.findings)
        if args.quiet:
            continue
        if result.findings and result.status in ("found", "changed"):
            for finding in result.findings:
                label = finding.detail or finding.name
                print(f"{result.path}:{finding.line}  {finding.kind}  {label}")
            if result.diff:
                sys.stdout.write(result.diff)
        elif result.status in ("rejected", "error"):
            print(f"{result.path}  {result.status}: {result.detail}", file=sys.stderr)

    elapsed = time.perf_counter() - started
    if not args.quiet:
        print(f"\n{found} finding(s) in {len(targets)} file(s) · {elapsed:.2f}s", file=sys.stderr)
    if args.json:
        payload = {
            "tool": PROG, "command": args.command, "version": __version__,
            "elapsed": round(elapsed, 3), "total": found,
            "files": [
                {
                    "path": r.path, "status": r.status,
                    "findings": [
                        {"name": f.name, "line": f.line, "kind": f.kind, "detail": f.detail}
                        for f in r.findings
                    ],
                }
                for r in results
            ],
        }
        text = json.dumps(payload, indent=2)
        if args.json == "-":
            print(text)
        else:
            Path(args.json).write_text(text, encoding="utf-8")
    if any(r.status == "error" for r in results):
        return 1
    if args.check and found:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
