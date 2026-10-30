#!/usr/bin/env python3
"""rmc_ts.py — the simple one: dh.TSStripper + ProcessPoolExecutor, nothing else.

`rmc.py` next door carries three engines, span maths and a token gate.  This file
is the small version: it imports the tree-sitter stripper you already wrote and
trusts it, and it parallelises with `concurrent.futures.ProcessPoolExecutor` and
nothing else — no `multiprocessing.Pool`, no threads, no joblib.

    from dh import TSStripper
    stripper = TSStripper(strip_comments=True, strip_docstrings=True, ...)
    stripped = stripper.strip(source)

Only keyword arguments the installed `TSStripper.__init__` actually declares are
passed, so this works against any of your dh revisions.  If `dh` is not on the
path the script says so and exits 2 instead of half-working.

    rmc_ts.py src/                       # comments + docstrings, 4 workers
    rmc_ts.py src/ -c -j 8               # comments only
    rmc_ts.py src/ -d --backup           # docstrings only, keep .bak
    rmc_ts.py src/ -n --diff             # preview, write nothing
    rmc_ts.py src/ --check               # CI: exit 1 if anything would change
    rmc_ts.py src/ -a --remove-blank-lines

What it will not do: annotations, libcst, regex, unparse.  That is `rmc/rmc.py`.

Exit codes: 0 ok · 1 errors / --check found work · 2 usage or dh missing · 130 interrupted
"""

from __future__ import annotations

import argparse
import ast
import difflib
import re
import fnmatch
import io
import json
import os
import stat
import sys
import time
import tokenize
from collections.abc import Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

__version__ = "1.0.0"
PROG = "rmc_ts.py"

try:  # your module, the reason this file exists
    import dh
    from dh import TSStripper
except Exception as exc:  # noqa: BLE001 - a broken dh must produce a message, not a traceback
    dh = None  # type: ignore[assignment]
    TSStripper = None  # type: ignore[assignment]
    _DH_IMPORT_ERROR: str = f"{type(exc).__name__}: {exc}"
else:
    _DH_IMPORT_ERROR = ""

FALLBACK_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".idea", ".vscode", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".cache", "target", "dist", "build",
})
SKIP_DIRS = frozenset(getattr(dh, "SKIP_DIRS", FALLBACK_SKIP_DIRS)) if dh is not None else FALLBACK_SKIP_DIRS
_COOKIE_RE = re.compile(rb"coding[:=]\s*[-\w.]+")


# --------------------------------------------------------------------------- #
# the stripper, one per worker process                                         #
# --------------------------------------------------------------------------- #

_STRIPPER: object | None = None
_STRIPPER_KEY: tuple | None = None
_BLANK_OK: bool | None = None
_BLANK_NOTED = False


def build_stripper(config: tuple) -> object:
    """Construct a TSStripper with only the kwargs this dh version declares."""
    if TSStripper is None:
        raise RuntimeError(f"cannot import TSStripper from dh ({_DH_IMPORT_ERROR})")
    strip_comments, strip_docstrings, strip_all, blank_lines = config
    # Exactly the knobs `legacy/rmc/rmc_termux.py` sets on your stripper, no more:
    # inventing a kwarg here is how you get a TypeError from a dh we have not seen.
    wanted = {
        "strip_comments": strip_comments or strip_all,
        "strip_docstrings": strip_docstrings or strip_all,
        "preserve_shebangs": not strip_all,
        "preserve_module_docstring": not strip_all,
        "preserve_directives": not strip_all,
        "preserve_commented_code": True,
        "validate": True,
    }
    if blank_lines:
        wanted["remove_blank_lines"] = True          # only if dh knows the name
    accepted = _accepted_kwargs(TSStripper)
    if accepted is None:  # **kwargs, or a signature we cannot read: pass everything
        kwargs = wanted
    else:
        kwargs = {key: value for key, value in wanted.items() if key in accepted}
    return TSStripper(**kwargs)


