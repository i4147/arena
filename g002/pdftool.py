#!/usr/bin/env python3
"""pdftool.py — one front end for twelve PDF scripts.

g002 held four tools wearing twelve names:

    PDF -> text      pdf2text, pdftotext, pdf2page, pdftools -p
    PDF -> images    pdf2img, pdf2png, pdftoolz to-img
    terminal viewer  pdfv, pdfv2, pdfviewer, pdfview
    surgery          pdftools (split/merge/extract), pdftools2/pdftoolz (outline,
                     shrink, rename, split-size)

Every backend is optional and probed at run time; ``pdftool.py doctor`` says what
is usable here.  No single library is required: text extraction degrades through
pymupdf -> pypdf -> pdfminer -> poppler -> OCR.

    pdftool.py doctor
    pdftool.py text  report.pdf --per-page -o out/
    pdftool.py image scans/ --dpi 200 --format jpg
    pdftool.py view  report.pdf
    pdftool.py merge a.pdf b.pdf -o merged.pdf
    pdftool.py split report.pdf --max-mb 5
    pdftool.py check report.pdf          # every page parses and yields text
    pdftool.py outline --html print.html --summary src/SUMMARY.md --sync-titles

Common flags: -n/--dry-run · --backup · -q/-v · --json · --exclude GLOB

Exit codes: 0 ok · 1 errors · 2 usage · 130 interrupted
"""

from __future__ import annotations

import argparse
import fnmatch
import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "1.0.0"
PROG = "pdftool.py"

TEXT_ENGINES = ("fitz", "pypdf", "pdfminer", "plumber", "pypdf2", "poppler", "ocr")
IMAGE_ENGINES = ("fitz", "poppler")
SHRINK_ENGINES = ("gs", "qpdf", "mutool")

_PROBE: dict[str, object | None] = {}


def probe(name: str) -> object | None:
    if name not in _PROBE:
        module: object | None = None
        try:
            if importlib.util.find_spec(name) is not None:
                module = importlib.import_module(name)
        except Exception:  # noqa: BLE001 - broken optional dep must not kill us
            module = None
        _PROBE[name] = module
    return _PROBE[name]


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def fitz_module() -> object | None:
    return probe("pymupdf") or probe("fitz")


# --------------------------------------------------------------------------- #
# pages, ranges, names                                                        #
# --------------------------------------------------------------------------- #


class PageSpecError(ValueError):
    """Bad ``--pages`` value: a usage error, not a per-file failure."""


def parse_pages(spec: str, total: int) -> list[int]:
    """``"1,3-5,8-"`` -> zero-based page indices (1-based, inclusive)."""
    pages: list[int] = []
    for raw in spec.replace(" ", "").split(","):
        if not raw:
            continue
        head, sep, tail = raw.partition("-")
        try:
            if sep:
                start = int(head) if head else 1
                stop = int(tail) if tail else total
            else:
                start = stop = int(head)
        except ValueError:
            raise PageSpecError(f"{raw!r} is not a page number or range") from None
        if start < 1 or stop < start:
            raise PageSpecError(f"{raw!r}: expected 1 <= first <= last")
        if start > total:
            raise PageSpecError(f"{raw!r}: only {total} page(s) in the document")
        pages.extend(range(start - 1, min(stop, total)))
    pages = list(dict.fromkeys(pages))
    if not pages:
        raise PageSpecError(f"{spec!r} selects no page of {total}")
    return pages


UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_name(value: str, fallback: str = "file") -> str:
    cleaned = UNSAFE.sub("_", value).strip("._")
    return cleaned or fallback


def human(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(size) < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


@dataclass
class Ctx:
    """Per-run switches shared by every subcommand."""

    command: str = ""
    dry_run: bool = False
    backup: bool = False
    quiet: bool = False
    verbose: bool = False
    json: bool = False
    started: float = field(default_factory=time.perf_counter)
    files: list[dict] = field(default_factory=list)
    failures: int = 0


CTX = Ctx()


def say(message: str) -> None:
    """Human progress line: silent under --json and --quiet."""
    if not CTX.quiet and not CTX.json:
        print(message)


def note(message: str) -> None:
    if CTX.verbose:
        print(message, file=sys.stderr)


def deliver(target: Path, writer, *, suffix: str | None = None) -> Path | None:
    """Write ``target`` atomically, honouring --dry-run and --backup.

    ``writer`` receives a temporary path that keeps the real suffix (fitz and
    pdftoppm pick the output format from it) and must write exactly that file.
    """
    if CTX.dry_run:
        say(f"[dry-run] would write {target}")
        CTX.files.append({"path": str(target), "action": "would-write"})
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    tail = suffix if suffix is not None else target.suffix
    tmp = target.with_name(f".{target.stem}.{os.getpid()}.tmp{tail}")
    try:
        writer(tmp)
        if target.exists() and CTX.backup:
            shutil.copy2(target, target.with_suffix(target.suffix + ".bak"))
            note(f"backup: {target.with_suffix(target.suffix + '.bak')}")
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    size = target.stat().st_size
    CTX.files.append({"path": str(target), "bytes": size, "action": "wrote"})
    note(f"wrote {target} ({human(size)})")
    return target


def write_bytes(data: bytes):
    def writer(tmp: Path) -> None:
        tmp.write_bytes(data)

    return writer


def write_text(text: str):
    def writer(tmp: Path) -> None:
        tmp.write_text(text, encoding="utf-8")

    return writer


@dataclass(frozen=True, slots=True)
class Doc:
    path: Path
    pages: int
    encrypted: bool = False

    @property
    def title(self) -> str:
        return self.path.stem


def open_doc(path: Path) -> Doc:
    """Page count + encryption flag with whichever library is present."""
    module = fitz_module()
    if module is not None:
        with module.open(path) as document:
            return Doc(path, len(document), bool(getattr(document, "needs_pass", False)))
    pypdf = probe("pypdf") or probe("PyPDF2")
    if pypdf is not None:
        with path.open("rb") as handle:
            reader = pypdf.PdfReader(handle)
            return Doc(path, len(reader.pages), bool(getattr(reader, "is_encrypted", False)))
    if have("pdfinfo"):
        out = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True)
        match = re.search(r"^Pages:\s*(\d+)", out.stdout, re.MULTILINE)
        if match:
            return Doc(path, int(match.group(1)))
    raise RuntimeError("no PDF library and no poppler: pip install pymupdf (or pypdf)")


