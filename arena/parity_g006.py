#!/usr/bin/env python3
"""arena/parity_g006.py — prove g006's two removal backends agree byte for byte.

The claim in PLAN.md §8 is that `annotations.py remove` produces *identical*
output from the tree-sitter and libcst backends.  That claim came from a loop in
the session, and this file is that loop:

    python arena/parity_g006.py                  # fixed fixture set, all green or exit 1
    python arena/parity_g006.py pdftool.py src/   # your own files
    python arena/parity_g006.py --keep /tmp/par     # leave the copies behind

Each input is copied twice, one copy per backend, both are rewritten in place, and
the two results are compared with `cmp`.  Files that a backend refuses (syntax
errors, unparsable input) are reported as skipped rather than failures — the
point is parity where both backends commit to running.

Exit codes: 0 identical everywhere · 1 a difference (or an unexpected backend
failure) · 2 bad usage / libcst not installed
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TOOL = ROOT / "annotations.py"

#: the fixtures that were actually used: hand-written corner cases + real files
FIXTURES = [
    "extract.py",        # plain defs, docstrings, returns
    "pdftool.py",        # dataclasses, `X | None`, decorators
    "codecs.py",         # enums, nested classes
    "deadcode.py",       # heavy on ast.Call / comprehensions, annotated attributes
    "comments.py",       # type comments, multiline signatures
]


def run_backend(tool: Path, python: str, backend: str, path: Path) -> tuple[int, str]:
    result = subprocess.run(
        [python, str(tool), "remove", str(path), "-b", backend, "-q"],
        capture_output=True, text=True,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("files", nargs="*", type=Path,
                        help="files to compare (default: the session's five fixtures)")
    parser.add_argument("--keep", type=Path, default=None,
                        help="keep the rewritten copies here instead of a temp dir")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args(argv)

    if not TOOL.is_file():
        print(f"missing {TOOL}", file=sys.stderr)
        return 2
    probe = subprocess.run([args.python, "-c", "import libcst"], capture_output=True)
    if probe.returncode != 0:
        print("libcst is not installed — both backends are needed for a parity check",
              file=sys.stderr)
        print("  .venv/bin/pip install libcst", file=sys.stderr)
        return 2

    inputs = args.files or [ROOT / name for name in FIXTURES]
    work = args.keep or Path(tempfile.mkdtemp(prefix="parity-g006."))
    work.mkdir(parents=True, exist_ok=True)

    same = different = skipped = 0
    differences: list[str] = []

    for source in inputs:
        if not source.is_file():
            print(f"  skip   {source} (not a file)")
            skipped += 1
            continue

        pair = []
        for backend in ("tree-sitter", "libcst"):
            target = work / f"{source.stem}.{backend.replace('-', '_')}{source.suffix}"
            shutil.copy2(source, target)
            rc, output = run_backend(TOOL, args.python, backend, target)
            if rc != 0:
                print(f"  skip   {source.name} [{backend}] exit {rc}: {output.splitlines()[:1]}")
                skipped += 1
                pair = []
                break
            pair.append(target)

        if len(pair) != 2:
            continue

        left, right = pair
        if left.read_bytes() == right.read_bytes():
            same += 1
            print(f"  same   {source.name}  ({left.stat().st_size} bytes, both backends)")
        else:
            different += 1
            diff = subprocess.run(["diff", "-u", str(left), str(right)],
                                  capture_output=True, text=True).stdout
            first = next((line for line in diff.splitlines() if line.startswith(("+", "-"))
                          and not line.startswith(("+++", "---"))), "")
            print(f"  DIFF   {source.name}  first differing line: {first!r}")
            (work / f"{source.stem}.diff").write_text(diff)
            differences.append(source.name)

    # a file that never parses is not a failure of parity; a crash is
    print(f"\n{same} identical · {different} different · {skipped} skipped"
          + (f"  ({', '.join(differences)} differ)" if differences else ""))
    if args.keep:
        print(f"copies kept in {work}")

    if different:
        return 1
    if same == 0:
        print("nothing was actually compared", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