def blank_lines_supported() -> bool:
    """True if the installed TSStripper takes `remove_blank_lines` itself."""
    global _BLANK_OK
    if _BLANK_OK is None:
        accepted = _accepted_kwargs(TSStripper)
        _BLANK_OK = accepted is None or "remove_blank_lines" in accepted
    return _BLANK_OK


def _accepted_kwargs(cls) -> set[str] | None:
    import inspect

    try:
        signature = inspect.signature(cls.__init__)
    except (TypeError, ValueError):
        return None
    names = set()
    for name, parameter in signature.parameters.items():
        if name == "self":
            continue
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return None
        names.add(name)
    return names


def stripper_for(config: tuple) -> object:
    """Cached per process: the ProcessPoolExecutor worker builds it once."""
    global _STRIPPER, _STRIPPER_KEY
    if _STRIPPER is None or _STRIPPER_KEY != config:
        _STRIPPER = build_stripper(config)
        _STRIPPER_KEY = config
    return _STRIPPER


# --------------------------------------------------------------------------- #
# one file                                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class Job:
    path: str
    config: tuple
    dry_run: bool
    check: bool
    backup: bool
    diff: bool


@dataclass
class Outcome:
    path: str
    status: str = "unchanged"      # changed | would-change | unchanged | skipped | error
    before: int = 0
    after: int = 0
    removed: int = 0
    diff: str = ""
    backup: str = ""
    detail: str = ""
    elapsed: float = 0.0


def strip_one(job: Job) -> Outcome:
    """Worker: runs in a subprocess.  Must stay a module-level function (pickling)."""
    global _BLANK_NOTED
    started = time.perf_counter()
    path = Path(job.path)
    outcome = Outcome(job.path)
    try:
        if path.is_symlink():
            outcome.status = "skipped"
            outcome.detail = "symbolic link"
            return outcome
        original = path.read_bytes()
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(original).readline)
            source = original.decode(encoding)
        except (SyntaxError, UnicodeDecodeError, LookupError) as exc:
            outcome.status = "skipped"
            outcome.detail = f"{type(exc).__name__}: {exc}"
            return outcome
        try:
            stripped = stripper_for(job.config).strip(source)  # type: ignore[union-attr]
        except SyntaxError:
            # the stripper parses too (validate=True); a 3.12-only file is a skip, not a bug
            if not _parses(source):
                outcome.status = "skipped"
                outcome.detail = "source does not parse with this interpreter"
                return outcome
            raise
        if job.config[3] and not blank_lines_supported():
            # older dh has no such kwarg: collapse here, with the same AST guard
            if not _BLANK_NOTED:
                print("note: this TSStripper has no remove_blank_lines kwarg; "
                      "collapsing blank lines locally", file=sys.stderr)
                _BLANK_NOTED = True
            stripped = remove_blank_lines(stripped)
        if stripped == source:
            outcome.before = outcome.after = len(original)
            return outcome
        # cheap safety net: never write something that no longer parses
        try:
            ast.parse(stripped, filename=job.path)
        except SyntaxError:
            if not _parses(source):
                outcome.status = "skipped"
                outcome.detail = "source does not parse with this interpreter"
                return outcome
            raise RuntimeError("stripper produced code that does not parse; file untouched") from None
        data = stripped.encode(encoding)
        outcome.before = len(original)
        outcome.after = len(data)
        outcome.removed = max(0, len(original) - len(data))
        if job.diff:
            outcome.diff = "".join(difflib.unified_diff(
                source.splitlines(keepends=True), stripped.splitlines(keepends=True),
                fromfile=f"a/{path.name}", tofile=f"b/{path.name}",
            ))
        if job.dry_run or job.check:
            outcome.status = "would-change"
            return outcome
        if job.backup:
            spare = path.with_name(path.name + ".bak")
            spare.write_bytes(original)
            os.chmod(spare, stat.S_IMODE(path.stat().st_mode))
            outcome.backup = str(spare)
        atomic_write(path, data)
        outcome.status = "changed"
    except Exception as exc:  # noqa: BLE001 - reported per file, the batch continues
        outcome.status = "error"
        outcome.detail = f"{type(exc).__name__}: {exc}"
    finally:
        outcome.elapsed = time.perf_counter() - started
    return outcome


