#!/data/data/com.termux/files/usr/bin/env python

from __future__ import annotations

import argparse
import shutil
import tempfile
import time
from pathlib import Path

import rmc3 as sc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", type=Path, default=Path.cwd())
    parser.add_argument("-j", "--jobs", type=int, default=min(8, sc.os.cpu_count() or 1))
    args = parser.parse_args()
    sources = list(sc.iter_python_files([args.path], []))
    if not sources:
        parser.error("no Python files found")
    options = sc.TransformOptions(
        remove_all=True,
        remove_all_comments=True,
        remove_docstrings=True,
        remove_type_annotations=True,
        remove_commented_code=False,
        collapse_blank_lines=True,
        make_backup=False,
        overwrite_backup=False,
        dry_run=False,
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        copies = []
        for number, source in enumerate(sources):
            destination = root / f"{number}{source.suffix}"
            shutil.copyfile(source, destination)
            copies.append(destination)
        started = time.perf_counter()
        results = list(sc._result_iterator(copies, options, args.jobs, sc.DEFAULT_CHUNK_SIZE))
        elapsed = time.perf_counter() - started
    errors = sum(result.error is not None for result in results)
    total_bytes = sum(result.old_size for result in results)
    rate = len(results) / elapsed if elapsed else float("inf")
    mib_rate = total_bytes / (1024 * 1024) / elapsed if elapsed else float("inf")
    print(f"files={len(results)} bytes={total_bytes} seconds={elapsed:.3f}")
    print(f"throughput={rate:.1f} files/s, {mib_rate:.1f} MiB/s, errors={errors}")
    return 2 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