def collect_pdfs(paths: Sequence[Path], recursive: bool = True,
                 exclude: Sequence[str] = ()) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for raw in paths or [Path.cwd()]:
        path = raw.expanduser()
        if not path.exists():
            continue
        candidates = (
            [path] if path.is_file()
            else sorted(path.rglob("*.pdf")) if recursive
            else sorted(path.glob("*.pdf"))
        )
        for candidate in candidates:
            if candidate.suffix.lower() != ".pdf" or candidate.is_symlink():
                continue
            if any(fnmatch.fnmatch(str(candidate), pat) or fnmatch.fnmatch(candidate.name, pat)
                   for pat in exclude):
                continue
            key = candidate.resolve()
            if key not in seen:
                seen.add(key)
                out.append(candidate)
    return out


# --------------------------------------------------------------------------- #
# text extraction                                                             #
# --------------------------------------------------------------------------- #


def extract_page(path: Path, page: int, engine: str, password: str = "", sort: bool = False) -> str:
    if engine == "poppler":
        return _poppler_page(path, page)
    if engine == "ocr":
        return _ocr_page(path, page)
    if engine == "fitz":
        module = fitz_module()
        if module is None:
            raise RuntimeError("pymupdf not installed")
        with module.open(path) as document:
            target = document.load_page(page)
            return target.get_text("text", sort=sort) if sort else target.get_text("text")
    if engine in ("pypdf", "pypdf2"):
        module = probe("pypdf") if engine == "pypdf" else probe("PyPDF2")
        if module is None:
            raise RuntimeError(f"{engine} not installed")
        reader = module.PdfReader(str(path), password=password) if password else module.PdfReader(str(path))
        return reader.pages[page].extract_text() or ""
    if engine == "pdfminer":
        from pdfminer.high_level import extract_text

        return extract_text(str(path), page_numbers=[page], password=password) or ""
    if engine == "plumber":
        plumber = probe("pdfplumber")
        if plumber is None:
            raise RuntimeError("pdfplumber not installed")
        with plumber.open(path) as pdf:
            return pdf.pages[page].extract_text() or ""
    raise ValueError(f"unknown engine: {engine}")


def _poppler_page(path: Path, page: int) -> str:
    if not have("pdftotext"):
        raise RuntimeError("pdftotext not found: apt install poppler-utils")
    out = subprocess.run(
        ["pdftotext", "-f", str(page + 1), "-l", str(page + 1), "-layout", str(path), "-"],
        capture_output=True, text=True,
    )
    return out.stdout


def _ocr_page(path: Path, page: int) -> str:
    module = importlib.util.find_spec("pytesseract")
    if module is None or not have("tesseract"):
        raise RuntimeError("OCR needs pytesseract + tesseract")
    import pytesseract

    fitz = fitz_module()
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "page.png"
        with fitz.open(path) as document:
            document.load_page(page).get_pixmap(dpi=300).save(str(target))
        return pytesseract.image_to_string(str(target))


def extract_text(path: Path, pages: Sequence[int], engine: str, password: str = "", sort: bool = False,
                 jobs: int = 4) -> list[str]:
    if engine == "auto":
        engine = pick_text_engine()
    if jobs > 1 and len(pages) > 1:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            return list(pool.map(lambda page: extract_page(path, page, engine, password, sort), pages))
    return [extract_page(path, page, engine, password, sort) for page in pages]


def pick_text_engine(preferred: str = "auto") -> str:
    if preferred != "auto":
        return preferred
    if fitz_module() is not None:
        return "fitz"
    if probe("pypdf") is not None:
        return "pypdf"
    if probe("pdfminer") is not None:
        return "pdfminer"
    if probe("pdfplumber") is not None:
        return "plumber"
    if have("pdftotext"):
        return "poppler"
    raise RuntimeError("no usable PDF text engine: pip install pymupdf (or pypdf / pdfminer.six)")


