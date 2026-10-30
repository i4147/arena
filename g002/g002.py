#!/data/data/com.termux/files/usr/bin/env python3
"""g002 - consolidated PDF toolkit.

Everything that used to live in this folder as separate scripts now lives in
this one file.  Each tool's original source is embedded verbatim below, so
nothing was lost: ``python g002.py unpack <tool>`` writes the original
file back out.

Four competing terminal viewers and four overlapping page utilities were
reduced to one viewer plus one tool per job (text, page surgery,
resource extraction, rasterising).

Tools
-----
text       Extract text per page with 5 interchangeable backends (pymupdf, pypdf, PyPDF2, pdfplumber, pdfminer), parallel, name templates, skip-existing.
           from pdf2page.py (439 lines)
pages      Seven page-level subcommands: outline, merge, to-img, rename-pages, shrink, split-size, split-page (pymupdf only).
           from pdftoolz.py (709 lines)
resources  Page count, per-page split, and extraction of images, fonts, annotations and embedded files.
           from pdftools.py (284 lines)
view       Terminal PDF viewer (half-block rendering, zoom, keyboard driven).
           from pdfviewer.py (317 lines)
img        Batch PDF -> JPG conversion (300 dpi) with optional removal of the converted originals.
           from pdf2img.py (94 lines)

Usage
-----
    python g002.py list                 # list the tools
    python g002.py doctor               # report missing third-party packages
    python g002.py <tool> --help        # help for one tool
    python g002.py text <args>
    python g002.py pages <args>
    python g002.py resources <args>
    python g002.py unpack <tool> [-o DIR] [--stdout]

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

FOLDER = "g002"


_HERE = Path(__file__).resolve().parent if "__file__" in globals() else None

TOOLS: dict[str, dict[str, object]] = {
    "text": {"source": "pdf2page.py", "lines": 439, "summary": 'Extract text per page with 5 interchangeable backends (pymupdf, pypdf, PyPDF2, pdfplumber, pdfminer), parallel, name templates, skip-existing.'},
    "pages": {"source": "pdftoolz.py", "lines": 709, "summary": 'Seven page-level subcommands: outline, merge, to-img, rename-pages, shrink, split-size, split-page (pymupdf only).'},
    "resources": {"source": "pdftools.py", "lines": 284, "summary": 'Page count, per-page split, and extraction of images, fonts, annotations and embedded files.'},
    "view": {"source": "pdfviewer.py", "lines": 317, "summary": 'Terminal PDF viewer (half-block rendering, zoom, keyboard driven).'},
    "img": {"source": "pdf2img.py", "lines": 94, "summary": 'Batch PDF -> JPG conversion (300 dpi) with optional removal of the converted originals.'},
}

_SOURCES: dict[str, str] = {}
_LOADED: dict[str, types.ModuleType] = {}


def _decode(*chunks: str) -> str:
    """Fallback representation for sources that cannot be written as raw strings."""
    return zlib.decompress(base64.b85decode("".join(chunks))).decode("utf-8")


_SOURCES['text'] = r"""#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Sequence


def _extract_plumber(pdf_path: str, page_indices: Sequence[int], opts: dict) -> list[tuple[int, str]]:
    import pdfplumber

    out: list[tuple[int, str]] = []
    with pdfplumber.open(pdf_path) as pdf:
        for idx in page_indices:
            try:
                text = pdf.pages[idx].extract_text() or ""
            except Exception as exc:
                print(
                    f"Error extracting page {idx + 1} from {pdf_path}: {exc}",
                    file=sys.stderr,
                )
                text = ""
            out.append((idx + 1, text))
    return out


def _extract_fitz(pdf_path: str, page_indices: Sequence[int], opts: dict) -> list[tuple[int, str]]:
    import fitz

    sort = bool(opts.get("sort", False))
    out: list[tuple[int, str]] = []
    doc = fitz.open(pdf_path)
    try:
        for idx in page_indices:
            try:
                page = doc[idx]
                text = page.get_text("text", sort=True) if sort else page.get_text()
            except Exception as exc:
                print(
                    f"Error extracting page {idx + 1} from {pdf_path}: {exc}",
                    file=sys.stderr,
                )
                text = ""
            out.append((idx + 1, text))
    finally:
        doc.close()
    return out


def _extract_pypdf(pdf_path: str, page_indices: Sequence[int], opts: dict) -> list[tuple[int, str]]:
    from pypdf import PdfReader

    out: list[tuple[int, str]] = []
    with open(pdf_path, "rb") as fh:
        reader = PdfReader(fh)
        for idx in page_indices:
            try:
                text = reader.pages[idx].extract_text() or ""
            except Exception as exc:
                print(
                    f"Error extracting page {idx + 1} from {pdf_path}: {exc}",
                    file=sys.stderr,
                )
                text = ""
            out.append((idx + 1, text))
    return out


def _extract_pypdf2(pdf_path: str, page_indices: Sequence[int], opts: dict) -> list[tuple[int, str]]:
    import PyPDF2

    out: list[tuple[int, str]] = []
    with open(pdf_path, "rb") as fh:
        reader = PyPDF2.PdfReader(fh)
        for idx in page_indices:
            try:
                text = reader.pages[idx].extract_text() or ""
            except Exception as exc:
                print(
                    f"Error extracting page {idx + 1} from {pdf_path}: {exc}",
                    file=sys.stderr,
                )
                text = ""
            out.append((idx + 1, text))
    return out


def _extract_pdfminer(pdf_path: str, page_indices: Sequence[int], opts: dict) -> list[tuple[int, str]]:
    from pdfminer.high_level import extract_text
    from pdfminer.layout import LAParams

    laparams = None
    if not opts.get("no_laparams", False):
        kw: dict = {
            "detect_vertical": bool(opts.get("detect_vertical", False)),
            "all_texts": bool(opts.get("all_texts", False)),
        }
        for key in (
            "line_overlap",
            "char_margin",
            "line_margin",
            "word_margin",
            "boxes_flow",
        ):
            val = opts.get(key)
            if val is not None:
                kw[key] = val
        laparams = LAParams(**kw)
    password = opts.get("password") or ""
    out: list[tuple[int, str]] = []
    for idx in page_indices:
        try:
            text = extract_text(pdf_path, page_numbers=[idx], password=password, laparams=laparams) or ""
        except Exception as exc:
            print(
                f"Error extracting page {idx + 1} from {pdf_path}: {exc}",
                file=sys.stderr,
            )
            text = ""
        out.append((idx + 1, text))
    return out


_BACKENDS = {
    "plumber": _extract_plumber,
    "fitz": _extract_fitz,
    "pypdf": _extract_pypdf,
    "pypdf2": _extract_pypdf2,
    "pdfminer": _extract_pdfminer,
}
_THREAD_BACKENDS = {"fitz", "plumber"}


def _extract_chunk(pdf_path: str, page_indices: Sequence[int], backend: str, opts: dict) -> list[tuple[int, str]]:
    return _BACKENDS[backend](pdf_path, page_indices, opts)


def _page_count(pdf_path: str, backend: str, password: str = "") -> int:
    if backend == "plumber":
        import pdfplumber

        with pdfplumber.open(pdf_path) as pdf:
            return len(pdf.pages)
    if backend == "fitz":
        import fitz

        doc = fitz.open(pdf_path)
        try:
            return len(doc)
        finally:
            doc.close()
    if backend == "pypdf":
        from pypdf import PdfReader

        with open(pdf_path, "rb") as fh:
            return len(PdfReader(fh).pages)
    if backend == "pypdf2":
        import PyPDF2

        with open(pdf_path, "rb") as fh:
            return len(PyPDF2.PdfReader(fh).pages)
    if backend == "pdfminer":
        from pdfminer.pdfpage import PDFPage

        with open(pdf_path, "rb") as fh:
            return sum(1 for _ in PDFPage.get_pages(fh, password=password or ""))
    msg = f"Unknown backend: {backend}"
    raise ValueError(msg)


