# arena — 96 Python scripts consolidated into 11 files

Eleven standalone command-line tools, one file each, all at the repository root.
No package, no `__init__.py`, no shared library: `python pdftool.py --help` works
on a fresh checkout with nothing installed.

Every tool is **stdlib-first**. Where a third-party library helps (`pymupdf`,
`libcst`, `tree_sitter`, `rich`, `zstandard`, …) it is optional, and each tool
tells you what it found with its own `doctor` / `--doctor` command instead of
failing at import time. Python **3.10+**.

| file | what it does | subcommands |
|---|---|---|
| `comments.py` | strip comments from source, 22 languages | (one operation) `-b tree-sitter\|regex\|libcst\|minify` |
| `pdftool.py` | read / render / restructure PDFs, 7 optional engines | `text image view merge split extract shrink check outline info doctor` |
| `pdftool_fitz.py` | same jobs, **PyMuPDF only** — no fallback engines | same eleven |
| `pydup.py` | find duplicate & near-duplicate Python files | (one scanner) `--doctor` |
| `extract.py` | pull classes, functions, docstrings, imports, constants out of a tree | (one operation) `--kinds` |
| `codecs.py` | compress single streams (gzip, zstd, lzma, brotli, bz2, lz4, …) | `compress decompress bench inspect doctor` |
| `archive.py` | pack / unpack containers (zip, tar, 7z, …) | `pack unpack list info check split remove merge convert fix bench doctor` |
| `annotations.py` | add **or** remove type annotations | `add remove check doctor` |
| `rmc.py` | strip comments/docstrings/annotations from Python, 3 engines | `strip check bench doctor` |
| `deadcode.py` | find unused imports, unused names, unreachable blocks | `imports unused block doctor` |
| `rmc_ts.py` | the simple stripper: your `dh.TSStripper` + `ProcessPoolExecutor` | (one operation) `-c -d -a --check --json` |

`sim/01.sh` is a six-line scratch file (`pwd; ls; git status`), left alone.

## Where the originals went

The 96 scripts this replaced lived in `g001/` … `g006/` and `rmc/`. They were
archived under `legacy/` during the merge, compared against their replacements,
and then deleted — at your request — so the repo is flat. They are still in git:

```bash
git show 16208c1:legacy/g002/pdf2text.py       # any original, by path
git ls-tree -r --name-only 16208c1 legacy/     # all 96
git show e40ce53:g002/pdf2text.py              # ...or by its pre-archive path
```

`PLAN.md` is the record of that merge: what each directory held, which ideas were
dropped and why, how every tool was proven, and the two single-dependency
variants. It describes directories that no longer exist; read `g002/pdf2text.py`
in it as "a file that used to be in `g002/`".

## Quick start

```bash
# nothing installed — see what each tool can use
python3 pdftool.py doctor
python3 pdftool_fitz.py doctor      # one engine: pymupdf, or exit 2

# the common jobs
python3 pdftool.py text report.pdf --per-page -o out/
python3 pdftool.py merge a.pdf b.pdf -o merged.pdf
python3 pdftool.py text scans/ -n --json                       # plan only
python3 archive.py pack src/ -o src.zip
python3 codecs.py bench /usr/share/dict/words
python3 comments.py src/ -l auto -b tree-sitter
python3 rmc.py strip src/ -c -d -n                            # preview
python3 deadcode.py imports src/ --check                      # CI gate
python3 annotations.py remove src/ -b tree-sitter             # byte-identical to libcst
```

Every tool shares the same conventions: `-n/--dry-run` writes nothing,
`--json` emits one machine-readable report, `--check` exits 1 iff work is pending,
`-q` quiet, `-v` verbose, `-j N` for the tools that parallelise.

## Install the optional extras

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install pymupdf pypdf libcst tree-sitter tree-sitter-python pyflakes
# or, for exactly what the tests used:
arena/setup_venv.sh
```

`rmc_ts.py` additionally needs your private `dh` module on `PYTHONPATH`:

```bash
PYTHONPATH=$HOME/lib python3 rmc_ts.py src/ -c -d -n
```

## Tests

The scripts used to build and verify all of this are in `arena/` — see
`arena/README.md`. The short version:

```bash
arena/verify_tools.sh                 # pyflakes + --help + doctor, all 11 files
arena/smoke_pdftool.sh                # 37 checks
arena/smoke_pdftool.sh --fitz         # 41 checks
arena/smoke_rmc.sh                    # 30 checks (uses arena/dh_stub.py if dh is absent)
python3 arena/parity_g006.py          # tree-sitter output == libcst output
```