# --------------------------------------------------------------------------- #
# images                                                                      #
# --------------------------------------------------------------------------- #


def render_page(path: Path, page: int, dpi: int) -> tuple[Path, object]:
    fitz = fitz_module()
    if fitz is None:
        raise RuntimeError("rendering needs pymupdf")
    with fitz.open(path) as document:
        pixmap = document.load_page(page).get_pixmap(dpi=dpi)
        handle, name = tempfile.mkstemp(suffix=".png")
        os.close(handle)
        target = Path(name)
        pixmap.save(str(target))
    return target, pixmap


def image_name(stem: str, index: int, fmt: str, digits: int) -> str:
    return f"{stem}_page_{index + 1:0{digits}d}.{fmt}"


def convert_images(pdf: Path, out_dir: Path, dpi: int, fmt: str, quality: int,
                   engine: str, pages: Sequence[int] | None, jobs: int) -> list[Path]:
    doc = open_doc(pdf)
    targets = list(pages) if pages is not None else list(range(doc.pages))
    digits = max(3, len(str(doc.pages)))
    if engine == "poppler":
        return _poppler_images(pdf, out_dir, dpi, fmt, targets, digits)
    fitz = fitz_module()
    if fitz is None:
        raise RuntimeError("image conversion needs pymupdf, or poppler's pdftoppm")
    written: list[Path] = []

    def render(index: int) -> Path:
        target = out_dir / image_name(pdf.stem, index, fmt, digits)

        def writer(tmp: Path) -> None:
            with fitz.open(pdf) as document:
                pixmap = document.load_page(index).get_pixmap(dpi=dpi)
                if fmt == "jpg":
                    pixmap.save(str(tmp), jpg_quality=quality)
                else:
                    pixmap.save(str(tmp))

        deliver(target, writer, suffix=f".{fmt}")
        return target

    if jobs > 1 and len(targets) > 1:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            written = list(pool.map(render, targets))
    else:
        written = [render(index) for index in targets]
    return written


def _poppler_images(pdf: Path, out_dir: Path, dpi: int, fmt: str, targets: Sequence[int], digits: int) -> list[Path]:
    if not have("pdftoppm"):
        raise RuntimeError("pdftoppm not found: apt install poppler-utils")
    written: list[Path] = []
    for index in targets:
        target = out_dir / image_name(pdf.stem, index, fmt, digits)
        flag = "-jpeg" if fmt == "jpg" else "-png"

        def writer(tmp: Path, index: int = index, flag: str = flag) -> None:
            subprocess.run(
                ["pdftoppm", flag, "-r", str(dpi), "-f", str(index + 1), "-l", str(index + 1),
                 "-singlefile", str(pdf), str(tmp.with_suffix(""))],
                check=True, capture_output=True,
            )

        deliver(target, writer, suffix=f".{fmt}")
        written.append(target)
    return written


# --------------------------------------------------------------------------- #
# merge / split / extract / shrink                                            #
# --------------------------------------------------------------------------- #


def merge(pdfs: Sequence[Path], target: Path) -> int:
    fitz = fitz_module()
    if fitz is not None:
        with fitz.open() as merged:
            for pdf in pdfs:
                with fitz.open(pdf) as source:
                    merged.insert_pdf(source)
            pages = len(merged)
            deliver(target, lambda tmp: merged.save(tmp, garbage=3, deflate=True), suffix=".pdf")
            return pages
    pypdf = probe("pypdf")
    if pypdf is None:
        raise RuntimeError("merging needs pymupdf or pypdf")
    writer = pypdf.PdfWriter()
    for pdf in pdfs:
        reader = pypdf.PdfReader(str(pdf))
        for page in reader.pages:
            writer.add_page(page)
    deliver(target, lambda tmp: writer.write(str(tmp)), suffix=".pdf")
    return len(writer.pages)


def split_pages(pdf: Path, out_dir: Path, pages: Sequence[int] | None) -> list[Path]:
    doc = open_doc(pdf)
    digits = max(1, len(str(doc.pages)))
    targets = list(pages) if pages is not None else list(range(doc.pages))
    directory = out_dir / pdf.stem if len(targets) > 1 else out_dir
    written: list[Path] = []
    for index in targets:
        target = directory / f"{pdf.stem}_page_{index + 1:0{digits}d}.pdf"
        deliver(target, lambda tmp, index=index: _write_slice(pdf, [index], tmp), suffix=".pdf")
        written.append(target)
    return written


def split_by_size(pdf: Path, out_dir: Path, max_mb: int) -> list[Path]:
    """Greedy page packing that never exceeds ``max_mb``."""
    doc = open_doc(pdf)
    budget = max_mb * 1024 * 1024
    chunks: list[list[int]] = []
    current: list[int] = []
    for index in range(doc.pages):
        current.append(index)
        if len(current) > 1:
            try:
                size = _slice_size(pdf, current)
            except Exception:  # noqa: BLE001 - unreadable page: keep packing
                continue
            if size > budget:
                current.pop()
                chunks.append(current)
                current = [index]
    if current:
        chunks.append(current)
    written: list[Path] = []
    for number, chunk in enumerate(chunks, 1):
        target = out_dir / f"{pdf.stem}_part{number:02d}.pdf"
        deliver(target, lambda tmp, chunk=chunk: _write_slice(pdf, chunk, tmp), suffix=".pdf")
        written.append(target)
    return written


