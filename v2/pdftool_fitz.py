#!/usr/bin/env python3
"""pdftool_fitz.py — the PDF toolkit with PyMuPDF and nothing else.

`pdftool.py` keeps a registry of seven text engines, two image engines and three
shrink backends because g002 shipped that many.  This file is the other reading
of the same folder: one library, no probing, no fallbacks.  If PyMuPDF is not
installed nothing works and that is fine — `doctor` says so and exits 2.

    pip install pymupdf

Same subcommands as pdftool.py, minus the ones that need an external binary:

    text      page text          (--per-page | --concat | --stdout, -p 1,3-5, --sort)
    image     page -> png/jpg    (--dpi / --zoom, --format, --quality, -j)
    view      terminal pager     (q quit · n next · p prev · g last · number jump)
    merge     a.pdf b.pdf -> one
    split     --per-page | --pages | --max-mb
    extract   embedded images, fonts, attachments
    shrink    re-save with garbage collection, deflate, image downsampling, font subsetting
    check     every page parses and yields text
    outline   mdBook SUMMARY.md -> PDF bookmarks (--sync-titles, --check-titles)
    info      pages, size, encryption, metadata
    doctor    PyMuPDF version and nothing else

Guard rails are the same as pdftool.py: a PDF over 100 MiB (or more pages than
--max-pages) is skipped with a message instead of being parsed.

Exit codes: 0 ok · 1 errors · 2 usage or PyMuPDF missing · 130 interrupted
"""


import argparse
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

try:
    import pymupdf
except ImportError:  # older wheels are imported as `fitz`; nothing else is accepted
    try:
        import fitz as pymupdf  # type: ignore[no-redef]
    except ImportError:
        pymupdf = None  # type: ignore[assignment]

__version__ = "1.0.0"
PROG = "pdftool_fitz.py"
CHUNK = 1 << 20

DEFAULT_MAX_SIZE = 100 * (1 << 20)
SIZE_UNITS = {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}
SIZE_RE = re.compile(r"^(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[kmgt]?)(?:i?b)?$", re.IGNORECASE)
UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


# --------------------------------------------------------------------------- #
# small helpers                                                                #
# --------------------------------------------------------------------------- #


class Skipped(Exception):
    """Valid file deliberately not processed (size or page limit)."""


class PageSpecError(ValueError):
    """Bad --pages value: a usage error, not a per-file failure."""


def human(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(size) < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def parse_size(text: str) -> int:
    raw = str(text).strip()
    if raw.lower() in ("0", "none", "off", "unlimited"):
        return 0
    match = SIZE_RE.match(raw)
    if match is None:
        raise argparse.ArgumentTypeError(f"bad size {text!r}: use 100M, 1.5G, 4096K or bytes")
    return int(float(match.group("value")) * SIZE_UNITS[match.group("unit").lower()])


def parse_pages(spec: str, total: int) -> list[int]:
    """``"1,3-5,8-"`` -> zero-based page indices (1-based input, inclusive)."""
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


def safe_name(value: str, fallback: str = "file") -> str:
    cleaned = UNSAFE.sub("_", value).strip("._")
    return cleaned or fallback


def require_mupdf() -> None:
    if pymupdf is None:
        print("PyMuPDF is not installed: pip install pymupdf", file=sys.stderr)
        raise SystemExit(2)


# --------------------------------------------------------------------------- #
# run context                                                                  #
# --------------------------------------------------------------------------- #


@dataclass
class Ctx:
    command: str = ""
    dry_run: bool = False
    backup: bool = False
    quiet: bool = False
    verbose: bool = False
    json: bool = False
    started: float = field(default_factory=time.perf_counter)
    files: list[dict] = field(default_factory=list)
    failures: int = 0
    skipped: int = 0
    max_size: int = DEFAULT_MAX_SIZE
    max_pages: int = 0


CTX = Ctx()


def say(message: str) -> None:
    if not CTX.quiet and not CTX.json:
        print(message)


def note(message: str) -> None:
    if CTX.verbose:
        print(message, file=sys.stderr)


def deliver(target: Path, writer, *, suffix: str | None = None) -> Path | None:
    """Write atomically; honour --dry-run and --backup.  ``writer`` gets a temp
    path that keeps the real suffix because MuPDF picks the output format from it."""
    if CTX.dry_run:
        say(f"[dry-run] would write {target}")
        CTX.files.append({"path": str(target), "action": "would-write"})
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.stem}.{os.getpid()}.tmp{suffix if suffix is not None else target.suffix}")
    try:
        writer(tmp)
        if target.exists() and CTX.backup:
            shutil.copy2(target, target.with_suffix(target.suffix + ".bak"))
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    size = target.stat().st_size
    CTX.files.append({"path": str(target), "bytes": size, "action": "wrote"})
    note(f"wrote {target} ({human(size)})")
    return target