def _collect_pdfs(inputs: Sequence[str], recursive: bool = True) -> list[Path]:
    if not inputs:
        inputs = ["."]
    seen: set = set()
    result: list[Path] = []
    for item in inputs:
        path = Path(item).expanduser()
        if path.is_file():
            if path.suffix.lower() == ".pdf":
                resolved = path.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    result.append(resolved)
            else:
                print(f"Warning: {path} is not a PDF file", file=sys.stderr)
        elif path.is_dir():
            iterator = path.rglob("*") if recursive else path.glob("*")
            for candidate in sorted(iterator):
                if candidate.is_file() and candidate.suffix.lower() == ".pdf":
                    resolved = candidate.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        result.append(resolved)
        else:
            print(f"Warning: {path} does not exist", file=sys.stderr)
    return result


def _build_out_dir(pdf: Path, args: argparse.Namespace) -> Path:
    if args.output_dir:
        return Path(args.output_dir).expanduser() / pdf.stem
    return pdf.parent / pdf.stem


def _format_name(template: str, stem: str, page: int, total: int) -> str:
    width = max(3, len(str(total)))
    return template.format(stem=stem, page=page, total=total, w=width)


def _build_opts(args: argparse.Namespace) -> dict:
    return {
        "sort": args.sort,
        "password": args.password or "",
        "no_laparams": args.no_laparams,
        "line_overlap": args.line_overlap,
        "char_margin": args.char_margin,
        "line_margin": args.line_margin,
        "word_margin": args.word_margin,
        "boxes_flow": args.boxes_flow,
        "detect_vertical": args.detect_vertical,
        "all_texts": args.all_texts,
    }