def _write_slice(pdf: Path, pages: Sequence[int], target: Path) -> None:
    fitz = fitz_module()
    if fitz is not None:
        with fitz.open(pdf) as source, fitz.open() as out:
            for index in pages:
                out.insert_pdf(source, from_page=index, to_page=index)
            out.save(target, garbage=3, deflate=True)
        return
    pypdf = probe("pypdf")
    if pypdf is None:
        raise RuntimeError("needs pymupdf or pypdf")
    reader = pypdf.PdfReader(str(pdf))
    writer = pypdf.PdfWriter()
    for index in pages:
        writer.add_page(reader.pages[index])
    with target.open("wb") as handle:
        writer.write(handle)


def _slice_size(pdf: Path, pages: Sequence[int]) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "slice.pdf"
        _write_slice(pdf, pages, target)
        return target.stat().st_size


def extract_objects(pdf: Path, out_dir: Path) -> dict[str, int]:
    fitz = fitz_module()
    if fitz is None:
        raise RuntimeError("object extraction needs pymupdf")
    counts = {"images": 0, "fonts": 0, "attachments": 0}
    images = out_dir / "images"
    fonts = out_dir / "fonts"
    attached = out_dir / "attachments"
    with fitz.open(pdf) as document:
        for xref in range(1, document.xref_length()):
            try:
                kind = document.xref_get_key(xref, "Subtype")[1]
            except Exception:  # noqa: BLE001
                continue
            if kind == "/Image":
                data = document.extract_image(xref)
                if not data or not data.get("image"):
                    continue
                name = images / safe_name(f"{pdf.stem}_x{xref}.{data['ext']}")
                deliver(name, write_bytes(data["image"]), suffix=f".{data['ext']}")
                counts["images"] += 1
            elif kind in ("/Type1", "/TrueType", "/Type0"):
                base, ext, _type, content = document.extract_font(xref)
                if not content:
                    continue
                name = fonts / f"{safe_name(base or f'font_x{xref}')}.{safe_name(ext or 'bin')}"
                deliver(name, write_bytes(content))
                counts["fonts"] += 1
        for info in document.embfile_names() or []:
            name = attached / safe_name(info)
            deliver(name, write_bytes(document.embfile_get(info)))
            counts["attachments"] += 1
    return counts


def shrink_command(engine: str, level: int) -> list[str]:
    """The argv prefix for ``engine``, with the output path appended last."""
    if engine == "gs":
        if not have("gs"):
            raise RuntimeError("ghostscript not found: apt install ghostscript")
        quality = {1: "prepress", 2: "ebook", 3: "screen"}.get(level, "ebook")
        return ["gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.5", f"-dPDFSETTINGS=/{quality}",
                "-dNOPAUSE", "-dBATCH", "-dQUIET"]
    if engine == "qpdf":
        if not have("qpdf"):
            raise RuntimeError("qpdf not found: apt install qpdf")
        return ["qpdf", "--linearize", "--object-streams=generate", "--recompress-flate",
                f"--compression-level={9 if level == 3 else 6}"]
    if engine == "mutool":
        if not have("mutool"):
            raise RuntimeError("mutool not found: apt install mupdf-tools")
        return ["mutool", "clean", "-gggg", "-z"]
    raise ValueError(f"unknown shrink engine: {engine}")


def shrink(pdf: Path, out_dir: Path, engine: str, level: int = 2) -> Path:
    target = out_dir / pdf.name
    argv = shrink_command(engine, level)

    def writer(tmp: Path) -> None:
        command = ([*argv, f"-sOutputFile={tmp}", str(pdf)] if engine == "gs"
                   else [*argv, str(pdf), str(tmp)])
        subprocess.run(command, check=True, capture_output=True)

    deliver(target, writer, suffix=".pdf")
    return target


# --------------------------------------------------------------------------- #
# outline from a mdBook SUMMARY.md                                            #
# --------------------------------------------------------------------------- #


@dataclass
class Node:
    title: str
    src: str = ""
    level: int = 1
    children: list["Node"] = field(default_factory=list)

    def walk(self) -> Iterator["Node"]:
        yield self
        for child in self.children:
            yield from child.walk()


SUMMARY_RE = re.compile(r"^(?P<indent> *)- \[(?P<title>[^\]]+)\]\((?P<src>[^)]+)\)", re.MULTILINE)
MD_HEADING_RE = re.compile(r"^#{2,6}\s+(?P<title>.+?)\s*$", re.MULTILINE)
MD_TITLE_RE = re.compile(r"^#\s+(?P<title>.+?)\s*$", re.MULTILINE)
HTML_HEADING_RE = re.compile(r"<h[2-6][^>]*>(?P<title>.*?)</h[2-6]>", re.IGNORECASE | re.DOTALL)
HTML_TITLE_RE = re.compile(r"<h1[^>]*>(?P<title>.*?)</h1>", re.IGNORECASE | re.DOTALL)


