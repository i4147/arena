#!/usr/bin/env python3
"""arena/make_pdf_fixtures.py — build the PDFs every pdftool test was run against.

Three files, each there for one reason:

  doc1.pdf   3 pages, one titled chapter each    merge, split, per-page text, pages, shrink
  doc2.pdf   2 pages                             merge (5 pages total), multi-file batches
  big.pdf    just over 100 MiB                   the --max-size guard (skipped by default)

plus the mdBook pair `SUMMARY.md` / `print.html`, which is what
`pdftool.py outline --sync-titles/--check-titles` consumes.

    python arena/make_pdf_fixtures.py                  # -> /tmp/pdft
    python arena/make_pdf_fixtures.py -o /tmp/mine     # elsewhere
    python arena/make_pdf_fixtures.py --no-big         # skip the 100 MiB file (~3 s and 100 MB)
    python arena/make_pdf_fixtures.py --big-mb 100.1   # the default target size

`big.pdf` is a normal one-page PDF with a 100 MiB random attachment: incompressible,
so `save(deflate=False)` cannot shrink it back under the limit, and `info` still
opens it to report the page count (which is the point of that exemption).

Exit codes: 0 written · 1 PyMuPDF missing / write failed · 2 bad usage
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from pathlib import Path

try:
    import pymupdf
except ImportError:  # older wheels only ship `fitz`
    try:
        import fitz as pymupdf  # type: ignore[no-redef]
    except ImportError:
        print("needs PyMuPDF: .venv/bin/pip install pymupdf", file=sys.stderr)
        raise SystemExit(1) from None

MiB = 1 << 20

SUMMARY = """# Summary

- [One](one.md)
"""

HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Fixture book</title></head>
<body>
<h1>Fixture book</h1>
<h2>One</h2>
<p>Chapter one, so that the outline has something to point at.</p>
</body>
</html>
"""


def chapter_pdf(path: Path, name: str, pages: int) -> None:
    doc = pymupdf.open()
    for number in range(1, pages + 1):
        page = doc.new_page(width=595, height=842)          # A4, points
        page.insert_text((72, 96), f"Chapter {number} of {name}", fontsize=18)
        page.insert_text((72, 132), f"Body text page {number}.", fontsize=11)
        page.insert_text((72, 160), f"fixture: {name} · page {number}/{pages}", fontsize=9)
    doc.set_metadata({"title": f"{name} fixture", "author": "arena/helpers"})
    doc.save(path, garbage=1, deflate=True)
    doc.close()


def big_pdf(path: Path, target_mib: float) -> None:
    """Valid one-page PDF whose size is just over target_mib."""
    target = int(target_mib * MiB)
    rng = random.Random(20251030)                            # reproducible bytes
    # the embedded stream costs ~300 bytes of object/xref overhead; aim slightly low
    blob = rng.randbytes(max(0, target - 4096))

    last = -1
    for _attempt in range(8):                               # top up if still under
        doc = pymupdf.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 96), "big.pdf — over the default 100 MiB guard", fontsize=14)
        page.insert_text((72, 128), f"target {target_mib:.1f} MiB", fontsize=10)
        doc.embfile_add("padding.bin", blob, filename="padding.bin", ufilename="padding.bin")
        doc.save(path, garbage=0, deflate=False)
        doc.close()
        size = path.stat().st_size
        if size > target:
            return
        if size == last:
            break
        last = size
        blob += rng.randbytes(max(1024, target + 1024 - size))
    raise SystemExit(f"could not push {path} past {target_mib:.1f} MiB (stopped at {last} bytes)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-o", "--out", type=Path, default=Path("/tmp/pdft"),
                        help="output directory (default: /tmp/pdft)")
    parser.add_argument("--big-mb", type=float, default=100.1,
                        help="size of big.pdf in MiB, must exceed 100 (default: 100.1)")
    parser.add_argument("--no-big", action="store_true", help="skip big.pdf (saves ~3 s and 100 MB)")
    parser.add_argument("-f", "--force", action="store_true", help="overwrite existing files")
    args = parser.parse_args(argv)

    if args.big_mb <= 100:
        parser.error("--big-mb must be > 100, otherwise the default guard does not trip")

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    plan = [
        ("doc1.pdf", lambda p: chapter_pdf(p, "doc1.pdf", 3), "3 chapters"),
        ("doc2.pdf", lambda p: chapter_pdf(p, "doc2.pdf", 2), "2 chapters"),
    ]
    if not args.no_big:
        plan.append(("big.pdf", lambda p: big_pdf(p, args.big_mb), f"{args.big_mb:.1f} MiB"))

    for name, builder, note in plan:
        target = out / name
        if target.exists() and not args.force:
            print(f"  keep   {target}  ({target.stat().st_size / MiB:.2f} MiB)")
            continue
        builder(target)
        print(f"  write  {target}  ({target.stat().st_size / MiB:.2f} MiB, {note})")

    for name, text in (("SUMMARY.md", SUMMARY), ("print.html", HTML)):
        target = out / name
        if target.exists() and not args.force:
            print(f"  keep   {target}")
            continue
        target.write_text(text, encoding="utf-8")
        print(f"  write  {target}")

    print(f"\nfixtures ready in {out}\n  arena/smoke_pdftool.sh {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