def remove_blank_lines(source: str) -> str:
    """Drop whitespace-only lines, protecting continuations and multi-line tokens.

    Same contract as the legacy `remove_blank_lines`: if the result would not have
    the same AST, the file is left alone (the caller turns that into an error).
    """
    protected: set[int] = set()
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.start[0] != token.end[0]:
                protected.update(range(token.start[0], token.end[0] + 1))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source
    lines = io.StringIO(source).readlines()
    for row, line in enumerate(lines, 1):
        if line.rstrip("\r\n").endswith("\\"):
            protected.add(row + 1)
        if row <= 2 and _COOKIE_RE.search(line.encode("utf-8")):
            protected.update(range(1, row + 1))
    result = "".join(
        line for row, line in enumerate(lines, 1)
        if row in protected or line.strip(" \t\f\r\n")
    )
    if ast.dump(ast.parse(source)) != ast.dump(ast.parse(result)):
        return source
    return result


def _parses(source: str) -> bool:
    try:
        ast.parse(source)
    except SyntaxError:
        return False
    return True


def atomic_write(path: Path, data: bytes) -> None:
    """tmp in the same directory -> copymode -> os.replace (interruption-safe)."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# walking                                                                      #
# --------------------------------------------------------------------------- #


def walk_python_files(paths: Sequence[Path], *, recursive: bool, exclude: Sequence[str]) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for raw in paths or [Path.cwd()]:
        root = raw.expanduser()
        if not root.exists():
            print(f"{root}: no such file or directory", file=sys.stderr)
            continue
        if root.is_file():
            candidates: Iterator[Path] = iter([root])
        elif recursive:
            candidates = (item for item in sorted(root.rglob("*.py")) if item.is_file())
        else:
            candidates = (item for item in sorted(root.glob("*.py")) if item.is_file())
        for candidate in candidates:
            if candidate.suffix != ".py" or candidate.is_symlink():
                continue
            if root.is_dir():
                if any(part in SKIP_DIRS or part.endswith(".dist-info") or part.endswith(".egg-info")
                       for part in candidate.relative_to(root).parts[:-1]):
                    continue
            if any(fnmatch.fnmatch(candidate.name, pattern) or fnmatch.fnmatch(str(candidate), pattern)
                   for pattern in exclude):
                continue
            key = candidate.resolve()
            if key not in seen:
                seen.add(key)
                found.append(candidate)
    return found


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Strip comments/docstrings with dh.TSStripper (tree-sitter only), "
                    "in ProcessPoolExecutor workers.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "the dh module must be importable:  PYTHONPATH=/path/to/parent rmc_ts.py ...\n"
            "\n"
            "examples:\n"
            "  %(prog)s src/ -c -j 8\n"
            "  %(prog)s src/ -n --diff\n"
            "  %(prog)s . --check\n"
            "  %(prog)s src/ -d --backup --remove-blank-lines\n"
        ),
    )
    parser.add_argument("paths", nargs="*", type=Path, metavar="PATH",
                        help="files and/or directories (default: .)")
    parser.add_argument("-c", "--comments", action="store_true", help="strip comments")
    parser.add_argument("-d", "--docstrings", action="store_true", help="strip docstrings")
    parser.add_argument("-a", "--all", action="store_true",
                        help="strip everything, including shebang, coding line, directives "
                             "and the module docstring")
    parser.add_argument("--remove-blank-lines", action="store_true", help="then collapse blank lines")
    parser.add_argument("-n", "--dry-run", action="store_true", help="write nothing")
    parser.add_argument("--check", action="store_true",
                        help="write nothing; exit 1 if any file would change")
    parser.add_argument("--diff", action="store_true", help="print a unified diff (implies -n)")
    parser.add_argument("--backup", action="store_true", help="write FILE.bak before overwriting")
    parser.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 1),
                        help="ProcessPoolExecutor workers (default: cpu count, max 8)")
    parser.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false")
    parser.add_argument("-q", "--quiet", action="store_true", help="only errors and the summary")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if TSStripper is None:
        print(f"{PROG}: cannot import TSStripper from dh ({_DH_IMPORT_ERROR})", file=sys.stderr)
        print("put dh.py on PYTHONPATH, e.g. PYTHONPATH=~/lib python rmc_ts.py src/", file=sys.stderr)
        return 2

    if not (args.comments or args.docstrings or args.all):
        args.comments = args.docstrings = True  # the sensible default: everything ordinary

    config = (bool(args.comments), bool(args.docstrings), bool(args.all),
              bool(args.remove_blank_lines))
    files = walk_python_files(args.paths, recursive=args.recursive, exclude=args.exclude)
    if not files:
        print("no .py files found", file=sys.stderr)
        return 1

    started = time.perf_counter()
    jobs = [Job(str(path), config, bool(args.dry_run or args.diff), args.check,
                args.backup, args.diff and not args.json) for path in files]
    workers = max(1, min(args.jobs, len(jobs)))
    chunksize = max(1, len(jobs) // (workers * 8))

    outcomes: list[Outcome] = []
    try:
        if workers == 1:
            outcomes = [strip_one(job) for job in jobs]
        else:
            # the only parallelism in this file: a process pool, nothing else
            with ProcessPoolExecutor(max_workers=workers) as pool:
                outcomes = list(pool.map(strip_one, jobs, chunksize=chunksize))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130

    failures = sum(1 for item in outcomes if item.status == "error")
    skipped = sum(1 for item in outcomes if item.status == "skipped")
    changed = sum(1 for item in outcomes if item.status == "changed")
    pending = sum(1 for item in outcomes if item.status == "would-change")
    saved = sum(item.removed for item in outcomes)
    elapsed = time.perf_counter() - started

    if args.json:
        payload = {
            "tool": PROG,
            "version": __version__,
            "command": "strip",
            "elapsed": round(elapsed, 3),
            "counts": {"files": len(outcomes), "changed": changed, "pending": pending,
                       "skipped": skipped, "failed": failures, "bytes_removed": saved,
                       "workers": workers},
            "files": [
                {"path": item.path, "status": item.status, "before": item.before,
                 "after": item.after, "removed": item.removed, "detail": item.detail,
                 "backup": item.backup}
                for item in outcomes
            ],
        }
        print(json.dumps(payload, indent=2))
    else:
        report(outcomes, args)
        summary = (f"{len(outcomes)} file(s) · {changed} changed · {pending} pending · "
                   f"{skipped} skipped · {failures} failed · {saved:,} bytes removed · "
                   f"{elapsed:.2f}s · {workers} worker(s)")
        print(summary, file=sys.stderr)

    if failures:
        return 1
    if args.check and pending:
        return 1
    return 0


def report(outcomes: Sequence[Outcome], args: argparse.Namespace) -> None:
    for item in outcomes:
        name = item.path
        if item.status == "changed":
            if not args.quiet:
                print(f"[done]  {name}  -{item.removed:,} bytes"
                      + (f"  (backup {Path(item.backup).name})" if item.backup else ""))
        elif item.status == "would-change":
            if not args.quiet:
                label = "check" if args.check else "dry"
                print(f"[{label}]  {name}  -{item.removed:,} bytes would go")
        elif item.status == "skipped":
            print(f"[skip]  {name}  {item.detail}", file=sys.stderr)
        elif item.status == "error":
            print(f"[fail]  {name}  {item.detail}", file=sys.stderr)
        elif args.diff:
            continue
        if item.diff:
            sys.stdout.write(item.diff)
            if not item.diff.endswith("\n"):
                sys.stdout.write("\n\\ No newline at end of file\n")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