def parse_summary(text: str) -> Node:
    root = Node("root")
    stack: list[tuple[int, Node]] = [(-1, root)]
    for match in SUMMARY_RE.finditer(text):
        depth = len(match.group("indent"))
        node = Node(match.group("title").strip(), match.group("src").strip(), level=depth // 2 + 1)
        while stack and stack[-1][0] >= depth:
            stack.pop()
        stack[-1][1].children.append(node)
        stack.append((depth, node))
    return root


def collect_headings(html: str) -> list[str]:
    """Chapter titles: h2..h6 (h1 is the document title, not a chapter)."""
    html_hits = [re.sub(r"<[^>]+>", "", m.group("title")).strip() for m in HTML_HEADING_RE.finditer(html)]
    if html_hits:
        return [title for title in html_hits if title]
    plain = re.sub(r"<h1[^>]*>.*?</h1>", "", html, flags=re.IGNORECASE | re.DOTALL)
    return [m.group("title").strip() for m in MD_HEADING_RE.finditer(plain)]


def document_title(html: str) -> str:
    match = HTML_TITLE_RE.search(html)
    if match:
        return re.sub(r"<[^>]+>", "", match.group("title")).strip()
    match = MD_TITLE_RE.search(html)
    return match.group("title").strip() if match else ""


def sync_titles(root: Node, headings: Sequence[str]) -> int:
    """Rename outline entries to the headings actually present in the HTML.

    ``pdftoolz outline --sync-titles``: the summary headings and the rendered
    chapter titles drift apart over time; this heals the summary side.
    """
    nodes = list(root.walk())[1:]
    if len(nodes) != len(headings):
        print(
            f"sync-titles skipped: {len(nodes)} summary entries vs {len(headings)} headings",
            file=sys.stderr,
        )
        return 0
    changed = 0
    for node, heading in zip(nodes, headings):
        if node.title != heading:
            node.title = heading
            changed += 1
    return changed


def check_titles(root: Node, headings: Sequence[str]) -> list[str]:
    problems: list[str] = []
    nodes = list(root.walk())[1:]
    for index, (node, heading) in enumerate(zip(nodes, headings), 1):
        if node.title != heading:
            problems.append(f"{index}: summary={node.title!r} html={heading!r}")
    if len(nodes) != len(headings):
        problems.append(f"count mismatch: {len(nodes)} summary entries vs {len(headings)} headings")
    return problems


def write_outline(pdf: Path, summary: Path, html: Path, output: Path, sync: bool) -> int:
    fitz = fitz_module()
    if fitz is None:
        raise RuntimeError("adding an outline needs pymupdf")
    root = parse_summary(summary.read_text(encoding="utf-8"))
    headings = collect_headings(html.read_text(encoding="utf-8", errors="replace")) if html.is_file() else []
    if sync and headings:
        changed = sync_titles(root, headings)
        if changed:
            print(f"synced {changed} title(s) from {html.name}", file=sys.stderr)
    nodes = list(root.walk())[1:]

    def writer(tmp: Path) -> None:
        with fitz.open(pdf) as document:
            toc = [[max(1, min(node.level, 6)), node.title, _page_for_source(document, node.src)]
                   for node in nodes]
            document.set_toc(toc)
            document.save(tmp, garbage=3, deflate=True)

    deliver(output, writer, suffix=".pdf")
    return len(nodes)


def _page_for_source(document: object, src: str) -> int:
    if not src:
        return 1
    needle = Path(src).stem
    for index in range(len(document)):
        if needle in document.load_page(index).get_text("text"):
            return index + 1
    return 1


# --------------------------------------------------------------------------- #
# viewer                                                                      #
# --------------------------------------------------------------------------- #


def _render_page(path: Path, page: int, engine: str) -> str:
    return extract_page(path, page - 1, engine)


def _print_page(path: Path, total: int, page: int, engine: str) -> None:
    body = _render_page(path, page, engine)
    rows = max(shutil.get_terminal_size((100, 40)).lines - 3, 10)
    clear = "" if os.environ.get("TERM", "") in ("", "dumb") else "\033[2J\033[H"
    print(f"{clear}{path.name}  page {page}/{total}  {human(path.stat().st_size)}")
    print("-" * 80)
    if body:
        lines = body.splitlines() or ["(no text on this page)"]
        for line in lines[:rows]:
            print(line)
        if len(lines) > rows:
            print(f"... {len(lines) - rows} more line(s) on this page")
    else:
        print("(no extractable text; try `pdftool.py image` for a raster)")
    print("-" * 80, flush=True)


def view(path: Path, page: int, engine: str) -> int:
    """Terminal pager for one page, with optional interactive navigation."""
    doc = open_doc(path)
    if page < 1 or page > doc.pages:
        print(f"page {page} out of range 1..{doc.pages}", file=sys.stderr)
        return 2
    resolved = pick_text_engine(engine)
    current = page
    if not sys.stdin.isatty():
        _print_page(path, doc.pages, current, resolved)
        return 0
    print("q quit · n next · p prev · g page", flush=True)
    while True:
        _print_page(path, doc.pages, current, resolved)
        try:
            key = input("page> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        low = key.lower()
        if low in ("q", "quit", "exit", ""):
            return 0
        if low in ("n", "j", "+"):
            current = min(current + 1, doc.pages)
        elif low in ("p", "k", "-"):
            current = max(current - 1, 1)
        elif low in ("home",):
            current = 1
        elif key in ("g", "end", "e", "G"):
            current = doc.pages
        elif key.isdigit():
            wanted = int(key)
            if 1 <= wanted <= doc.pages:
                current = wanted
            else:
                print(f"page {wanted} out of range 1..{doc.pages}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def print_doctor() -> int:
    rows = (
        ("pymupdf", fitz_module() is not None, "text, images, merge, outline", "pip install pymupdf"),
        ("pypdf", probe("pypdf") is not None, "text, merge, split", "pip install pypdf"),
        ("pdfminer", probe("pdfminer") is not None, "text", "pip install pdfminer.six"),
        ("pdfplumber", probe("pdfplumber") is not None, "text with layout", "pip install pdfplumber"),
        ("pdftotext", have("pdftotext"), "text via poppler", "apt install poppler-utils"),
        ("pdftoppm", have("pdftoppm"), "images via poppler", "apt install poppler-utils"),
        ("gs", have("gs"), "shrink", "apt install ghostscript"),
        ("qpdf", have("qpdf"), "shrink", "apt install qpdf"),
        ("mutool", have("mutool"), "shrink", "apt install mupdf-tools"),
        ("tesseract", have("tesseract"), "OCR", "apt install tesseract-ocr"),
    )
    print(f"{PROG} {__version__}  python {sys.version.split()[0]}  ({os.cpu_count()} cpu)\n")
    print(f"{'engine':<12}{'status':<9}{'enables':<28}hint")
    for name, ok, enables, hint in rows:
        print(f"{name:<12}{'ok' if ok else 'missing':<9}{enables:<28}{'' if ok else hint}")
    try:
        print(f"\ndefault text engine: {pick_text_engine()}")
    except RuntimeError as exc:
        print(f"\nno text engine: {exc}")
    return 0


def output_dir_for(command: str, given: Path | None, pdf: Path) -> Path:
    if given is not None:
        return given
    return pdf.parent


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-n", "--dry-run", action="store_true", help="report what would be written, write nothing")
    common.add_argument("--backup", action="store_true", help="keep a .bak of every overwritten output")
    common.add_argument("-q", "--quiet", action="store_true", help="only errors")
    common.add_argument("-v", "--verbose", action="store_true", help="per-file detail on stderr")
    common.add_argument("--json", action="store_true", help="machine-readable summary on stdout")
    common.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                        help="skip PDFs whose path matches (repeatable)")

    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Unified PDF toolkit: text, images, viewer, merge, split, outline, shrink.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "was: pdf2text pdf2page pdftotext pdftools pdftools2 pdftoolz pdf2img pdf2png\n"
            "     pdfv pdfv2 pdfview pdfviewer\n"
            "\n"
            "examples:\n"
            "  %(prog)s text report.pdf --per-page -o out/\n"
            "  %(prog)s merge a.pdf b.pdf -o merged.pdf\n"
            "  %(prog)s split big.pdf --max-mb 5 -o parts/\n"
            "  %(prog)s outline --html print.html --summary src/SUMMARY.md --sync-titles\n"
            "  %(prog)s text . -n --json          # plan only\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subs = parser.add_subparsers(dest="command", required=True)

    def add_inputs(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("inputs", nargs="*", type=Path, metavar="PDF",
                         help="PDF files and/or directories (default: .)")

    def add_walk(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--no-recursive", action="store_true")
        sub.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 1))

    text = subs.add_parser("text", parents=[common], help="extract text")
    add_inputs(text)
    add_walk(text)
    text.add_argument("-e", "--engine", choices=("auto", *TEXT_ENGINES), default="auto")
    text.add_argument("--per-page", action="store_true", help="one .txt per page")
    text.add_argument("--concat", action="store_true", help="one .txt per PDF (default when no --per-page)")
    text.add_argument("-o", "--output-dir", type=Path, default=None)
    text.add_argument("-p", "--pages", default=None, help="1,3-5,8-")
    text.add_argument("--password", default="")
    text.add_argument("--sort", action="store_true")
    text.add_argument("--pad-width", type=int, default=0)
    text.add_argument("--stdout", action="store_true")

    image = subs.add_parser("image", parents=[common], help="render pages to images")
    add_inputs(image)
    add_walk(image)
    image.add_argument("-o", "--output-dir", type=Path, default=Path("output_images"))
    image.add_argument("--dpi", type=int, default=300)
    image.add_argument("-z", "--zoom", type=float, default=0.0, help="scale factor, 1.0 == 72 dpi (overrides --dpi)")
    image.add_argument("--format", choices=("png", "jpg"), default="png")
    image.add_argument("--quality", type=int, default=95)
    image.add_argument("-e", "--engine", choices=("auto", *IMAGE_ENGINES), default="auto")
    image.add_argument("-p", "--pages", default=None)
    image.add_argument("--delete-source", action="store_true", help="remove the PDFs after converting")

    viewer = subs.add_parser("view", parents=[common], help="show pages in the terminal")
    add_inputs(viewer)
    viewer.add_argument("-p", "--page", type=int, default=1)
    viewer.add_argument("-e", "--engine", choices=("auto", *TEXT_ENGINES), default="auto")

    merge_cmd = subs.add_parser("merge", parents=[common], help="merge PDFs in the order given")
    add_inputs(merge_cmd)
    merge_cmd.add_argument("-o", "--output", type=Path, default=Path("merged.pdf"))

    split = subs.add_parser("split", parents=[common], help="split into pages, ranges or size-limited parts")
    add_inputs(split)
    split.add_argument("-o", "--output-dir", type=Path, default=Path("."))
    split.add_argument("-p", "--pages", default=None, help="only these pages: 1,3-5")
    split.add_argument("--max-mb", type=int, default=0, help="pack pages into parts no larger than N MiB")
    split.add_argument("--per-page", action="store_true", help="one file per page (default)")

    extract = subs.add_parser("extract", parents=[common], help="extract embedded images, fonts and attachments")
    add_inputs(extract)
    extract.add_argument("-o", "--output-dir", type=Path, default=Path("."))

    shrink_cmd = subs.add_parser("shrink", parents=[common], help="recompress with ghostscript / qpdf / mutool")
    add_inputs(shrink_cmd)
    shrink_cmd.add_argument("-e", "--engine", choices=SHRINK_ENGINES, required=True)
    shrink_cmd.add_argument("-l", "--level", type=int, default=2, choices=(1, 2, 3))
    shrink_cmd.add_argument("-o", "--output-dir", type=Path, default=Path("shrunk"))

    check = subs.add_parser("check", parents=[common], help="verify every page parses and yields text")
    add_inputs(check)
    add_walk(check)
    check.add_argument("-e", "--engine", choices=("auto", *TEXT_ENGINES), default="auto")

    outline = subs.add_parser("outline", parents=[common], help="build a PDF outline from a mdBook SUMMARY.md")
    outline.add_argument("--html", dest="html_path", type=Path, default=Path("print.html"))
    outline.add_argument("--pdf", dest="pdf_path", type=Path, default=Path("output.pdf"))
    outline.add_argument("--summary", dest="summary_path", type=Path, default=Path("src/SUMMARY.md"))
    outline.add_argument("-o", "--output", dest="output_path", type=Path, default=Path("output_with_outline.pdf"))
    outline.add_argument("--sync-titles", action="store_true",
                         help="rename outline entries to match the HTML headings")
    outline.add_argument("--check-titles", action="store_true", help="report mismatches and change nothing")
    outline.add_argument("--overwrite", action="store_true")

    info = subs.add_parser("info", parents=[common], help="page count, size, encryption")
    add_inputs(info)
    add_walk(info)

    doctor = subs.add_parser("doctor", help="report which libraries and CLI tools are present")
    doctor.add_argument("inputs", nargs="*", type=Path)
    return parser


# --------------------------------------------------------------------------- #
# commands                                                                    #
# --------------------------------------------------------------------------- #


def cmd_text(pdf: Path, args: argparse.Namespace) -> None:
    doc = open_doc(pdf)
    pages = parse_pages(args.pages, doc.pages) if args.pages else list(range(doc.pages))
    texts = extract_text(pdf, pages, args.engine, args.password, args.sort, args.jobs)
    if args.stdout:
        print("\n".join(texts))
        CTX.files.append({"path": str(pdf), "action": "stdout", "pages": len(pages)})
        return
    out_dir = output_dir_for("text", args.output_dir, pdf)
    if args.per_page:
        target_dir = out_dir / pdf.stem
        width = args.pad_width or max(3, len(str(doc.pages)))
        for index, body in zip(pages, texts):
            name = f"{pdf.stem}_page_{index + 1:0{width}d}.txt"
            deliver(target_dir / name, write_text(body), suffix=".txt")
        say(f"{pdf}: {len(pages)} page(s) -> {target_dir}")
    else:
        target = out_dir / f"{pdf.stem}.txt"
        deliver(target, write_text("\n".join(texts)), suffix=".txt")
        say(f"{pdf}: {len(pages)} page(s) -> {target}")


def cmd_image(pdf: Path, args: argparse.Namespace, many: bool) -> None:
    doc = open_doc(pdf)
    pages = parse_pages(args.pages, doc.pages) if args.pages else None
    engine = args.engine if args.engine != "auto" else ("fitz" if fitz_module() else "poppler")
    dpi = int(round(args.zoom * 72)) if args.zoom else args.dpi
    out_dir = args.output_dir / pdf.stem if many else args.output_dir
    written = convert_images(pdf, out_dir, dpi, args.format, args.quality, engine, pages, args.jobs)
    say(f"{pdf}: {len(written)} image(s) -> {out_dir}")
    if args.delete_source:
        if CTX.dry_run:
            say(f"[dry-run] would delete {pdf}")
        else:
            pdf.unlink()
            note(f"deleted source {pdf}")


def cmd_split(pdf: Path, args: argparse.Namespace) -> None:
    if args.max_mb:
        written = split_by_size(pdf, args.output_dir / pdf.stem, args.max_mb)
    else:
        pages = parse_pages(args.pages, open_doc(pdf).pages) if args.pages else None
        written = split_pages(pdf, args.output_dir, pages)
    say(f"{pdf}: {len(written)} file(s) -> {args.output_dir}")


def cmd_shrink(pdf: Path, args: argparse.Namespace) -> None:
    before = pdf.stat().st_size
    target = shrink(pdf, args.output_dir, args.engine, args.level)
    if CTX.dry_run:
        say(f"{pdf}: {human(before)} -> ? (dry-run)")
        return
    after = target.stat().st_size
    saved = before - after
    say(f"{pdf}: {human(before)} -> {human(after)} ({saved / max(before, 1):.0%} smaller)")


def cmd_extract(pdf: Path, args: argparse.Namespace) -> None:
    counts = extract_objects(pdf, args.output_dir / pdf.stem)
    say(f"{pdf}: {counts}")


def cmd_check(pdf: Path, args: argparse.Namespace) -> None:
    doc = open_doc(pdf)
    engine = pick_text_engine(args.engine)
    empty = [page + 1 for page in range(doc.pages) if not extract_page(pdf, page, engine).strip()]
    CTX.files.append({"path": str(pdf), "pages": doc.pages, "engine": engine, "empty_pages": len(empty)})
    if empty:
        say(f"{pdf}: {doc.pages} page(s), no text on {len(empty)}: {empty[:10]}")
    else:
        say(f"{pdf}: {doc.pages} page(s) ok ({engine})")


def cmd_info(pdf: Path, args: argparse.Namespace) -> None:
    doc = open_doc(pdf)
    size = pdf.stat().st_size
    CTX.files.append({"path": str(pdf), "pages": doc.pages, "bytes": size, "encrypted": doc.encrypted})
    say(f"{pdf}  {doc.pages} page(s)  {human(size)}" + ("  [encrypted]" if doc.encrypted else ""))


def run_outline(args: argparse.Namespace) -> int:
    if args.check_titles:
        root = parse_summary(args.summary_path.read_text(encoding="utf-8"))
        headings = collect_headings(args.html_path.read_text(encoding="utf-8", errors="replace"))
        problems = check_titles(root, headings)
        for problem in problems:
            say(problem)
        return 1 if problems else 0
    if args.output_path.exists() and not args.overwrite and not CTX.dry_run:
        print(f"{args.output_path} exists; pass --overwrite", file=sys.stderr)
        return 2
    count = write_outline(args.pdf_path, args.summary_path, args.html_path, args.output_path, args.sync_titles)
    say(f"wrote {count} outline entries to {args.output_path}")
    return 0


HANDLERS = {
    "text": cmd_text,
    "image": cmd_image,
    "split": cmd_split,
    "shrink": cmd_shrink,
    "extract": cmd_extract,
    "check": cmd_check,
    "info": cmd_info,
}


def emit_json(command: str) -> None:
    wrote = sum(1 for item in CTX.files if item.get("action") == "wrote")
    planned = sum(1 for item in CTX.files if item.get("action") == "would-write")
    payload = {
        "tool": PROG,
        "version": __version__,
        "command": command,
        "elapsed": round(time.perf_counter() - CTX.started, 3),
        "counts": {"files": len(CTX.files), "written": wrote, "planned": planned, "failed": CTX.failures},
        "files": CTX.files,
    }
    print(json.dumps(payload, indent=2, default=str))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    CTX.command = args.command
    CTX.dry_run = getattr(args, "dry_run", False)
    CTX.backup = getattr(args, "backup", False)
    CTX.quiet = getattr(args, "quiet", False)
    CTX.verbose = getattr(args, "verbose", False)
    CTX.json = getattr(args, "json", False)

    if args.command == "doctor":
        return print_doctor()

    try:
        code = _run(args)
    except KeyboardInterrupt:
        return 130
    if CTX.json and not (args.command == "text" and getattr(args, "stdout", False)):
        emit_json(args.command)
    return code


def _run(args: argparse.Namespace) -> int:
    if args.command == "outline":
        return run_outline(args)

    pdfs = collect_pdfs(args.inputs, recursive=not getattr(args, "no_recursive", False),
                        exclude=getattr(args, "exclude", ()))
    if not pdfs:
        print("no PDF files found", file=sys.stderr)
        return 1

    if args.command == "view":
        return view(pdfs[0], args.page, args.engine)
    if args.command == "merge":
        if len(pdfs) < 1:
            print("nothing to merge", file=sys.stderr)
            return 1
        pages = merge(pdfs, args.output)
        say(f"merged {len(pdfs)} file(s), {pages} page(s) -> {args.output}")
        return 0

    handler = HANDLERS[args.command]
    many = len(pdfs) > 1
    for pdf in pdfs:
        try:
            if args.command == "image":
                cmd_image(pdf, args, many)
            else:
                handler(pdf, args)
        except PageSpecError as exc:
            print(f"--pages: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001 - one bad PDF must not stop the batch
            CTX.failures += 1
            print(f"{pdf}: {type(exc).__name__}: {exc}", file=sys.stderr)

    if many and not CTX.json:
        print(f"\n{len(pdfs) - CTX.failures}/{len(pdfs)} ok · {time.perf_counter() - CTX.started:.2f}s",
              file=sys.stderr)
    return 1 if CTX.failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