def process_pdf(pdf: Path, args: argparse.Namespace) -> int:
    try:
        total = _page_count(str(pdf), args.backend, args.password or "")
    except Exception as exc:
        print(f"Error opening PDF {pdf}: {exc}", file=sys.stderr)
        return 0
    if total == 0:
        print(f"Skipping empty PDF: {pdf}")
        return 0
    if args.page_numbers:
        page_indices = [p - 1 for p in args.page_numbers if 1 <= p <= total]
    else:
        page_indices = list(range(total))
    if args.maxpages and args.maxpages > 0:
        page_indices = page_indices[: args.maxpages]
    if not page_indices:
        print(f"Nothing to extract from {pdf.name}")
        return 0
    out_dir = _build_out_dir(pdf, args)
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"Processing {pdf.name} ({len(page_indices)}/{total} pages) -> {out_dir}")
    to_process: list[int] = []
    skipped = 0
    for idx in page_indices:
        name = _format_name(args.name_template, pdf.stem, idx + 1, total)
        if args.skip_existing and (out_dir / name).exists():
            skipped += 1
            continue
        to_process.append(idx)
    if skipped:
        print(f"  Skipped {skipped} existing page file(s)")
    if not to_process:
        return 0
    workers = max(1, args.workers)
    if len(to_process) <= workers:
        chunks: list[list[int]] = [[idx] for idx in to_process]
    else:
        chunk_size = max(1, (len(to_process) + workers - 1) // workers)
        chunks = [to_process[i : i + chunk_size] for i in range(0, len(to_process), chunk_size)]
    opts = _build_opts(args)
    parallel = args.parallel
    if parallel == "auto":
        parallel = "thread" if args.backend in _THREAD_BACKENDS else "process"
    results: list[tuple[int, str]] = []
    try:
        if parallel == "none" or workers == 1 or len(chunks) == 1:
            for chunk in chunks:
                results.extend(_extract_chunk(str(pdf), chunk, args.backend, opts))
        elif parallel == "thread":
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(_extract_chunk, str(pdf), c, args.backend, opts) for c in chunks]
                for fut in as_completed(futures):
                    results.extend(fut.result())
        elif parallel == "process":
            with ProcessPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(_extract_chunk, str(pdf), c, args.backend, opts) for c in chunks]
                for fut in as_completed(futures):
                    results.extend(fut.result())
        else:
            msg = f"Unknown parallel mode: {parallel}"
            raise ValueError(msg)
    except Exception as exc:
        print(f"Error processing {pdf}: {exc}", file=sys.stderr)
        if not results:
            return 0
    written = 0
    for page_1based, text in sorted(results):
        name = _format_name(args.name_template, pdf.stem, page_1based, total)
        (out_dir / name).write_text(text, encoding=args.encoding)
        written += 1
    print(f"  Wrote {written} page file(s)")
    return written


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pdf_pages_to_txt.py",
        description="Extract every PDF page into its own .txt file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            (__doc__ or "").split("Original script equivalents", 1)[-1]
            if "Original script equivalents" in (__doc__ or "")
            else None
        ),
    )
    p.add_argument("inputs", nargs="*", help="PDF files and/or directories (default: cwd).")
    p.add_argument(
        "-b",
        "--backend",
        choices=sorted(_BACKENDS.keys()),
        default="pdfminer",
        help="PDF library backend (default: pdfminer).",
    )
    p.add_argument(
        "-o",
        "--output-dir",
        default=None,
        help="Base output directory (default: <pdf_dir>/<stem>/).",
    )
    p.add_argument(
        "-w",
        "--workers",
        type=int,
        default=4,
        help="Number of parallel workers (default: 4).",
    )
    p.add_argument(
        "-P",
        "--parallel",
        choices=["auto", "thread", "process", "none"],
        default="auto",
        help="Parallelism mode (default: auto — thread for fitz/plumber, process for pypdf/pypdf2/pdfminer).",
    )
    p.add_argument(
        "--name-template",
        default="page_{page:0{w}d}.txt",
        help="Output filename template. Placeholders: {stem} {page} {total} {w}. Default: page_{page:0{w}d}.txt",
    )
    p.add_argument(
        "-e",
        "--encoding",
        default="utf-8",
        help="Output file encoding (default: utf-8).",
    )
    p.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip pages whose output file already exists.",
    )
    p.add_argument(
        "--no-recursive",
        action="store_true",
        help="Do not descend into subdirectories.",
    )
    p.add_argument(
        "--sort",
        action="store_true",
        help="Sort text blocks top-to-bottom/left-to-right (fitz backend only).",
    )
    p.add_argument(
        "--password",
        default=None,
        help="Password for encrypted PDFs (pdfminer backend).",
    )
    p.add_argument("-m", "--maxpages", type=int, default=0, help="Maximum pages per PDF (0 = all).")
    p.add_argument(
        "-p",
        "--page-numbers",
        type=int,
        nargs="+",
        default=None,
        help="Specific 1-based page numbers to extract.",
    )
    p.add_argument("--no-laparams", action="store_true", help="Disable pdfminer layout analysis.")
    p.add_argument(
        "--line-overlap",
        type=float,
        default=0.5,
        help="pdfminer LAParams.line_overlap (default: 0.5).",
    )
    p.add_argument(
        "--char-margin",
        type=float,
        default=2.0,
        help="pdfminer LAParams.char_margin (default: 2.0).",
    )
    p.add_argument(
        "--line-margin",
        type=float,
        default=0.5,
        help="pdfminer LAParams.line_margin (default: 0.5).",
    )
    p.add_argument(
        "--word-margin",
        type=float,
        default=0.1,
        help="pdfminer LAParams.word_margin (default: 0.1).",
    )
    p.add_argument(
        "--boxes-flow",
        type=float,
        default=0.5,
        help="pdfminer LAParams.boxes_flow (default: 0.5).",
    )
    p.add_argument(
        "--detect-vertical",
        action="store_true",
        help="pdfminer LAParams.detect_vertical.",
    )
    p.add_argument("--all-texts", action="store_true", help="pdfminer LAParams.all_texts.")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    pdfs = _collect_pdfs(args.inputs, recursive=not args.no_recursive)
    if not pdfs:
        print("No PDF files found.", file=sys.stderr)
        return 1
    print(f"Found {len(pdfs)} PDF file(s) to process.")
    total_written = 0
    for i, pdf in enumerate(pdfs, 1):
        print(f"[{i}/{len(pdfs)}] {pdf.name}")
        total_written += process_pdf(pdf, args)
    print(f"Done. Total page files written: {total_written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""

_SOURCES['pages'] = r"""#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import argparse
import contextlib
import re
import shutil
import subprocess
import sys
import urllib.parse
from concurrent.futures import ProcessPoolExecutor
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional

import pymupdf


def human_size(n: float) -> str:
    sign = "-" if n < 0 else ""
    n = abs(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{sign}{n:.2f} {unit}"
        n /= 1024
    return f"{sign}{n:.2f} PB"


def run_cmd(cmd: list[str], show_output: bool = True) -> subprocess.CompletedProcess:
    if show_output:
        print(" ".join(str(c) for c in cmd))
    return subprocess.run(cmd, capture_output=not show_output, text=True)


def collect_pdfs(inputs: Iterable[str]) -> list[Path]:
    out: list[Path] = []
    for x in inputs:
        p = Path(x)
        if p.is_file() and p.suffix.lower() == ".pdf":
            out.append(p)
        elif p.is_dir():
            out.extend(p.rglob("*.pdf"))
    return out


def total_size(path: Path) -> int:
    p = Path(path)
    if p.is_file():
        return p.stat().st_size
    total = 0
    for f in p.rglob("*"):
        if f.is_file():
            with contextlib.suppress(OSError):
                total += f.stat().st_size
    return total


def find_ghostscript() -> Optional[str]:
    for name in ("gs", "gswin64c"):
        if shutil.which(name):
            return name
    return None


class TreeNode:
    def __init__(self, title: str, source_file: str, depth: int, index: int) -> None:
        self.title = title
        self.source_file = source_file
        self.depth = depth
        self.index = index
        self.parent: Optional["TreeNode"] = None
        self.children: list["TreeNode"] = []
        self.page: int = 0

    def is_root(self) -> bool:
        return self.parent is None

    def path_to_root(self) -> list[str]:
        parts: list[str] = []
        node: TreeNode = self
        while not node.is_root():
            parts.append(str(node.index + 1))
            node = node.parent
        return parts[::-1]

    def __str__(self) -> str:
        return "{}. {}".format(".".join(self.path_to_root()), self.title)


_SUMMARY_RE = re.compile(
    r"^(?P<indent> *)- \[(?P<title>[^\]]+)\]\((?P<src>[^)]+)\)",
    re.MULTILINE,
)


class _IdCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs) -> None:
        for k, v in attrs:
            if k == "id" and v:
                self.ids.add(v)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)


def _collect_html_ids(html: str) -> set[str]:
    collector = _IdCollector()
    collector.feed(html)
    return collector.ids


def _parse_summary(text: str) -> TreeNode:
    root = TreeNode("root", "", 0, 0)
    levels: dict[int, list[TreeNode]] = {0: [root]}
    min_indent = 4
    for m in _SUMMARY_RE.finditer(text):
        indent = len(m.group("indent"))
        if 0 < indent < min_indent:
            min_indent = indent
        depth = indent // min_indent
        node = TreeNode(m.group("title"), m.group("src"), depth + 1, 0)
        levels.setdefault(depth + 1, []).append(node)
        parent = levels[depth][-1]
        node.parent = parent
        node.index = len(parent.children)
        parent.children.append(node)
    return root


def _outline_id(node: TreeNode) -> str:
    n = node.source_file.removeprefix("./").split(".")[0].lower()
    return n.replace("/", "-").replace(" ", "-")


def _check_titles(base_dir: Path, node: TreeNode, sync: bool) -> bool:
    for child in node.children:
        if not _check_titles(base_dir, child, sync):
            return False
    if node.is_root():
        return True
    file_path = base_dir / node.source_file
    if not file_path.exists():
        print(f"File {file_path} does not exist")
        return False
    lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
    matched = False
    for lineno, line in enumerate(lines):
        if line.startswith("# "):
            title = line[2:]
            if not title.startswith(node.title):
                print(
                    f"[ERROR] Title not matched: source_file:{file_path}, "
                    f"line num:{lineno}, title:{title!r}, "
                    f"title in `SUMMARY.md`:{node.title!r}"
                )
                break
            matched = True
            break
    if not matched and sync:
        lines.insert(0, f"# {node.title}\n")
        print(f"[Info] Overwrite title as {node.title} in {node.source_file}")
        file_path.write_text("".join(lines), encoding="utf-8")
        matched = True
    return matched


def _resolve_named_dests(doc: pymupdf.Document) -> dict:
    result: dict = {}
    try:
        names = doc.resolve_names()
        if isinstance(names, dict):
            for name, dest in names.items():
                pno: Optional[int] = None
                if isinstance(dest, (list, tuple)) and dest:
                    first = dest[0]
                    if isinstance(first, int):
                        pno = first
                elif isinstance(dest, dict) and "page" in dest:
                    try:
                        pno = int(dest["page"])
                    except (TypeError, ValueError):
                        pno = None
                if pno is not None:
                    result[str(name)] = pno
    except Exception:
        pass
    for pno in range(len(doc)):
        try:
            page = doc[pno]
            links = page.get_links()
        except Exception:
            continue
        for link in links:
            nd = link.get("nameddest")
            if nd and nd not in result:
                result[nd] = pno
    return result


def _lookup_dest(dest_map: dict, oid: str) -> Optional[int]:
    quoted = urllib.parse.quote(oid)
    for candidate in (oid, "/" + oid, quoted, "/" + quoted):
        if candidate in dest_map:
            return int(dest_map[candidate])
    return None


def _build_toc(
    html_ids: set[str],
    dest_map: dict,
    root: TreeNode,
) -> list[list]:
    toc: list[list] = []

    def walk(n: TreeNode) -> None:
        if not n.is_root():
            oid = _outline_id(n)
            if oid not in html_ids:
                print(f"[ERROR] Element not found: [{oid}]")
            else:
                pno = _lookup_dest(dest_map, oid)
                if pno is None:
                    print(f"[ERROR] Named destination not found: [{oid}]")
                else:
                    n.page = pno + 1
                    toc.append([n.depth, str(n), n.page])
        for c in n.children:
            walk(c)

    walk(root)
    return toc


def cmd_outline(args: argparse.Namespace) -> int:
    html_path = Path(args.html_path)
    pdf_path = Path(args.pdf_path)
    summary_path = Path(args.summary_path)
    output_path = Path(args.output_path)
    for p in (html_path, pdf_path, summary_path):
        if not p.exists():
            raise FileNotFoundError(str(p))
    tree = _parse_summary(summary_path.read_text(encoding="utf-8"))
    if args.check_titles:
        _check_titles(Path(args.check_titles), tree, args.sync_titles)
    html_ids = _collect_html_ids(html_path.read_text(encoding="utf-8"))
    doc = pymupdf.open(str(pdf_path))
    dest_map = _resolve_named_dests(doc)
    toc = _build_toc(html_ids, dest_map, tree)
    if toc:
        doc.set_toc(toc)
    doc.save(str(output_path))
    doc.close()
    print(f"[INFO] Write to {output_path}")
    return 0


_NUM_SUFFIX_RE = re.compile(r"_(\d+)\.pdf$", re.IGNORECASE)


def _merge_sort_key(name: str):
    m = _NUM_SUFFIX_RE.search(name)
    if m:
        return (int(m.group(1)),)
    return (float("inf"),)


def cmd_merge(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    if args.inputs:
        files: list[Path] = []
        for raw in args.inputs:
            p = Path(raw)
            if p.is_file() and p.suffix.lower() == ".pdf":
                files.append(p)
            elif p.is_dir():
                files.extend(p.glob("*.pdf"))
        files = sorted(files, key=lambda p: _merge_sort_key(p.name))
    else:
        files = sorted(cwd.glob("*.pdf"), key=lambda p: _merge_sort_key(p.name))
    if not files:
        print("No PDF files found.")
        return 1
    out_doc = pymupdf.open()
    for f in files:
        with pymupdf.open(str(f)) as src:
            out_doc.insert_pdf(src)
    out_path = cwd / args.output
    out_doc.save(str(out_path))
    out_doc.close()
    print(f"Merged {len(files)} files into: {out_path}")
    return 0


def _convert_one_pdf(
    pdf_path: Path,
    out_root: Path,
    dpi: int,
) -> bool:
    target = out_root / pdf_path.stem
    try:
        print(f"Converting '{pdf_path.name}'...")
        target.mkdir(parents=True, exist_ok=True)
        doc = pymupdf.open(str(pdf_path))
        zoom = dpi / 72.0
        mat = pymupdf.Matrix(zoom, zoom)
        count = 0
        for i in range(len(doc)):
            page = doc[i]
            pix = page.get_pixmap(matrix=mat)
            out = target / f"{pdf_path.stem}_page_{i + 1}.jpg"
            pix.save(str(out))
            count += 1
        doc.close()
        print(f"Successfully converted '{pdf_path.name}' to {count} JPG files in '{target}'.")
        return True
    except Exception as e:
        print(f"Error converting '{pdf_path.name}': {e}")
        if target.exists():
            try:
                shutil.rmtree(target)
            except Exception as cleanup_e:
                print(f"Error during cleanup of '{target}': {cleanup_e}")
        return False


def cmd_to_img(args: argparse.Namespace) -> int:
    root = Path(args.directory).resolve()
    out_root = root / args.output_subdir
    out_root.mkdir(exist_ok=True)
    print(f"Starting PDF to JPG conversion in directory: {root}")
    print(f"Output will be saved in: {out_root}")
    succeeded = 0
    failed = 0
    for pdf in root.rglob("*"):
        if not (pdf.is_file() and pdf.suffix.lower() == ".pdf"):
            continue
        if out_root in pdf.parents:
            print(f"Skipping PDF '{pdf.name}' as it's within the output directory.")
            continue
        if _convert_one_pdf(pdf, out_root, args.dpi):
            if not args.keep:
                try:
                    pdf.unlink()
                    print(f"Removed original PDF: '{pdf.name}'")
                except OSError as e:
                    print(f"Error removing original PDF '{pdf.name}': {e}")
                    failed += 1
                    continue
            succeeded += 1
        else:
            failed += 1
    print("\n---Conversion Summary---")
    print(f"Successfully converted and removed: {succeeded} PDF files.")
    print(f"Failed to convert: {failed} PDF files.")
    print("------------------------")
    return 0 if failed == 0 else 1


def _rename_one(path_str: str) -> None:
    path = Path(path_str)
    if not path.exists() or path.is_symlink():
        return
    with pymupdf.open(str(path)) as doc:
        count = len(doc)
    new_name = f"{path.stem}{count}.pdf"
    print(new_name)
    new_path = path.parent / new_name
    if str(count) in path.stem:
        return
    if not new_path.exists():
        path.rename(new_path)
        print(f"{path.name} --> {new_path.name}")
    else:
        print(f"{new_path.name} exists.")


def cmd_rename_pages(args: argparse.Namespace) -> int:
    root = Path.cwd()
    files = [str(p) for p in root.rglob("*") if p.is_file() and p.suffix == ".pdf"]
    if not files:
        print("No PDF files found.")
        return 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for _ in ex.map(_rename_one, files):
            pass
    return 0


_GS_BASIC: list[str] = [
    "-sDEVICE=pdfwrite",
    "-dCompatibilityLevel=1.4",
    "-dDownsampleColorImages=true",
    "-dDownsampleGrayImages=true",
    "-dDownsampleMonoImages=true",
    "-dColorImageResolution=65",
    "-dGrayImageResolution=65",
    "-dMonoImageResolution=65",
    "-dColorImageDownsampleType=/Bicubic",
    "-dGrayImageDownsampleType=/Bicubic",
    "-dMonoImageDownsampleType=/Subsample",
    "-dNOPAUSE",
    "-dBATCH",
]
_GS_SCREEN: list[str] = [
    "-dBATCH",
    "-dColorConversionStrategy=/sRGB",
    "-dColorImageDownsampleType=/Bicubic",
    "-dColorImageResolution=85",
    "-dCompatibilityLevel=1.4",
    "-dDownsampleColorImages=true",
    "-dDownsampleGrayImages=true",
    "-dDownsampleMonoImages=true",
    "-dEmbedAllFonts=false",
    "-dGrayImageDownsampleType=/Bicubic",
    "-dGrayImageResolution=85",
    "-dHaveTransparency=false",
    "-dMonoImageDownsampleType=/Bicubic",
    "-dMonoImageResolution=85",
    "-dNOPAUSE",
    "-dOptimize=true",
    "-dPDFSETTINGS=/screen",
    "-dSubsetFonts=true",
    "-sDEVICE=pdfwrite",
]
_GS_MOBILE: list[str] = [
    "-sDEVICE=pdfwrite",
    "-dCompatibilityLevel=1.4",
    "-dPDFSETTINGS=/screen",
    "-dFastWebView=true",
    "-dNOPAUSE",
    "-dQUIET",
    "-dBATCH",
]


def _shrink_gs_basic(pdf: Path) -> None:
    gs = find_ghostscript()
    if not gs:
        print("[ERROR] Ghostscript not found in PATH.")
        return
    tmp = pdf.with_name(f"temp_gs_{pdf.name}")
    before = pdf.stat().st_size
    print(f"{pdf.name} Before : {human_size(before)}")
    cmd = [gs, *_GS_BASIC, f"-sOutputFile={tmp}", str(pdf)]
    run_cmd(cmd, show_output=False)
    if tmp.exists() and tmp.stat().st_size:
        after = tmp.stat().st_size
        print(f"{pdf.name} After  : {human_size(after)}")
        if after < before:
            tmp.replace(pdf)
            print(f"Saved  : {human_size(before - after)}")
        else:
            print("original file is smaller")
            tmp.unlink(missing_ok=True)


def _shrink_gs_screen(pdf: Path) -> None:
    gs = find_ghostscript()
    if not gs:
        print("[ERROR] Ghostscript not found in PATH.")
        return
    tmp = pdf.with_name(f"temp_gs_{pdf.name}")
    before = pdf.stat().st_size
    print(f"{pdf.name} Before : {human_size(before)}")
    cmd = [gs, *_GS_SCREEN, f"-sOutputFile={tmp}", str(pdf)]
    run_cmd(cmd, show_output=True)
    if tmp.exists() and tmp.stat().st_size:
        after = tmp.stat().st_size
        print(f"{pdf.name} After  : {human_size(after)}")
        if after < before:
            tmp.replace(pdf)
            print(f"Saved  : {human_size(before - after)}")
        else:
            print("original file is smaller")
            tmp.unlink(missing_ok=True)


def _shrink_gs_mobile(pdf: Path) -> None:
    gs = find_ghostscript()
    if not gs:
        print("[ERROR] Ghostscript ('gs' or 'gswin64c') is not installed or not in your PATH.")
        return
    before = pdf.stat().st_size
    print(f"Original size: {human_size(before)}")
    print("Compressing for mobile viewing... (72 DPI + Linearization)")
    tmp = pdf.with_suffix(pdf.suffix + ".tmp")
    cmd = [gs, *_GS_MOBILE, f"-sOutputFile={tmp}", str(pdf)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"Ghostscript Error:\n{r.stderr}")
            if tmp.exists():
                tmp.unlink()
            return
        after = tmp.stat().st_size
        if after < before:
            tmp.replace(pdf)
            print("Success! Inplace update complete.")
            print(f"New mobile-optimized size: {human_size(after)}")
            print(f"Saved: {(before - after) / before * 100:.1f}% space")
        else:
            print("Compression did not reduce file size. Original file kept intact.")
            if tmp.exists():
                tmp.unlink()
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        if tmp.exists():
            tmp.unlink()


def _shrink_qpdf(pdf: Path) -> None:
    if not shutil.which("qpdf"):
        print("[ERROR] qpdf not found in PATH.")
        return
    if not pdf.exists():
        print("Input file not found.", file=sys.stderr)
        return
    tmp = pdf.with_name(f"temp_qpdf_{pdf.name}")
    before = pdf.stat().st_size
    print(f"{pdf.name} Before : {human_size(before)}")
    cmd = ["qpdf", "--linearize", "--object-streams=generate", str(pdf), str(tmp)]
    run_cmd(cmd, show_output=True)
    if tmp.exists():
        after = tmp.stat().st_size
        print(f"{pdf.name} After  : {human_size(after)}")
        if after < before:
            tmp.replace(pdf)
            print(f"Saved  : {human_size(before - after)}")
        else:
            print("original file is smaller")
            tmp.unlink(missing_ok=True)


_SHRINK_ENGINES = {
    "gs-basic": _shrink_gs_basic,
    "gs-screen": _shrink_gs_screen,
    "gs-mobile": _shrink_gs_mobile,
    "qpdf": _shrink_qpdf,
}


def _shrink_dispatch(path_str: str, engine: str) -> None:
    _SHRINK_ENGINES[engine](Path(path_str))


def cmd_shrink(args: argparse.Namespace) -> int:
    files = collect_pdfs(args.inputs) if args.inputs else collect_pdfs([str(Path.cwd())])
    if not files:
        print("No PDF files found.")
        return 1
    fn = _SHRINK_ENGINES[args.engine]
    if len(files) == 1 or args.workers <= 1:
        before = total_size(Path.cwd())
        for f in files:
            fn(f)
        if len(files) > 1:
            freed = before - total_size(Path.cwd())
            if freed > 0:
                print(f"space freed: {human_size(freed)}")
        return 0
    before = total_size(Path.cwd())
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        list(
            ex.map(
                _shrink_dispatch,
                [str(f) for f in files],
                [args.engine] * len(files),
            )
        )
    freed = before - total_size(Path.cwd())
    if freed > 0:
        print(f"space freed: {human_size(freed)}")
    return 0


def _split_by_size(input_path: Path, output_dir: Path, max_mb: int) -> None:
    max_bytes = max_mb * 1024 * 1024
    src = pymupdf.open(str(input_path))
    stem = input_path.stem
    writer = pymupdf.open()
    part = 1
    for pno in range(len(src)):
        writer.insert_pdf(src, from_page=pno, to_page=pno)
        data = writer.tobytes()
        if len(data) > max_bytes and len(writer) > 1:
            writer.delete_page(len(writer) - 1)
            data = writer.tobytes()
            out = output_dir / f"{stem}_{part}.pdf"
            out.write_bytes(data)
            writer.close()
            writer = pymupdf.open()
            writer.insert_pdf(src, from_page=pno, to_page=pno)
            part += 1
    if len(writer) > 0:
        out = output_dir / f"{stem}_{part}.pdf"
        out.write_bytes(writer.tobytes())
    writer.close()
    src.close()


def cmd_split_size(args: argparse.Namespace) -> int:
    out_dir = Path(args.output).resolve()
    out_dir.mkdir(exist_ok=True)
    files = collect_pdfs(args.inputs) if args.inputs else list(Path.cwd().rglob("*.pdf"))
    if not files:
        print("No PDF files found.")
        return 1
    with ProcessPoolExecutor() as ex:
        futures = [ex.submit(_split_by_size, f, out_dir, args.max_mb) for f in files]
        for fut in futures:
            fut.result()
    print(f"Processing complete. Output files in: {out_dir}")
    return 0


def _split_by_page(input_path: Path, output_dir: Path) -> None:
    src = pymupdf.open(str(input_path))
    stem = input_path.stem
    total = len(src)
    pad = len(str(total))
    for idx in range(total):
        w = pymupdf.open()
        w.insert_pdf(src, from_page=idx, to_page=idx)
        name = f"{stem}_{str(idx + 1).zfill(pad)}.pdf"
        w.save(str(output_dir / name))
        w.close()
    src.close()


def cmd_split_page(args: argparse.Namespace) -> int:
    out_dir = Path(args.output).resolve()
    out_dir.mkdir(exist_ok=True)
    files = collect_pdfs(args.inputs) if args.inputs else list(Path.cwd().rglob("*.pdf"))
    if not files:
        print("No PDF files found.")
        return 1
    with ProcessPoolExecutor() as ex:
        futures = [ex.submit(_split_by_page, f, out_dir) for f in files]
        for fut in futures:
            fut.result()
    print(f"Processing complete. Output files in: {out_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="merged.py", description="Unified PDF toolkit.")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("outline", help="Add outline to a PDF using a mdBook SUMMARY.md.")
    p.add_argument("--html_path", default="print.html")
    p.add_argument("--pdf_path", default="output.pdf")
    p.add_argument("--summary_path", default="src/SUMMARY.md")
    p.add_argument("--output_path", default="output_with_outline.pdf")
    p.add_argument(
        "--check-titles",
        default=None,
        help="Base directory to check source titles against SUMMARY.md.",
    )
    p.add_argument(
        "--sync-titles",
        action="store_true",
        help="Overwrite mismatched titles when --check-titles is used.",
    )
    p.set_defaults(func=cmd_outline)
    p = sub.add_parser("merge", help="Merge PDF files.")
    p.add_argument("-o", "--output", default="merged.pdf")
    p.add_argument("inputs", nargs="*")
    p.set_defaults(func=cmd_merge)
    p = sub.add_parser("to-img", help="Convert PDFs to JPG images.")
    p.add_argument("--directory", default=".")
    p.add_argument("--output-subdir", default="output_jpgs")
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument(
        "--keep",
        action="store_true",
        help="Keep original PDFs after conversion (original deletes them).",
    )
    p.set_defaults(func=cmd_to_img)
    p = sub.add_parser("rename-pages", help="Rename PDFs appending their page count.")
    p.add_argument("--workers", type=int, default=8)
    p.set_defaults(func=cmd_rename_pages)
    p = sub.add_parser("shrink", help="Shrink PDF files with Ghostscript or qpdf.")
    p.add_argument("--engine", choices=sorted(_SHRINK_ENGINES.keys()), required=True)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("inputs", nargs="*")
    p.set_defaults(func=cmd_shrink)
    p = sub.add_parser("split-size", help="Split PDFs into chunks no larger than --max-mb.")
    p.add_argument("--max-mb", type=int, default=5)
    p.add_argument("--output", default="output")
    p.add_argument("inputs", nargs="*")
    p.set_defaults(func=cmd_split_size)
    p = sub.add_parser("split-page", help="Split PDFs into one file per page.")
    p.add_argument("--output", default="output")
    p.add_argument("inputs", nargs="*")
    p.set_defaults(func=cmd_split_page)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
"""

_SOURCES['resources'] = r"""#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import argparse
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

import pymupdf

if TYPE_CHECKING:
    from collections.abc import Sequence
MERGED_NAME: str = "merged.pdf"
UNSAFE: re.Pattern[str] = re.compile(r"[^A-Za-z0-9._-]+")


def count_pages(pdf_path: Path) -> int:
    with pymupdf.open(pdf_path) as document:
        return len(document)


def extract_one_page(job: tuple[str, int]) -> str:
    pdf_name, page_number = job
    with pymupdf.open(pdf_name) as document:
        page = document.load_page(page_number)
        return page.get_text("text").rstrip("\f")


def safe_name(value: str, fallback: str) -> str:
    cleaned = UNSAFE.sub("_", value).strip("._")
    return cleaned or fallback


def is_readable(path: Path) -> bool:
    try:
        with pymupdf.open(path) as document:
            return not document.needs_pass and len(document) > 0
    except Exception:
        return False


def chunked(items: Sequence[int], parts: int) -> list[list[int]]:
    size = max(1, -(-len(items) // max(1, parts)))
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def split_one_page(job: tuple[str, int, str]) -> str:
    pdf_name, page_number, target = job
    with pymupdf.open(pdf_name) as source, pymupdf.open() as output:
        output.insert_pdf(source, from_page=page_number, to_page=page_number)
        output.save(target, garbage=3, deflate=True)
    return target


def scan_resources(job: tuple[str, int, int]) -> tuple[list[int], list[int]]:
    pdf_name, start, end = job
    images: set[int] = set()
    fonts: set[int] = set()
    with pymupdf.open(pdf_name) as document:
        for number in range(start, end):
            images.update(item[0] for item in document.get_page_images(number, full=True))
            fonts.update(item[0] for item in document.get_page_fonts(number, full=True))
    images.discard(0)
    fonts.discard(0)
    return sorted(images), sorted(fonts)


def extract_images(job: tuple[str, list[int], str]) -> int:
    pdf_name, xrefs, target = job
    folder = Path(target)
    saved = 0
    with pymupdf.open(pdf_name) as document:
        for xref in xrefs:
            try:
                info = document.extract_image(xref)
            except (RuntimeError, ValueError):
                continue
            if not info or not info.get("image"):
                continue
            ext = str(info.get("ext") or "bin")
            (folder / f"image_{xref:06d}.{ext}").write_bytes(info["image"])
            saved += 1
    return saved


def extract_fonts(job: tuple[str, list[int], str]) -> int:
    pdf_name, xrefs, target = job
    folder = Path(target)
    saved = 0
    with pymupdf.open(pdf_name) as document:
        for xref in xrefs:
            try:
                name, ext, _kind, content = document.extract_font(xref)
            except (RuntimeError, ValueError):
                continue
            if not content or ext == "n/a":
                continue
            (folder / f"{safe_name(str(name), 'font')}_{xref:06d}.{ext}").write_bytes(content)
            saved += 1
    return saved


def extract_annotation_files(job: tuple[str, int, int, str]) -> int:
    pdf_name, start, end, target = job
    folder = Path(target)
    saved = 0
    with pymupdf.open(pdf_name) as document:
        for number in range(start, end):
            page = document.load_page(number)
            for index, annot in enumerate(page.annots(types=[pymupdf.PDF_ANNOT_FILE_ATTACHMENT])):
                try:
                    info = annot.file_info
                    content = annot.get_file()
                except (RuntimeError, ValueError):
                    continue
                filename = safe_name(str(info.get("filename") or ""), "attachment")
                (folder / f"page{number + 1:04d}_{index:02d}_{filename}").write_bytes(content)
                saved += 1
    return saved


def extract_embedded_files(pdf: Path, target: Path) -> int:
    saved = 0
    with pymupdf.open(pdf) as document:
        for index, name in enumerate(document.embfile_names()):
            info = document.embfile_info(name)
            raw_name = str(info.get("ufilename") or info.get("filename") or name)
            filename = safe_name(raw_name, f"embedded_{index}")
            (target / f"embedded_{index:03d}_{filename}").write_bytes(document.embfile_get(name))
            saved += 1
    return saved


def run_per_page(pdf: Path, out_root: Path, pool: ProcessPoolExecutor, jobs: int) -> int:
    total = count_pages(pdf)
    width = len(str(total))
    folder = out_root / pdf.stem
    folder.mkdir(parents=True, exist_ok=True)
    tasks = [(str(pdf), number) for number in range(total)]
    chunksize = max(1, total // (jobs * 4))
    results = pool.map(extract_one_page, tasks, chunksize=chunksize)
    for number, text in enumerate(results, start=1):
        (folder / f"{pdf.stem}_page_{number:0{width}d}.txt").write_text(text, encoding="utf-8")
    return total


def run_split(pdf: Path, out_root: Path, pool: ProcessPoolExecutor, jobs: int) -> int:
    total = count_pages(pdf)
    width = len(str(total))
    folder = out_root / pdf.stem
    folder.mkdir(parents=True, exist_ok=True)
    tasks = [(str(pdf), number, str(folder / f"{pdf.stem}_page_{number + 1:0{width}d}.pdf")) for number in range(total)]
    chunksize = max(1, total // (jobs * 4))
    for _ in pool.map(split_one_page, tasks, chunksize=chunksize):
        pass
    return total


def run_extract(pdf: Path, out_root: Path, pool: ProcessPoolExecutor, jobs: int) -> dict[str, int]:
    total = count_pages(pdf)
    base = out_root / pdf.stem
    folders: dict[str, Path] = {name: base / name for name in ("images", "fonts", "attachments")}
    for folder in folders.values():
        folder.mkdir(parents=True, exist_ok=True)
    ranges = [(part[0], part[-1] + 1) for part in chunked(range(total), jobs * 2)]
    image_xrefs: set[int] = set()
    font_xrefs: set[int] = set()
    for images, fonts in pool.map(scan_resources, [(str(pdf), start, end) for start, end in ranges]):
        image_xrefs.update(images)
        font_xrefs.update(fonts)
    image_jobs = [(str(pdf), part, str(folders["images"])) for part in chunked(sorted(image_xrefs), jobs)]
    font_jobs = [(str(pdf), part, str(folders["fonts"])) for part in chunked(sorted(font_xrefs), jobs)]
    annot_jobs = [(str(pdf), start, end, str(folders["attachments"])) for start, end in ranges]
    counts: dict[str, int] = {
        "images": sum(pool.map(extract_images, image_jobs)),
        "fonts": sum(pool.map(extract_fonts, font_jobs)),
        "attachments": extract_embedded_files(pdf, folders["attachments"])
        + sum(pool.map(extract_annotation_files, annot_jobs)),
    }
    for folder in folders.values():
        if not any(folder.iterdir()):
            folder.rmdir()
    if not any(base.iterdir()):
        base.rmdir()
    return counts


def merge_pdfs(pdfs: Sequence[Path], target: Path) -> int:
    target.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as merged:
        for pdf in pdfs:
            with pymupdf.open(pdf) as source:
                merged.insert_pdf(source)
        merged.save(target, garbage=3, deflate=True)
        return len(merged)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="pdftools",
        description="PDF utilities built on PyMuPDF: text extraction, split, merge and object extraction.",
    )
    parser.add_argument("inputs", nargs="+", type=Path, metavar="PDF", help="one or more input PDF files")
    parser.add_argument(
        "-p",
        "--per-page",
        action="store_true",
        help="extract the text of every page into separate .txt files in a folder named after the input stem",
    )
    parser.add_argument(
        "-s",
        "--split",
        action="store_true",
        help="split every input into one PDF per page, in a folder named after the input stem",
    )
    parser.add_argument(
        "-m",
        "--merge",
        action="store_true",
        help=f"merge all inputs, in the order given, into {MERGED_NAME}",
    )
    parser.add_argument(
        "-x",
        "--extract",
        action="store_true",
        help="extract embedded images, fonts and attached files into <stem>/images, fonts and attachments",
    )
    parser.add_argument("-o", "--output-dir", type=Path, default=Path(), help="base output directory")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1, help="number of worker processes")
    args = parser.parse_args(argv)
    if not (args.per_page or args.split or args.merge or args.extract):
        parser.error("choose at least one of -p, -s, -m, -x")
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    failed = False
    pdfs: list[Path] = []
    for path in args.inputs:
        if path.is_file() and is_readable(path):
            pdfs.append(path)
        else:
            print(f"skipped (missing, unreadable, empty or encrypted): {path}", file=sys.stderr)
            failed = True
    if not pdfs:
        return 1
    out_root: Path = args.output_dir
    out_root.mkdir(parents=True, exist_ok=True)
    jobs: int = args.jobs
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        for pdf in pdfs:
            try:
                if args.per_page:
                    pages = run_per_page(pdf, out_root, pool, jobs)
                    print(f"{pdf.name}: text of {pages} pages -> {out_root / pdf.stem}")
                if args.split:
                    pages = run_split(pdf, out_root, pool, jobs)
                    print(f"{pdf.name}: split into {pages} pdf files -> {out_root / pdf.stem}")
                if args.extract:
                    counts = run_extract(pdf, out_root, pool, jobs)
                    summary = ", ".join(f"{name}={count}" for name, count in counts.items())
                    print(f"{pdf.name}: extracted {summary} -> {out_root / pdf.stem}")
            except Exception as error:
                print(f"{pdf.name}: failed: {error}", file=sys.stderr)
                failed = True
    if args.merge:
        try:
            target = out_root / MERGED_NAME
            pages = merge_pdfs(pdfs, target)
            print(f"merged {len(pdfs)} files ({pages} pages) -> {target}")
        except Exception as error:
            print(f"merge failed: {error}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
"""

_SOURCES['view'] = r"""#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import argparse
import math
import os
import select
import shutil
import sys
import termios
import tty

try:
    import fitz
except ImportError:
    try:
        import pymupdf as fitz
    except ImportError:
        sys.exit("PyMuPDF is required:  pip install pymupdf")
RESET = "\x1b[0m"
HOME = "\x1b[H"
CLEAR = "\x1b[2J"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
ENTER_ALT = "\x1b[?1049h"
LEAVE_ALT = "\x1b[?1049l"
REVERSE = "\x1b[7m"
HALF_BLOCK = "\u2580"


def read_key(fd: int, timeout: float | None = None) -> str | None:
    ready, _, _ = select.select([fd], [], [], timeout)
    if not ready:
        return None
    first = os.read(fd, 1)
    if not first:
        return "q"
    if first != b"\x1b":
        return first.decode("utf-8", "replace")
    seq = bytearray(first)
    while len(seq) < 8:
        ready, _, _ = select.select([fd], [], [], 0.03)
        if not ready:
            break
        seq += os.read(fd, 1)
        if seq[-1:].isalpha() or seq[-1:] == b"~":
            break
    return seq.decode("latin-1")


class Viewer:
    MIN_ZOOM = 0.25
    MAX_ZOOM = 8.0
    ZOOM_STEP = 1.25
    CACHE_LIMIT = 6

    def __init__(self, path: str, page: int = 1, zoom: float = 1.0) -> None:
        self.path = path
        self.doc = fitz.open(path)
        if self.doc.page_count == 0:
            msg = "document contains no pages"
            raise ValueError(msg)
        self.page_index = max(0, min(page - 1, self.doc.page_count - 1))
        self.zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, zoom))
        self.x = 0
        self.y = 0
        self.running = True
        self._cache: dict[tuple[int, int], tuple[int, int, bytes]] = {}

    def term_size(self) -> tuple[int, int]:
        size = shutil.get_terminal_size((80, 24))
        return size.columns, size.lines

    def view_rows(self) -> int:
        _, lines = self.term_size()
        return max(1, lines - 1)

    def render_width(self) -> int:
        cols, _ = self.term_size()
        return max(1, round(cols * self.zoom))

    def page_px_size(self, index: int | None = None) -> tuple[int, int]:
        index = self.page_index if index is None else index
        width = self.render_width()
        rect = self.doc[index].rect
        scale = width / rect.width if rect.width else 1.0
        return width, max(1, math.ceil(rect.height * scale))

    def pixmap(self, index: int, width: int) -> tuple[int, int, bytes]:
        key = (index, width)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        page = self.doc[index]
        scale = width / page.rect.width if page.rect.width else 1.0
        pm = page.get_pixmap(
            matrix=fitz.Matrix(scale, scale),
            colorspace=fitz.csRGB,
            alpha=False,
        )
        data = (pm.width, pm.height, pm.samples)
        if len(self._cache) >= self.CACHE_LIMIT:
            self._cache.clear()
        self._cache[key] = data
        return data

    @staticmethod
    def _paint_row(data: bytes, w: int, h: int, top: int, x0: int, cols: int) -> str:
        bottom = top + 1
        have_top = 0 <= top < h
        have_bot = 0 <= bottom < h
        base_t = top * w * 3
        base_b = bottom * w * 3
        out: list[str] = []
        last_fg: tuple[int, int, int] | None = None
        last_bg: tuple[int, int, int] | None = None
        blank = False
        for i in range(cols):
            x = x0 + i
            if x >= w:
                if not blank:
                    out.append(RESET)
                    last_fg = last_bg = None
                    blank = True
                out.append(" ")
                continue
            blank = False
            if have_top:
                p = base_t + x * 3
                fg = (data[p], data[p + 1], data[p + 2])
            else:
                fg = (0, 0, 0)
            if have_bot:
                p = base_b + x * 3
                bg = (data[p], data[p + 1], data[p + 2])
            else:
                bg = (0, 0, 0)
            if fg != last_fg:
                out.append(f"\x1b[38;2;{fg[0]};{fg[1]};{fg[2]}m")
                last_fg = fg
            if bg != last_bg:
                out.append(f"\x1b[48;2;{bg[0]};{bg[1]};{bg[2]}m")
                last_bg = bg
            out.append(HALF_BLOCK)
        out.append(RESET)
        return "".join(out)

    def _status(self, cols: int) -> str:
        name = os.path.basename(self.path)
        total = self.doc.page_count
        _, h = self.page_px_size()
        max_y = max(0, h - self.view_rows() * 2)
        pct = 100 if max_y == 0 else round(100 * self.y / max_y)
        left = f" {name}  {self.page_index + 1}/{total}  {pct:3d}%  {self.zoom:.2f}x "
        right = " q quit  n/p page  j/k scroll  +/- zoom "
        if len(left) + len(right) <= cols:
            line = left + " " * (cols - len(left) - len(right)) + right
        else:
            line = left
        return REVERSE + line[:cols].ljust(cols) + RESET

    def draw(self) -> None:
        cols, _ = self.term_size()
        rows = self.view_rows()
        width = self.render_width()
        w, h, data = self.pixmap(self.page_index, width)
        max_y = max(0, h - rows * 2)
        self.y = max(0, min(self.y, max_y))
        max_x = max(0, w - cols)
        self.x = max(0, min(self.x, max_x))
        buf = [HOME]
        for row in range(rows):
            top = self.y + row * 2
            buf.append(self._paint_row(data, w, h, top, self.x, cols))
            if row != rows - 1:
                buf.append("\r\n")
        buf.append(RESET)
        buf.append(self._status(cols))
        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    def goto_page(self, index: int) -> None:
        if 0 <= index < self.doc.page_count:
            self.page_index = index
            self.x = self.y = 0

    def screen_down(self) -> None:
        rows = self.view_rows()
        step = rows * 2
        _, h = self.page_px_size()
        max_y = max(0, h - step)
        if self.y >= max_y:
            if self.page_index + 1 < self.doc.page_count:
                self.page_index += 1
                self.x = self.y = 0
        else:
            self.y = min(self.y + step, max_y)

    def screen_up(self) -> None:
        rows = self.view_rows()
        step = rows * 2
        if self.y <= 0:
            if self.page_index > 0:
                self.page_index -= 1
                self.x = 0
                _, h = self.page_px_size()
                self.y = max(0, h - step)
        else:
            self.y = max(0, self.y - step)

    def set_zoom(self, value: float) -> None:
        value = max(self.MIN_ZOOM, min(self.MAX_ZOOM, value))
        if value == self.zoom:
            return
        _, old_h = self.page_px_size()
        frac = self.y / old_h if old_h else 0.0
        self.zoom = value
        _, new_h = self.page_px_size()
        self.y = int(frac * new_h)

    def handle(self, key: str) -> None:
        if key in ("q", "Q", "\x03") or key == "\x1b":
            self.running = False
        elif key in ("j", "\x1b[B", "\n", "\r"):
            self.y += 2
        elif key in ("k", "\x1b[A"):
            self.y -= 2
        elif key in ("h", "\x1b[D"):
            self.x -= 4
        elif key in ("l", "\x1b[C"):
            self.x += 4
        elif key in (" ", "\x1b[6~", "f", "J"):
            self.screen_down()
        elif key in ("b", "\x1b[5~", "K"):
            self.screen_up()
        elif key == "n":
            self.goto_page(self.page_index + 1)
        elif key in ("N", "p"):
            self.goto_page(self.page_index - 1)
        elif key in ("g", "\x1b[H", "\x1b[1~", "\x1b[7~", "\x1bOH"):
            self.x = self.y = 0
        elif key in ("G", "\x1b[F", "\x1b[4~", "\x1b[8~", "\x1bOF"):
            self.y = 1 << 30
        elif key in ("+", "="):
            self.set_zoom(self.zoom * self.ZOOM_STEP)
        elif key in ("-", "_"):
            self.set_zoom(self.zoom / self.ZOOM_STEP)
        elif key == "0":
            self.zoom = 1.0
            self.x = self.y = 0

    def run(self, fd: int) -> None:
        dirty = True
        last_size = (0, 0)
        while self.running:
            size = self.term_size()
            if size != last_size:
                last_size = size
                self._cache.clear()
                dirty = True
            if dirty:
                self.draw()
                dirty = False
            key = read_key(fd, 0.25)
            if key is None:
                continue
            self.handle(key)
            dirty = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tpv",
        description="View a PDF file in the terminal.",
    )
    parser.add_argument("file", help="path to the PDF file")
    parser.add_argument(
        "-p",
        "--page",
        type=int,
        default=1,
        help="page to open first (1-based, default 1)",
    )
    parser.add_argument(
        "-z",
        "--zoom",
        type=float,
        default=1.0,
        help="initial zoom factor (default 1.0)",
    )
    args = parser.parse_args(argv)
    if not os.path.isfile(args.file):
        parser.error(f"no such file: {args.file}")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("must be run from an interactive terminal")
    try:
        viewer = Viewer(args.file, args.page, args.zoom)
    except Exception as exc:
        sys.exit(f"tpv: could not open {args.file!r}: {exc}")
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        sys.stdout.write(ENTER_ALT + HIDE_CURSOR + CLEAR + HOME)
        sys.stdout.flush()
        viewer.run(fd)
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write(RESET + SHOW_CURSOR + LEAVE_ALT)
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""

_SOURCES['img'] = r'''#!/data/data/com.termux/files/usr/bin/env python
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import pymupdf


def convert_pdf_to_jpg(pdf_path: Path, output_folder: Path) -> bool:
    pdf_output_dir: Path = output_folder / pdf_path.stem
    try:
        print(f"Converting '{pdf_path.name}'...")
        pdf_output_dir.mkdir(parents=True, exist_ok=True)
        converted_files: list[Path] = []
        with pymupdf.open(pdf_path) as document:
            for page_index, page in enumerate(document):
                pixmap: pymupdf.Pixmap = page.get_pixmap(dpi=300)
                final_jpg_path: Path = pdf_output_dir / f"{pdf_path.stem}_page_{page_index + 1}.jpg"
                pixmap.save(str(final_jpg_path), jpg_quality=95)
                converted_files.append(final_jpg_path)
        print(f"Successfully converted '{pdf_path.name}' to {len(converted_files)} JPG files in '{pdf_output_dir}'.")
        return True
    except Exception as e:
        print(f"Error converting '{pdf_path.name}': {e}")
        if pdf_output_dir.exists():
            try:
                shutil.rmtree(pdf_output_dir)
            except Exception as cleanup_e:
                print(f"Error during cleanup of '{pdf_output_dir}': {cleanup_e}")
        return False


def process_directory(start_dir: Path, output_base_dir: Path, keep_originals: bool = False) -> int:
    print(f"Starting PDF to JPG conversion in directory: {start_dir}")
    print(f"Output will be saved in: {output_base_dir}")
    converted_count: int = 0
    failed_count: int = 0
    for item in start_dir.rglob("*"):
        if item.is_file() and item.suffix.lower() == ".pdf":
            if output_base_dir in item.parents:
                print(f"Skipping PDF '{item.name}' as it's within the output directory.")
                continue
            if convert_pdf_to_jpg(item, output_base_dir):
                if keep_originals:
                    converted_count += 1
                    continue
                try:
                    item.unlink()
                    print(f"Removed original PDF: '{item.name}'")
                    converted_count += 1
                except OSError as e:
                    print(f"Error removing original PDF '{item.name}': {e}")
                    failed_count += 1
            else:
                failed_count += 1
    print("\n--- Conversion Summary ---")
    verb = "converted" if keep_originals else "converted and removed"
    print(f"Successfully {verb}: {converted_count} PDF files.")
    print(f"Failed to convert: {failed_count} PDF files.")
    print("------------------------")
    return 1 if failed_count else 0


def main(argv: list[str] | None = None) -> int:
    """Entry point (the original script only had an ``if __name__`` block)."""
    parser = argparse.ArgumentParser(
        prog="pdf2img",
        description="Render every PDF under a directory to per-page 300 dpi JPGs.",
    )
    parser.add_argument("root", nargs="?", default=".", help="Directory to scan (default: .).")
    parser.add_argument("-o", "--output", default=None, help="Output directory (default: <root>/output_jpgs).")
    parser.add_argument(
        "-k",
        "--keep-originals",
        action="store_true",
        help="Keep the source PDFs (without this flag they are deleted after conversion).",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        parser.error(f"not a directory: {root}")
    output_directory = Path(args.output).expanduser().resolve() if args.output else root / "output_jpgs"
    output_directory.mkdir(parents=True, exist_ok=True)
    rc = process_directory(root, output_directory, args.keep_originals)
    print("\nScript finished.")
    print(f"Converted JPG files are located in: {output_directory}")
    return rc


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