def write_text(text: str):
    def writer(tmp: Path) -> None:
        tmp.write_text(text, encoding="utf-8")

    return writer


def write_bytes(data: bytes):
    def writer(tmp: Path) -> None:
        tmp.write_bytes(data)

    return writer


# --------------------------------------------------------------------------- #
# documents                                                                    #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Doc:
    path: Path
    pages: int
    encrypted: bool


def open_doc(path: Path, password: str = "") -> Doc:
    with pymupdf.open(path) as document:  # noqa: F821 - checked by require_mupdf
        if document.needs_pass:
            if not password or not document.authenticate(password):
                raise RuntimeError("encrypted PDF: pass --password")
        return Doc(path, document.page_count, bool(document.needs_pass))


def collect_pdfs(paths: Sequence[Path], recursive: bool = True, exclude: Sequence[str] = (),
                 max_size: int = 0, skipped: list[dict] | None = None) -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for raw in paths or [Path.cwd()]:
        path = raw.expanduser()
        if not path.exists():
            print(f"{path}: no such file or directory", file=sys.stderr)
            continue
        if path.is_file():
            candidates: Iterator[Path] = iter([path])
        elif recursive:
            candidates = (item for item in sorted(path.rglob("*.pdf")) if item.is_file())
        else:
            candidates = (item for item in sorted(path.glob("*.pdf")) if item.is_file())
        for candidate in candidates:
            if candidate.suffix.lower() != ".pdf" or candidate.is_symlink():
                continue
            if any(_glob(candidate, pattern) for pattern in exclude):
                continue
            key = candidate.resolve()
            if key in seen:
                continue
            seen.add(key)
            size = candidate.stat().st_size
            if max_size and size > max_size:
                if skipped is not None:
                    skipped.append({"path": str(candidate), "bytes": size,
                                    "reason": f"{human(size)} > {human(max_size)} limit"})
                continue
            out.append(candidate)
    return out


def _glob(path: Path, pattern: str) -> bool:
    import fnmatch

    return fnmatch.fnmatch(str(path), pattern) or fnmatch.fnmatch(path.name, pattern)


def limit_reason(pdf: Path, pages: int | None = None) -> str | None:
    if CTX.max_size:
        size = pdf.stat().st_size
        if size > CTX.max_size:
            return f"{human(size)} > {human(CTX.max_size)} size limit"
    if CTX.max_pages and pages is not None and pages > CTX.max_pages:
        return f"{pages} pages > {CTX.max_pages} page limit"
    return None


def guard(pdf: Path, pages: int | None = None) -> None:
    reason = limit_reason(pdf, pages)
    if reason is not None:
        raise Skipped(reason)


def remember_skip(pdf: Path, reason: str, pages: int | None = None) -> None:
    CTX.skipped += 1
    record = {"path": str(pdf), "action": "skipped", "reason": reason}
    if pages is not None:
        record["pages"] = pages
    if pdf.exists():
        record["bytes"] = pdf.stat().st_size
    CTX.files.append(record)
    print(f"{pdf}: skipped ({reason})", file=sys.stderr)


# --------------------------------------------------------------------------- #
# reading                                                                      #
# --------------------------------------------------------------------------- #


def page_text(pdf: Path, index: int, password: str = "", sort: bool = False) -> str:
    with pymupdf.open(pdf) as document:  # noqa: F821
        if document.needs_pass and not document.authenticate(password):
            raise RuntimeError("encrypted PDF: pass --password")
        return document.load_page(index).get_text("text", sort=bool(sort))


def extract_text(pdf: Path, pages: Sequence[int], password: str = "", sort: bool = False,
                 jobs: int = 1) -> list[str]:
    """One MuPDF handle per page when threaded: PyMuPDF handles are not thread-safe."""
    if jobs > 1 and len(pages) > 1:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            return list(pool.map(lambda index: page_text(pdf, index, password, sort), pages))
    with pymupdf.open(pdf) as document:  # noqa: F821
        if document.needs_pass and not document.authenticate(password):
            raise RuntimeError("encrypted PDF: pass --password")
        return [document.load_page(index).get_text("text", sort=bool(sort)) for index in pages]


# --------------------------------------------------------------------------- #
# commands: text, image, view                                                  #
# --------------------------------------------------------------------------- #


def cmd_text(pdf: Path, args: argparse.Namespace) -> None:
    doc = open_doc(pdf, args.password)
    guard(pdf, doc.pages)
    pages = parse_pages(args.pages, doc.pages) if args.pages else list(range(doc.pages))
    texts = extract_text(pdf, pages, args.password, args.sort, args.jobs)
    if args.stdout:
        print("\n".join(texts))
        CTX.files.append({"path": str(pdf), "action": "stdout", "pages": len(pages)})
        return
    out_dir = args.output_dir or pdf.parent
    if args.per_page:
        target_dir = out_dir / pdf.stem
        width = args.pad_width or max(3, len(str(doc.pages)))
        for index, body in zip(pages, texts):
            deliver(target_dir / f"{pdf.stem}_page_{index + 1:0{width}d}.txt", write_text(body), suffix=".txt")
        say(f"{pdf}: {len(pages)} page(s) -> {target_dir}")
    else:
        target = out_dir / f"{pdf.stem}.txt"
        deliver(target, write_text("\n".join(texts)), suffix=".txt")
        say(f"{pdf}: {len(pages)} page(s) -> {target}")


def render(pdf: Path, index: int, target: Path, dpi: int, fmt: str, quality: int) -> Path:
    def writer(tmp: Path) -> None:
        with pymupdf.open(pdf) as document:  # noqa: F821
            pixmap = document.load_page(index).get_pixmap(dpi=dpi)
            if fmt == "jpg":
                pixmap.save(str(tmp), jpg_quality=quality)
            else:
                pixmap.save(str(tmp))

    deliver(target, writer, suffix=f".{fmt}")
    return target


def cmd_image(pdf: Path, args: argparse.Namespace, many: bool) -> None:
    doc = open_doc(pdf)
    guard(pdf, doc.pages)
    pages = parse_pages(args.pages, doc.pages) if args.pages else list(range(doc.pages))
    dpi = int(round(args.zoom * 72)) if args.zoom else args.dpi
    out_dir = args.output_dir / pdf.stem if many else args.output_dir
    width = max(3, len(str(doc.pages)))
    targets = [out_dir / f"{pdf.stem}_page_{index + 1:0{width}d}.{args.format}" for index in pages]

    def one(job: tuple[int, Path]) -> Path:
        return render(pdf, job[0], job[1], dpi, args.format, args.quality)

    if args.jobs > 1 and len(pages) > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            list(pool.map(one, zip(pages, targets)))
    else:
        for job in zip(pages, targets):
            one(job)
    say(f"{pdf}: {len(pages)} image(s) -> {out_dir} @ {dpi} dpi")
    if args.delete_source:
        if CTX.dry_run:
            say(f"[dry-run] would delete {pdf}")
        else:
            pdf.unlink()
            note(f"deleted source {pdf}")


def _print_page(pdf: Path, total: int, page: int) -> None:
    body = page_text(pdf, page - 1)
    rows = max(shutil.get_terminal_size((100, 40)).lines - 3, 10)
    clear = "" if os.environ.get("TERM", "") in ("", "dumb") else "\033[2J\033[H"
    print(f"{clear}{pdf.name}  page {page}/{total}  {human(pdf.stat().st_size)}")
    print("-" * 80)
    lines = body.splitlines() or ["(no extractable text on this page)"]
    for line in lines[:rows]:
        print(line)
    if len(lines) > rows:
        print(f"... {len(lines) - rows} more line(s) on this page")
    print("-" * 80, flush=True)


def cmd_view(pdf: Path, args: argparse.Namespace) -> int:
    doc = open_doc(pdf)
    guard(pdf, doc.pages)
    if args.page < 1 or args.page > doc.pages:
        print(f"page {args.page} out of range 1..{doc.pages}", file=sys.stderr)
        return 2
    current = args.page
    if not sys.stdin.isatty():
        _print_page(pdf, doc.pages, current)
        return 0
    print("q quit · n next · p prev · g last · number = jump", flush=True)
    while True:
        _print_page(pdf, doc.pages, current)
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
        elif low in ("g", "end", "e"):
            current = doc.pages
        elif low == "home":
            current = 1
        elif key.isdigit():
            wanted = int(key)
            if 1 <= wanted <= doc.pages:
                current = wanted
            else:
                print(f"page {wanted} out of range 1..{doc.pages}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# commands: merge, split, extract, shrink                                      #
# --------------------------------------------------------------------------- #


def merge(pdfs: Sequence[Path], target: Path) -> int:
    pages = 0

    def writer(tmp: Path) -> None:
        nonlocal pages
        with pymupdf.open() as merged:  # noqa: F821
            for pdf in pdfs:
                with pymupdf.open(pdf) as source:  # noqa: F821
                    merged.insert_pdf(source)
            pages = merged.page_count
            merged.save(tmp, garbage=3, deflate=True)

    deliver(target, writer, suffix=".pdf")
    return pages


def write_slice(pdf: Path, pages: Sequence[int], target: Path) -> None:
    def writer(tmp: Path) -> None:
        with pymupdf.open(pdf) as source, pymupdf.open() as out:  # noqa: F821
            for index in pages:
                out.insert_pdf(source, from_page=index, to_page=index)
            out.save(tmp, garbage=3, deflate=True)

    deliver(target, writer, suffix=".pdf")


def cmd_split(pdf: Path, args: argparse.Namespace) -> None:
    doc = open_doc(pdf)
    guard(pdf, doc.pages)
    if args.max_mb:
        written = split_by_size(pdf, args.output_dir / pdf.stem, args.max_mb, doc.pages)
    else:
        pages = parse_pages(args.pages, doc.pages) if args.pages else list(range(doc.pages))
        digits = max(1, len(str(doc.pages)))
        directory = args.output_dir / pdf.stem if len(pages) > 1 else args.output_dir
        written = [write_slice(pdf, [index], directory / f"{pdf.stem}_page_{index + 1:0{digits}d}.pdf")
                   for index in pages]
    say(f"{pdf}: {len(written)} file(s) -> {args.output_dir}")


def split_by_size(pdf: Path, out_dir: Path, max_mb: int, total: int) -> list[Path]:
    """Greedy page packing that stays under ``max_mb``."""
    budget = max_mb * 1024 * 1024
    chunks: list[list[int]] = []
    current: list[int] = []
    for index in range(total):
        current.append(index)
        if len(current) > 1 and _slice_size(pdf, current) > budget:
            current.pop()
            chunks.append(current)
            current = [index]
    if current:
        chunks.append(current)
    written: list[Path] = []
    for number, chunk in enumerate(chunks, 1):
        target = out_dir / f"{pdf.stem}_part{number:02d}.pdf"
        write_slice(pdf, chunk, target)
        written.append(target)
    return written


def _slice_size(pdf: Path, pages: Sequence[int]) -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "slice.pdf"
        with pymupdf.open(pdf) as source, pymupdf.open() as out:  # noqa: F821
            for index in pages:
                out.insert_pdf(source, from_page=index, to_page=index)
            out.save(target, garbage=3, deflate=True)
        return target.stat().st_size


def cmd_extract(pdf: Path, args: argparse.Namespace) -> None:
    doc = open_doc(pdf)
    guard(pdf, doc.pages)
    out_dir = args.output_dir / pdf.stem
    counts = {"images": 0, "fonts": 0, "attachments": 0}
    with pymupdf.open(pdf) as document:  # noqa: F821
        for xref in range(1, document.xref_length()):
            try:
                kind = document.xref_get_key(xref, "Subtype")[1]
            except Exception:  # noqa: BLE001 - a broken xref is not fatal
                continue
            if kind == "/Image":
                data = document.extract_image(xref)
                if not data or not data.get("image"):
                    continue
                ext = data.get("ext", "bin")
                deliver(out_dir / "images" / safe_name(f"{pdf.stem}_x{xref}.{ext}"),
                        write_bytes(data["image"]), suffix=f".{ext}")
                counts["images"] += 1
            elif kind in ("/Type1", "/TrueType", "/Type0"):
                base, ext, _type, content = document.extract_font(xref)
                if not content:
                    continue
                deliver(out_dir / "fonts" / f"{safe_name(base or f'font_x{xref}')}.{safe_name(ext or 'bin')}",
                        write_bytes(content))
                counts["fonts"] += 1
        for name in document.embfile_names() or []:
            deliver(out_dir / "attachments" / safe_name(name), write_bytes(document.embfile_get(name)))
            counts["attachments"] += 1
    say(f"{pdf}: {counts}")


def cmd_shrink(pdf: Path, args: argparse.Namespace) -> None:
    """No ghostscript here: MuPDF's own garbage collection, deflate, image
    downsampling and font subsetting do the same job."""
    doc = open_doc(pdf)
    guard(pdf, doc.pages)
    target = args.output_dir / pdf.name
    before = pdf.stat().st_size
    level = args.level

    def writer(tmp: Path) -> None:
        with pymupdf.open(pdf) as document:  # noqa: F821
            if level >= 2:
                dpi = {2: 150, 3: 96}.get(level, 150)
                with _quiet():
                    document.rewrite_images(dpi_threshold=dpi, dpi_target=dpi // 2 or 72, quality=75)
            if level >= 3:
                with _quiet():
                    document.subset_fonts()
            document.save(tmp, garbage=4 if level >= 2 else 3, deflate=True, clean=level >= 2)

    deliver(target, writer, suffix=".pdf")
    if CTX.dry_run:
        say(f"{pdf}: {human(before)} -> ? (dry-run)")
        return
    after = target.stat().st_size
    saved = before - after
    say(f"{pdf}: {human(before)} -> {human(after)} ({saved / max(before, 1):.0%} smaller)")


class _quiet:
    """Silence MuPDF's chatty rewrite_images / subset_fonts output."""

    def __enter__(self):
        self._saved = sys.stdout
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 - closed in __exit__
        return self

    def __exit__(self, *exc):
        sys.stdout.close()
        sys.stdout = self._saved
        return False


# --------------------------------------------------------------------------- #
# commands: outline, check, info, doctor                                       #
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
    hits = [re.sub(r"<[^>]+>", "", match.group("title")).strip() for match in HTML_HEADING_RE.finditer(html)]
    if hits:
        return [title for title in hits if title]
    plain = re.sub(r"<h1[^>]*>.*?</h1>", "", html, flags=re.IGNORECASE | re.DOTALL)
    return [match.group("title").strip() for match in MD_HEADING_RE.finditer(plain)]


def sync_titles(root: Node, headings: Sequence[str]) -> int:
    nodes = list(root.walk())[1:]
    if len(nodes) != len(headings):
        print(f"sync-titles skipped: {len(nodes)} summary entries vs {len(headings)} headings",
              file=sys.stderr)
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


def page_for_source(document, src: str) -> int:
    if not src:
        return 1
    needle = Path(src).stem
    for index in range(document.page_count):
        if needle in document.load_page(index).get_text("text"):
            return index + 1
    return 1


def run_outline(args: argparse.Namespace) -> int:
    if args.check_titles:
        root = parse_summary(args.summary_path.read_text(encoding="utf-8"))
        headings = collect_headings(args.html_path.read_text(encoding="utf-8", errors="replace"))
        problems = check_titles(root, headings)
        for problem in problems:
            say(problem)
        return 1 if problems else 0
    guard(args.pdf_path, open_doc(args.pdf_path).pages)
    if args.output_path.exists() and not args.overwrite and not CTX.dry_run:
        print(f"{args.output_path} exists; pass --overwrite", file=sys.stderr)
        return 2
    root = parse_summary(args.summary_path.read_text(encoding="utf-8"))
    headings = collect_headings(args.html_path.read_text(encoding="utf-8", errors="replace"))
    if args.sync_titles and headings:
        changed = sync_titles(root, headings)
        if changed:
            print(f"synced {changed} title(s) from {args.html_path.name}", file=sys.stderr)
    nodes = list(root.walk())[1:]

    def writer(tmp: Path) -> None:
        with pymupdf.open(args.pdf_path) as document:  # noqa: F821
            toc = [[max(1, min(node.level, 6)), node.title, page_for_source(document, node.src)]
                   for node in nodes]
            document.set_toc(toc)
            document.save(tmp, garbage=3, deflate=True)

    deliver(args.output_path, writer, suffix=".pdf")
    say(f"wrote {len(nodes)} outline entries to {args.output_path}")
    return 0


def cmd_check(pdf: Path, args: argparse.Namespace) -> None:
    doc = open_doc(pdf)
    guard(pdf, doc.pages)
    with pymupdf.open(pdf) as document:  # noqa: F821
        empty = [index + 1 for index in range(document.page_count)
                 if not document.load_page(index).get_text("text").strip()]
    CTX.files.append({"path": str(pdf), "pages": doc.pages, "empty_pages": len(empty)})
    if empty:
        say(f"{pdf}: {doc.pages} page(s), no text on {len(empty)}: {empty[:10]}")
    else:
        say(f"{pdf}: {doc.pages} page(s) ok")


def cmd_info(pdf: Path, args: argparse.Namespace) -> None:
    # exempt from --max-size / --max-pages: this is how you find out what is too big
    doc = open_doc(pdf)
    size = pdf.stat().st_size
    with pymupdf.open(pdf) as document:  # noqa: F821
        meta = {key: value for key, value in (document.metadata or {}).items() if value}
    row = {"path": str(pdf), "pages": doc.pages, "bytes": size,
           "encrypted": doc.encrypted, "metadata": meta}
    CTX.files.append(row)
    if CTX.json:
        return
    say(f"{pdf}  {doc.pages} page(s)  {human(size)}" + ("  [encrypted]" if doc.encrypted else ""))
    for key in ("title", "author", "producer", "creationDate"):
        if meta.get(key):
            say(f"    {key}: {meta[key]}")


def cmd_doctor(_args: argparse.Namespace) -> int:
    print(f"{PROG} {__version__}  python {sys.version.split()[0]}  ({os.cpu_count()} cpu)\n")
    if pymupdf is None:
        print("pymupdf   MISSING   pip install pymupdf")
        print("\nnothing works without PyMuPDF — this build has no fallback engine.")
        return 2
    print(f"pymupdf   ok        {pymupdf.version}")
    print(f"mupdf     {getattr(pymupdf, 'mupdf_version', '?')}")
    print("\nengines: pymupdf only (no pypdf, pdfminer, poppler, ghostscript, qpdf, mutool)")
    print(f"skip limit: files over {human(DEFAULT_MAX_SIZE)} are skipped "
          f"(override with --max-size / --no-max-size, add --max-pages N)")
    return 0


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-n", "--dry-run", action="store_true", help="report what would be written")
    common.add_argument("--backup", action="store_true", help="keep a .bak of overwritten outputs")
    common.add_argument("-q", "--quiet", action="store_true")
    common.add_argument("-v", "--verbose", action="store_true")
    common.add_argument("--json", action="store_true", help="machine-readable summary on stdout")
    common.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    common.add_argument("--max-size", type=parse_size, default=DEFAULT_MAX_SIZE, metavar="SIZE",
                        help="skip PDFs larger than SIZE (default 100M, 0 = no limit)")
    common.add_argument("--no-max-size", dest="max_size", action="store_const", const=0)
    common.add_argument("--max-pages", type=int, default=0, metavar="N",
                        help="skip PDFs with more than N pages")

    parser = argparse.ArgumentParser(
        prog=PROG,
        description="PDF toolkit built on PyMuPDF alone — no other backend, no fallback.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "was: pdf2text pdf2page pdftotext pdftools pdftools2 pdftoolz pdf2img pdf2png\n"
            "     pdfv pdfv2 pdfview pdfviewer  (pymupdf-only halves of each)\n"
            "\n"
            "examples:\n"
            "  %(prog)s text report.pdf --per-page -o out/\n"
            "  %(prog)s image scans/ --dpi 200 --format jpg -j 4\n"
            "  %(prog)s merge a.pdf b.pdf -o merged.pdf\n"
            "  %(prog)s split big.pdf --max-mb 5 -o parts/\n"
            "  %(prog)s shrink scan.pdf -l 3 -o small/\n"
            "  %(prog)s outline --html print.html --summary src/SUMMARY.md --sync-titles\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subs = parser.add_subparsers(dest="command", required=True)

    def add_inputs(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("inputs", nargs="*", type=Path, metavar="PDF")

    def add_walk(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--no-recursive", action="store_true")
        sub.add_argument("-j", "--jobs", type=int, default=min(8, os.cpu_count() or 1))

    text = subs.add_parser("text", parents=[common], help="extract text")
    add_inputs(text)
    add_walk(text)
    text.add_argument("--per-page", action="store_true", help="one .txt per page")
    text.add_argument("--concat", action="store_true", help="one .txt per PDF (default)")
    text.add_argument("-o", "--output-dir", type=Path, default=None)
    text.add_argument("-p", "--pages", default=None, help="1,3-5,8-")
    text.add_argument("--password", default="")
    text.add_argument("--sort", action="store_true", help="reading order instead of stream order")
    text.add_argument("--pad-width", type=int, default=0)
    text.add_argument("--stdout", action="store_true")

    image = subs.add_parser("image", parents=[common], help="render pages to images")
    add_inputs(image)
    add_walk(image)
    image.add_argument("-o", "--output-dir", type=Path, default=Path("output_images"))
    image.add_argument("--dpi", type=int, default=300)
    image.add_argument("-z", "--zoom", type=float, default=0.0, help="scale factor, 1.0 == 72 dpi")
    image.add_argument("--format", choices=("png", "jpg"), default="png")
    image.add_argument("--quality", type=int, default=95)
    image.add_argument("-p", "--pages", default=None)
    image.add_argument("--delete-source", action="store_true")

    viewer = subs.add_parser("view", parents=[common], help="show pages in the terminal")
    add_inputs(viewer)
    viewer.add_argument("-p", "--page", type=int, default=1)

    merge_cmd = subs.add_parser("merge", parents=[common], help="merge PDFs in the order given")
    add_inputs(merge_cmd)
    merge_cmd.add_argument("-o", "--output", type=Path, default=Path("merged.pdf"))

    split = subs.add_parser("split", parents=[common], help="split into pages or size-limited parts")
    add_inputs(split)
    split.add_argument("-o", "--output-dir", type=Path, default=Path("."))
    split.add_argument("-p", "--pages", default=None, help="only these pages: 1,3-5")
    split.add_argument("--max-mb", type=int, default=0)
    split.add_argument("--per-page", action="store_true", help="one file per page (default)")

    extract = subs.add_parser("extract", parents=[common], help="embedded images, fonts, attachments")
    add_inputs(extract)
    extract.add_argument("-o", "--output-dir", type=Path, default=Path("."))

    shrink = subs.add_parser("shrink", parents=[common], help="re-save with MuPDF's own compression")
    add_inputs(shrink)
    shrink.add_argument("-l", "--level", type=int, default=2, choices=(1, 2, 3),
                        help="1 garbage+deflate · 2 +image downsampling · 3 +font subsetting")
    shrink.add_argument("-o", "--output-dir", type=Path, default=Path("shrunk"))

    check = subs.add_parser("check", parents=[common], help="every page parses and yields text")
    add_inputs(check)
    add_walk(check)

    outline = subs.add_parser("outline", parents=[common], help="mdBook SUMMARY.md -> bookmarks")
    outline.add_argument("--html", dest="html_path", type=Path, default=Path("print.html"))
    outline.add_argument("--pdf", dest="pdf_path", type=Path, default=Path("output.pdf"))
    outline.add_argument("--summary", dest="summary_path", type=Path, default=Path("src/SUMMARY.md"))
    outline.add_argument("-o", "--output", dest="output_path", type=Path,
                         default=Path("output_with_outline.pdf"))
    outline.add_argument("--sync-titles", action="store_true")
    outline.add_argument("--check-titles", action="store_true")
    outline.add_argument("--overwrite", action="store_true")

    info = subs.add_parser("info", parents=[common], help="pages, size, encryption, metadata")
    add_inputs(info)
    add_walk(info)

    subs.add_parser("doctor", help="PyMuPDF version, and nothing else")
    return parser


def emit_json(command: str) -> None:
    wrote = sum(1 for item in CTX.files if item.get("action") == "wrote")
    planned = sum(1 for item in CTX.files if item.get("action") == "would-write")
    payload = {
        "tool": PROG,
        "version": __version__,
        "command": command,
        "elapsed": round(time.perf_counter() - CTX.started, 3),
        "counts": {"files": len(CTX.files), "written": wrote, "planned": planned,
                   "failed": CTX.failures, "skipped": CTX.skipped},
        "files": CTX.files,
    }
    print(json.dumps(payload, indent=2, default=str))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    CTX.command = args.command
    for flag in ("dry_run", "backup", "quiet", "verbose", "json"):
        setattr(CTX, flag, getattr(args, flag, False))
    CTX.max_size = getattr(args, "max_size", DEFAULT_MAX_SIZE) or 0
    CTX.max_pages = max(0, getattr(args, "max_pages", 0) or 0)

    if args.command == "doctor":
        return cmd_doctor(args)
    require_mupdf()

    try:
        code = _run(args)
    except KeyboardInterrupt:
        return 130
    if CTX.json and not (args.command == "text" and getattr(args, "stdout", False)):
        emit_json(args.command)
    return code


def _run(args: argparse.Namespace) -> int:
    try:
        if args.command == "outline":
            return run_outline(args)
    except Skipped as exc:
        remember_skip(args.pdf_path, str(exc))
        return 0
    except PageSpecError as exc:
        print(f"--pages: {exc}", file=sys.stderr)
        return 2

    oversized: list[dict] = []
    budget = 0 if args.command == "info" else CTX.max_size
    pdfs = collect_pdfs(args.inputs, recursive=not getattr(args, "no_recursive", False),
                        exclude=getattr(args, "exclude", ()), max_size=budget, skipped=oversized)
    for item in oversized:
        remember_skip(Path(item["path"]), item["reason"])
    pre_skipped = CTX.skipped
    if not pdfs:
        if pre_skipped:
            print(f"nothing to do: {pre_skipped} file(s) skipped by --max-size", file=sys.stderr)
            return 0
        print("no PDF files found", file=sys.stderr)
        return 1

    if args.command == "view":
        try:
            return cmd_view(pdfs[0], args)
        except Skipped as exc:
            remember_skip(pdfs[0], str(exc))
            return 0

    if args.command == "merge":
        if CTX.max_pages:
            keep = []
            for pdf in pdfs:
                pages = open_doc(pdf).pages
                reason = limit_reason(pdf, pages)
                if reason is None:
                    keep.append(pdf)
                else:
                    remember_skip(pdf, reason, pages)
            pdfs = keep
        if not pdfs:
            print("nothing to merge after the limits", file=sys.stderr)
            return 0
        pages = merge(pdfs, args.output)
        say(f"merged {len(pdfs)} file(s), {pages} page(s) -> {args.output}")
        return 0

    handler = HANDLERS[args.command]
    many = len(pdfs) > 1
    for pdf in pdfs:
        try:
            handler(pdf, args, many) if args.command == "image" else handler(pdf, args)
        except Skipped as exc:
            remember_skip(pdf, str(exc))
        except PageSpecError as exc:
            print(f"--pages: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:  # noqa: BLE001 - one bad PDF must not stop the batch
            CTX.failures += 1
            print(f"{pdf}: {type(exc).__name__}: {exc}", file=sys.stderr)

    if many and not CTX.json:
        dropped = CTX.skipped - pre_skipped
        total = len(pdfs) + pre_skipped
        done = len(pdfs) - CTX.failures - dropped
        skipped = f" · {CTX.skipped} skipped" if CTX.skipped else ""
        print(f"\n{done}/{total} ok{skipped} · {time.perf_counter() - CTX.started:.2f}s", file=sys.stderr)
    return 1 if CTX.failures else 0


HANDLERS = {
    "text": cmd_text,
    "image": cmd_image,
    "split": cmd_split,
    "extract": cmd_extract,
    "shrink": cmd_shrink,
    "check": cmd_check,
    "info": cmd_info,
}


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
