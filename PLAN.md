# Consolidation Plan — 96 scripts → 9 files

**Status:** approved · implemented · validated · originals archived under `legacy/`
**Python floor:** 3.10 · **third-party deps:** all optional, every tool has a `doctor`

---

## 0. TL;DR

| dir | theme (as actually implemented) | scripts | → | consolidated file | lines |
|---|---|---|---|---|---|
| `g001` | strip comments from source code, 22 languages | 19 | → | `g001/comments.py` | **1,414** ✅ |
| `g002` | read / render / restructure PDFs | 12 | → | `g002/pdftool.py` | **1,104** ✅ |
| `g003` | find duplicate & near-duplicate Python files | 2 | → | `g003/pydup.py` | **3,024** ✅ |
| `g004` | extract artifacts (classes, funcs, docstrings, imports, constants) | 6 | → | `g004/extract.py` | **563** ✅ |
| `g005` | compress single streams / archive containers / benchmark | 24 | → | `g005/codecs.py` + `g005/archive.py` | **1,238 + 1,426** ✅ |
| `g006` | add **or** remove type annotations | 4 | → | `g006/annotations.py` | **1,036** ✅ |
| `rmc` | strip comments/docstrings/annotations **and** dead code from Python | 29 | → | `rmc/rmc.py` + `rmc/deadcode.py` | **1,293 + 614** ✅ |
| `sim` | only `01.sh` (a 6-line `pwd; ls; git status` scratch file) | 0 py | — | skipped, per your instruction | — |

**45,266 → 11,712 lines (−74%), 96 files → 9.**

Two folders are two tools each, on your call (§7): `g005` splits *streams* from
*containers* (`codecs.py` / `archive.py`), and `rmc` splits *comment stripping*
from *dead-code removal* (`rmc.py` / `deadcode.py`).

All 96 originals were moved with `git mv` into `legacy/<dir>/` — nothing was
deleted, so every behaviour can still be diffed against its replacement.

---

## 1. What the analysis found (this shapes every decision below)

These are fact, not opinion — each one is a reason a naive "concatenate the files" merge would fail.

**1.1 Fifteen scripts import a module that does not exist.**
`from dh import …` appears in **15 of 96 files** — `g001/clean-rs.py`, `g004/ex_imports.py`, `g005/compresstools.py`, `g005/fast_compress.py`, `g006/clean_type_annotations.py`, and ten in `rmc/` (`brmc, py_clean, pycleaner, rmc-ts, rmc, rmc2, rmc_termux, rmcts, srmc1, tmc`). There is no `dh.py` in the repo and nothing installable that matches; it was the author's private helper (`get_files`, `mpf`, `unique_path`, `SKIP_DIRS`). **A sixth of the repo cannot run as shipped.** The consolidation inlines those four helpers and drops the import.

**1.2 Three files do not even parse.**

| file | problem |
|---|---|
| `g006/clean_type_annotations.py` | real syntax error, line 153: `def main(...) -> int:    args = list(...)` — the body was joined onto the `def` line. Whole file dead. |
| `rmc/rmc3.py` | `type Backend = Callable[...]` (PEP 695) → needs 3.12+, fails to parse on ≤3.11 |
| `rmc/srmc2.py` | `def on_leave[N: cst.CSTNode](...)` (PEP 695 generics) → needs 3.12+ |

Only the first is a genuine bug; the other two are 3.12-only syntax that dies on
the 3.11 the rest of the repo assumes.

I will target **3.10+** for everything so the tools work on the Termux/older-interpreter setups these were clearly written on, and rewrite the PEP-695 bits.

**1.3 Two g004 scripts are silent no-ops.**
`ex_ex.py` filters on node types `{"function_docstrings", "class_docstrings"}` and `ex_comments.py` on `{"comment", "expression_statements"}`. Tree-sitter-python has neither `function_docstrings` nor `expression_statements` (the real name is `expression_statement`). They always produce empty output. Their *intent* (extract docstrings / comments) is kept, fixed.

**1.4 `g003`'s two scripts are the same program, one a strict subset.**
`python_duplicate_finder.py` is 2,430 lines; `pydup.py` is 2,954 lines and contains everything the former has plus `tlsh`, minhash/LSH banding, simhash, 5 more executor strategies and `--minhash/--simhash/--tlsh/--mp-method` flags. Diff is 620 lines, all additive except renames. **Keep `pydup.py`, delete the other.**

**1.5 `g002` has five different PDF→text engines and four different viewers.**
That is the real content of the folder; the union is a backend registry, not more code. Note `pdftools2.py` (pypdf + lxml + pdf2image) and `pdftoolz.py` (pymupdf only) are two rewrites of the same `merged.py`; `pdftoolz` is the better base because it has no `lxml` and no poppler dependency.

**1.6 `g005` has 3 benchmark harnesses and 2 directory-archivers doing the same job.**
`compression_benchmark.py` (stdlib + everything optional), `smart_archiver.py benchmark`, `xcompress.py bench`. All three answer "which codec wins for this data". Same for `archive_merger.py` ⊂ `archive_dir.py`.

**1.7 Nine spellings for the same three flags, across 96 files.**
`--dry-run` / `--dryrun` / `-n`; `--backup` / `-b` / `--keep-orig` / `--keep-originals`; `-j` / `-w` / `-t` / `--jobs` / `--workers` / `--threads` / `-P`. The consolidated CLIs pick one spelling each and keep the old ones as hidden aliases.

---

## 2. Cross-cutting contract (shared by all 9 files)

This is the part that makes 7 scripts feel like one toolkit.

### 2.1 Dependency policy — "stdlib first, everything else optional"
```python
def probe(name): ...          # find_spec + import, cached, returns None on failure
TS   = probe("tree_sitter")
LCST = probe("libcst")
```
* No mandatory third-party import. A script with zero optional deps installed still runs in its degraded but correct mode (regex/stlib codec).
* Every script gets a `doctor` (or `--backends`) command printing a capability table:
  `backend · available · version · what it enables · install hint`.
* `--backend auto` picks the best **available** backend; `--backend X` fails loudly if X is missing.

### 2.2 One traversal, one reporting layer, one safety layer
```python
iter_targets(paths, *, include, exclude, skip_dirs, recursive, max_depth, max_size, follow_symlinks)
```
* skip `.git .hg .svn .idea .vscode .venv venv node_modules __pycache__ .mypy_cache .pytest_cache .ruff_cache .tox .cache target dist build`
* never follow symlinks (default), never write through one, never touch a binary (`\0` in first 1 KiB) or a file > `--max-size`.
* **atomic write**: `tmp` in same dir → `copymode` → `os.replace`; `.bak` written only if absent unless `--force-backup`.
* **never emit broken output**: every writer re-parses the result; if the result is worse than the input, the file is *rejected and left untouched* and reported.

### 2.3 One CLI grammar
```
<script> [SUBCOMMAND] [OPTIONS] [PATH ...]
```
Shared option block, identical spelling everywhere:

| flag | meaning |
|---|---|
| `-n, --dry-run` | compute, report, write nothing (alias `--dryrun`) |
| `--diff` | unified diff to stdout (implies dry-run) |
| `--check` | write nothing; **exit 1 if anything would change** (CI mode) |
| `--backup` / `--backup-suffix SUF` | copy `<file><SUF>` before writing (default `.bak`) |
| `-j, --jobs N` | worker processes (aliases `-w`, `--workers`, `--threads`) |
| `-q / -v / --color {auto,always,never}` | verbosity / colour |
| `--json [FILE]` | machine-readable report (`-` = stdout) |
| `--exclude GLOB` / `--include GLOB` / `--skip-dir NAME` / `--no-recursive` / `--max-depth N` | traversal |
| `--max-size BYTES` | skip big inputs |
| `--stats` | per-file + total summary |

Exit codes everywhere: **0** success, **1** errors *or* "`--check` found pending work", **2** usage error, **130** interrupted.

### 2.4 JSON report (same shape in all scripts)
```json
{"tool":"comments","version":"1.0","root":"/x","started":...,"elapsed":0.4,
 "counts":{"changed":3,"unchanged":9,"skipped":1,"error":0,"rejected":0},
 "bytes":{"before":1200,"after":900,"saved":300},
 "files":[{"path":"a.py","status":"changed","lang":"python","backend":"tree-sitter",
           "before":400,"after":310,"removed":3,"detail":""}]}
```

### 2.5 Legacy compatibility
Original flag spellings stay as `argparse` aliases where they don't collide (e.g. `--dryrun`, `-diff`, `--keep-orig`). No original *capability* is dropped; several are documented in `--help` epilogues as "was: `clean-html.py --backend htmlmin`".

---

## 3. Per-directory design

### 3.1 `g001` → `g001/comments.py` — **reference implementation written**

**Job:** remove comments from source files; 22 languages; preserve the ones that change behaviour (shebang, `# type:`, `# noqa`, `eslint-disable`, `@license`, `SPDX`, `//go:`, `# shellcheck`, Rust `SAFETY:` …).

**Duplication map**

| cluster | scripts | note |
|---|---|---|
| per-language tree-sitter strippers | `cccpp, clean-bash, clean-cpp, clean-css, clean-html, clean-js, clean-lua, clean-rs, clean-toml, clean-ts, cleanbash, cleanlua, cleanrs, cleanvim` | 14 × same 200-line program, one language each |
| multi-language tree-sitter strippers | `cleants, cleants2, uncomment` | 3 generations of the same generic engine; `uncomment` = newest |
| JS/TS duplicates | `cleanjsts, clean-js, clean-ts` | same job, 3 impls |
| Python-specific | `cleanpy` (libcst) | only one that understands docstrings |
| alternative backends | `clean-css` (tinycss2, csscompressor), `clean-html` (htmlmin), `clean-js/ts` (rjsmin, jsmin), `clean-lua` (luaparse = validate only), `clean-toml` (toml re-parse), `clean-rs` (rich+`dh`) | the "another library" implementations you mentioned |

**Design**
```
comments.py [options] PATH...            # one operation, so no subcommands
             └── --list-languages / --doctor
```
Backends (`-b/--backend`): `auto` (default) · `tree-sitter` (precise, 22 languages) · `regex` (stdlib mini-lexer, no deps) · `libcst` (Python, understands docstrings) · `minify` (rjsmin/jsmin, csscompressor, htmlmin — aggressive, drops whitespace too).

Flags:

| group | flags |
|---|---|
| what | `-l/--language`, `--docstrings`, `--module-docstring`, `--keep-doc`, `--all`, `--keep-regex`, `--keep-shebang/--no-keep-shebang` |
| how | `-b/--backend`, `--verify {none,parse,tokens}` (default `tokens`), `--preserve-lines`, `--collapse-blank-lines`, `-i/--interactive` |
| io | `-n --dry-run`, `--diff`, `--check`, `--backup`, `--backup-suffix`, `--stdout`, `-j`, `--max-size`, `--exclude`, `--no-recursive`, `--json`, `--stats`, `-q/-v/--color` |

Coverage vs. the 19 originals:
`--interactive` (was `cccpp -i`), `--collapse-blank-lines` (was `cleanvim`), `--preserve-lines` (was `rmcomdocs`-style), `--keep-doc` (was Rust `///`), `--all` (was `cleants2`), minify backends (was `-b htmlmin` / `csscompressor` / `rjsmin` / `jsmin`), docstrings (was `cleanpy`).

**Correctness gate (the reason this can be trusted):** after stripping, the file is re-parsed with tree-sitter; then the *leaf-token signature* of the old and new trees (comment nodes removed, docstrings substituted by `pass`) must be **identical**. If not → rejected, file untouched. Ported from `cleants.py`, and it is what makes a 20-language tool safe with one code path.

---

### 3.2 `g002` → `g002/pdftool.py`

**Job:** everything PDF. The folder is really 4 tools wearing 12 names.

| cluster | scripts |
|---|---|
| PDF → text | `pdf2text`, `pdftotext`, `pdf2page`, `pdftools -p` |
| PDF → images | `pdf2img` (pymupdf→jpg, deletes source), `pdf2png` (pdftoppm→png), `pdftoolz to-img` |
| interactive terminal viewer | `pdfv` (pymupdf + readchar), `pdfv2` (pdftoppm), `pdfviewer` (fitz), `pdfview` (text pager + OCR fallback) |
| document surgery | `pdftools` (split/merge/extract images+fonts+attachments), `pdftools2`/`pdftoolz` (outline/merge/shrink/rename/split-size) |

**Design — subcommands:**
```
pdftool doctor
pdftool text     [--engine fitz|pypdf|pdfminer|plumber|pypdf2|poppler|ocr]
                 [--per-page|--concat] [-o DIR] [-p 1,3-5] [--password] [--layout-kwargs…] [--sort]
pdftool image    [-o DIR] [--dpi 300] [--format png|jpg] [--quality 95] [--engine fitz|poppler] [--delete-source]
pdftool view     [--render image|text] [--dpi N] [--zoom Z] [--page N]
pdftool outline  --html print.html --summary src/SUMMARY.md [-o OUT] [--sync-titles] [--check-titles]
pdftool merge    -o merged.pdf A.pdf B.pdf…
pdftool split    [--per-page|--max-mb 5|--ranges 1-3,7] [-o DIR]
pdftool extract  [--what images,fonts,attachments,text] [-o DIR]
pdftool shrink   --engine gs|qpdf|mutool [--level] [-o DIR]
pdftool rename   [--pattern '{stem}_{pages}p']
pdftool info     [--json]
```
`--engine` collapses pdfminer/pypdf/pypdf2/pdfplumber/fitz/poppler (`pdftotext`,`pdftoppm`,`pdfinfo`)/tesseract-OCR into one registry — that alone replaces `pdf2page.py`'s ~180 lines of engine dispatch, `pdf2text.py`, `pdftotext.py`, `pdfview.py`'s backend half.
`view --render image|text` merges the four viewers: `pdfv/pdfv2/pdfviewer` are the image (ANSI half-block) family with different renderers; `pdfview` is the pager family.

Kept: mdBook `SUMMARY.md` outline builder with the `--sync-titles` self-healing path from `pdftoolz` (the pypdf version in `pdftools2` dies when `named_destinations` is missing).

---

### 3.3 `g003` → `g003/pydup.py`

**Job:** find duplicate / near-duplicate Python modules. Already a single argparse program with 40 flags; it just exists twice.

**Design:** keep `pydup.py` as the base (it is the superset — see §1.4), delete `python_duplicate_finder.py`, then clean:

* one `--mode {strict,default,loose,structural}` (AST normalisation level; already there),
* one similarity registry: `ssdeep · tlsh · simhash · minhash` behind `--similarity`, degrading gracefully when the C extension is missing (currently `_hash_tlsh` silently returns `None` — will say *why* in `--doctor`),
* keep `--to-trash/--quarantine/--dry-run/--diff/--keep/--prefer/--pattern/--manifest/--baseline/--changed-since/--watch`,
* add the shared contract bits it lacks: `--json`, uniform exit codes, `doctor`.

No flag is removed. This is the cheapest folder: ~150 lines of dead code and one duplicate file go away.

---

### 3.4 `g004` → `g004/extract.py`

**Job:** walk a Python tree, pull one *kind* of declaration out of every file, and dump it into `output/<subfolder>/<kind>.py`, so you can read "all my classes" in one place.

| script | kind | state |
|---|---|---|
| `ex_class` | `class_definition` | works |
| `ex_func` | `function_definition` | works |
| `ex_imports` | import statements | works, but uses `dh` |
| `ex_comments` | comments | **broken** (`expression_statements` typo) |
| `ex_ex` | docstrings | **broken** (invented node types) |
| `ex_const` | module-level UPPER constants, deduped by file hash | works, different mechanism (AST + xxhash) |

**Design:**
```
extract.py {classes,functions,imports,comments,docstrings,constants,all} [PATH…]
           [-o DIR] [--per-folder|--per-file|--single] [--dedupe {none,content,name}]
           [--min-lines N] [--strip] [-j N] [-n] [--json]
```
* Backend: `ast` (stdlib, always available) **or** `tree-sitter` when installed — `ast` is enough for declarations *and* fixes the two broken scripts, because docstrings are just `Expr(Constant(str))` first statement.
* `--dedupe content` preserves `ex_const.py`'s real idea (identical files across a tree collapse to one set of constants).
* Output layout keeps the originals' contract: `output/<relative folder>/<kind>.py`.

---

### 3.5 `g005` → `g005/codecs.py` + `g005/archive.py`

**Job:** compress, archive, decompress, pick the best codec, and audit an archive tree. 24 scripts, 13.7 k lines.

| cluster | scripts | went to |
|---|---|---|
| single codec, whole dir | `br`, `brer`, `lz4er`, `zstdtools`, `xztools`, `gzbz`, `compresstools`, `compress_text`, `fast_compress` | `codecs.py` |
| codec-agnostic engine | `xcompress` (bench half) | `codecs.py bench` |
| benchmarking | `compression_benchmark`, `smart_archiver benchmark`, `xcompress bench` | `codecs.py bench` |
| archive containers | `archive_tools`, `xtar`, `trydecomp`, `7ziper`, `py7zmax`, `archive_convert`, `artools` | `archive.py` |
| dir → archive | `archive_dir`, `archive_merger`, `compress_subdirs`, `smart_archiver archive` | `archive.py pack --per-subdir/--merge` |
| audit / surgery | `xreport`, `rm_from_archive` | `archive.py info/list`, `archive.py remove` |

**Why two files.** A stream codec is "one file in, one file out"; an archive is a
member table plus a container. Trying to hold both in one 2,600-line file made
every command read through a format test first. The split is by *input shape*,
not by script origin, and the two files share no imports — each runs alone.

**`codecs.py`** — one `Codec` dataclass drives compress, decompress, bench,
inspect and doctor:

```python
Codec(name, ext, low, high, default, hint,
      open_write, open_read,      # streaming adapters (raw sink/source -> codec)
      once, once_decode,          # one-shot, also used for framed blocks
      magic, aliases, chunked_only)
```

| codec | backend | levels | note |
|---|---|---|---|
| gzip bz2 xz lzma zlib | stdlib | 0-9 | always available |
| zstd brotli lz4 | wheel **or** CLI (`zstd`, `brotli`, `lz4`) | 1-22 / 0-11 / 0-16 | probed at run time |
| snappy ppmd zopfli | `python-snappy`, `pyppmd`, `zopfli` | — / 1-9 / 1-49 | ppmd + zopfli only exist as framed blocks |

```
codecs.py compress   PATH… -a zstd [-l 19] [-o DIR] [--chunk-size 64M] [--min-size 4K]
                     [--no-skip-ext] [--verify] [--remove-source] [-j N] [-n] [-f] [--backup] [--json]
codecs.py decompress PATH… [-a auto|CODEC] [-o DIR] [--verify] [--remove-source] [-j N]
codecs.py bench      PATH… [-a gzip,bz2,xz,zstd,brotli,lz4,ppmd] [--level N] [--json]
codecs.py inspect    PATH… [--deep]          # magic -> codec, declared/actual size
codecs.py doctor
```

Framed blocks (`PYCHUNK\x01` + codec name + `[u64 len][payload]`…) exist for three
reasons that each cost a legacy script: codecs with no streaming API (ppmd,
zopfli), very large files (`--chunk-size`), and letting a renamed `.xz` still
decompress because the frame carries its own codec name.

**`archive.py`** — one `Format` table drives pack, unpack, list, info, check,
split, remove, merge, convert, fix and bench:

```python
Format(name, suffixes, kind, codec, hint)
tar · tar.gz · tar.bz2 · tar.xz · tar.zst · tar.br · tar.lz4   kind="tar"  + codec
zip (zip/whl/jar) · 7z (py7zr)                                  kind="zip" | "7z"
gzip bz2 xz lzma zstd brotli lz4                                kind="stream"
```

```
archive.py pack    PATH… [-o DIR | --output FILE] [--format auto|tar.zst|zip|7z|…]
                   [--per-subdir] [--merge] [--cleanup] [--no-compress] [--exclude GLOB] [-j N]
archive.py unpack  ARCHIVE… [-o DIR] [--strip-components N] [--members GLOB] [--skip-existing]
archive.py list    ARCHIVE… [--long]
archive.py info    ARCHIVE…                     # format, members, ratio
archive.py check   ARCHIVE…                     # full read / zip testzip / 7z test
archive.py split   ARCHIVE --parts N [-o DIR] [--format …]
archive.py remove  ARCHIVE MEMBER… [-o OUT] [--backup]
archive.py merge   ARCHIVE… -o OUT
archive.py convert IN --format tar.xz [-o OUT]  # replaces whl<->tar.xz, archive-convert
archive.py fix     ROOT [--apply]               # dir/name/name misextraction repair
archive.py bench   PATH --formats tar.gz,tar.xz,tar.zst,zip
archive.py doctor
```

Safety, since `trydecomp.py` shipped a working zip-slip:

* every member name is validated before anything is written (`..`, absolute
  paths and backslash tricks are refused and reported as failures);
* writes go to `.<name>.<pid>.tmp<suffix>` and are `os.replace`d, so an
  interrupted pack never leaves a truncated archive;
* `pack --cleanup` only removes a source after the archive exists;
* `--dry-run` prints the member list and creates neither files nor directories.

Not ported, deliberately: `rar/cab/ace/iso/pycdlib/libarchive` sniffing. Those
formats need external libraries that are not installable from PyPI alone; the
magic table still *names* them in `inspect`, it just does not claim to open them.

---

### 3.6 `g006` → `g006/annotations.py`

**Job:** put type annotations **on**, or take them **off**, across a Python tree.

| script | mode | engine |
|---|---|---|
| `annotate_types.py` | add, from `stubgen`-generated `.pyi` | libcst `ApplyTypeAnnotationsVisitor` |
| `add_hints.py` | add, from heuristics (`name→str`, literal inference, return-type inference) | libcst |
| `annotate.py` | **both** (add + `--remove`) | libcst + tree-sitter |
| `clean_type_annotations.py` | remove | libcst — *syntax error* |

**Design:** `annotate.py` is already the right frame; extend it into
```
annotations.py add    PATH… [-s STUB] [--source stubgen|heuristic|auto] [--no-overwrite]
                      [--future-annotations] [--validate {compile,mypy,none}] [-j N]
annotations.py remove PATH… [-b libcst|tree-sitter|ast] [--keep-module-docstring] [--keep-casts]
annotations.py check  PATH… [--strict]      # exit 1 if a file is missing annotations / has stale ones
annotations.py doctor
```
`--source heuristic` resurrects `add_hints.py` (its `NAME_HINTS` table + `infer_literal_type` + `_ReturnCollector`), `--source stubgen` is `annotate_types.py`, `remove` repairs `clean_type_annotations.py`. Also fixed: the `_typeshed.Incomplete → Any` sanitiser currently rewrites *user* code that merely mentions `Incomplete`; scoped to the generated stub only.

---

### 3.7 `rmc` → `rmc/rmc.py`

**Job:** the Python-only superset of g001 — strip comments **and** docstrings **and** type annotations from `.py`, with several engines and heavy safety machinery. 29 scripts, 15.4 k lines, ~30 different flag spellings.

| cluster | scripts |
|---|---|
| tree-sitter | `rmc`, `rmc-ts`, `rmcts`, `tsrmc`, `tscleaner`, `armc`, `rmcomdocs` |
| libcst | `rmcst`, `grmcst`, `felormc`, `crmc`, `tcs`, `brmc` |
| AST / multi-engine | `rmc2`, `rmc3`, `drmc`, `rmca`, `rmcco`, `rmc_termux`, `tmc`, `srmc1`, `srmc2`, `pyclean`, `py_clean`, `frmc` |
| benchmark | `benchrmc` |
| **different goal** | `pycleaner` (unused imports / dead functions / code-block removal — *not* comment stripping) |

**Design:**
```
rmc.py strip    [PATH…]  -c/--comments  -d/--docstrings  -t/--annotations  -a/--all
                -b tree-sitter|libcst|ast  [--ast-mode splice|unparse] [--fallback regex]
                [--keep-shebang] [--keep-module-docstring] [--keep-noqa] [--keep-todo]
                [--keep-prefix REGEX] [--preserve-lines|--blank] [--eat-trailing-newline]
                [--remove-blank-lines] [-j N] [--pool-method …] [--chunksize N]
                [-n] [--diff] [--check] [--backup [--backup-dir DIR]] [--json] [--report] [-q/-v]
rmc.py check    [PATH…]              # report only (was pyclean.py check)
rmc.py bench    [PATH…] [-b …]       # was benchrmc.py
rmc.py doctor
```
Three engines kept on purpose — they fail differently, and each original exists because of a real failure mode:
* `tree-sitter` — fastest, byte-precise, keeps formatting, can't see Python semantics;
* `libcst` — round-trips whitespace exactly and understands docstring *ownership* (`--keep-module-docstring`);
* `ast` with **`--ast-mode splice`** — operates on `lineno/col_offset` spans so comments and blank lines survive (the trick that makes `brmc.py`/`pyclean.py ast-rewrite` the only ones that don't reformat your file), vs `--ast-mode unparse` (`ast.unparse`, destructive, last resort).
Every engine is followed by the same `compile()` + token-signature gate.

**Decision (yours, §7):** `pycleaner.py` is a different goal — unused imports,
dead functions and licence-block removal — so it shipped as its own file:

```
deadcode.py imports [PATH…] [-r] [--scope file|global] [--apply] [--backup] [--check] [--json]
deadcode.py unused  [PATH…] [--kinds functions,classes,constants] [--apply] [--check]
deadcode.py block   [PATH…] --block-file LICENCE.txt [--apply] [--check]
deadcode.py doctor
```

`rmc.py` keeps the four subcommands the folder justified: `strip`, `check`,
`bench` (the three engines really do rank differently) and `doctor`.

---

## 4. What was dropped, and what replaced it

| file | why | replaced by |
|---|---|---|
| `g003/python_duplicate_finder.py` | strict subset of `pydup.py` (620-line diff, all additive) | `g003/pydup.py` |
| `g005/archive_merger.py` | primitive duplicate of `archive_dir.py` | `archive.py pack --per-subdir --merge` |
| `g005/compression_benchmark.py`, `smart_archiver.py` benchmark | 3rd copy of the same bench | `codecs.py bench` |
| `g002/pdf2page.py`, `pdf2text.py`, `pdftotext.py` | engines folded into one registry | `pdftool.py text -e …` |
| `g002/pdftools2.py`, `pdftoolz.py` | two rewrites of the same outline builder | `pdftool.py outline` |
| `g001/cleants.py`, `cleants2.py` | older generations of `uncomment.py` | `comments.py` |
| `g004/ex_comments.py`, `ex_ex.py` | node types that do not exist — never produced output | `extract.py` (fixed with `tokenize`/`ast`) |
| `rmc/rmc3.py`, `rmc/srmc2.py` | PEP 695, 3.12-only, subsets of `rmc.py` | `rmc.py strip -b …` |
| `rmc/cleants*.py`, `srmc1.py` | same engine, fewer flags | `rmc.py strip` |
| `g006/clean_type_annotations.py` | does not parse (line 153) | `annotations.py remove` |

Every dropped file still exists under `legacy/`; nothing was deleted. Everything
not listed keeps its *behaviour* as a flag or subcommand, with the old name in
the `--help` epilogue (`was: …`).

---

## 5. Dependency situation

Every tool runs with **zero** third-party packages; the optional ones only unlock
better paths, and each tool's `doctor` says which are present.

| tier | packages | used by | state in `.venv` |
|---|---|---|---|
| core stdlib | `gzip bz2 lzma zlib tarfile zipfile hashlib ast difflib tokenize` | always | — |
| parser | `tree_sitter` + 11 grammars | g001, g004, g006, rmc | installed |
| codemod | `libcst` | g001, g006, rmc | installed |
| pdf | `pymupdf`, `pypdf`, `pdfminer.six` (poppler/gs/qpdf optional) | g002 | installed (no CLI tools in this image) |
| codecs | `zstandard brotli lz4 py7zr pyppmd` | g005 | installed |
| similarity | `ssdeep tlsh xxhash` | g003 | optional, degrades to stdlib hashes |
| cosmetics | `rich`, `loguru` | g003 (used to be everywhere) | not needed — output is plain lines |

The old code assumed `rich`/`loguru` everywhere; both are now optional imports
with a plain-text fallback, which is why every tool still runs on a bare Termux
python.

---

## 6. How each consolidated script gets proven

1. **Behavioural diff harness.** For every original script that runs at all, run *old* and *new* over a fixture tree (real files: `g001/*.py` for Python/Rust/C/etc. samples, generated PDFs, a 200-file archive dir) and require identical output bytes for the same semantics.
2. **Idempotence.** `f(f(x)) == f(x)` for every stripper; stripping twice removes nothing the second time.
3. **Round-trip gate.** `ast.parse` / `tree_sitter.parse` of every output must succeed and the token signature must match the input minus comments.
4. **Dry-run purity.** `--dry-run` must leave `git status` clean; `--check` exit code 1 iff work pending.
5. **No-write-on-reject.** Feed syntax-broken and binary files: expect status `skipped`/`rejected`, zero bytes written.
6. **CLI smoke matrix.** `--help`, `doctor`, empty args, bogus backend, missing path, `-j 8` on a 1-file dir.
7. A `tests/` directory with these as pytest, per script. (Fixture data stays out of git; generated by a `conftest.py`.)

---

## 7. Decisions you made (all implemented)

| # | question | your answer | where it landed |
|---|---|---|---|
| 1 | `rmc/pycleaner.py` — drop, fold in, or separate? | **separate** | `rmc/deadcode.py` (614 lines) |
| 2 | old files — delete or keep? | **move to `legacy/`** | 96 files `git mv`'d into `legacy/<dir>/` |
| 3 | Python floor | **3.10** | `from __future__ import annotations` everywhere, no PEP 695, no `match`-only tricks |
| 4 | exotic flags — trim or keep? | **keep, full superset** (`--sync-titles` named explicitly) | `pdftool.py outline --sync-titles/--check-titles`, `rmc.py --preserve-lines/--eat-trailing-newline`, … |
| 5 | `g005` — one file or two? | **split** | `g005/codecs.py` + `g005/archive.py` |

Consequence of #4: the CLIs are wide. Every flag that had a job in the originals
still has one; nothing was trimmed to make the help text shorter.

---

## 8. What shipped, and how it was proven

| file | lines | subcommands | validated against |
|---|---|---|---|
| `g001/comments.py` | 1,414 | (one operation) `-b tree-sitter\|regex\|libcst\|minify` | 9-file / 8-language fixture, backend parity, idempotence, `-j 8` == serial, token gate |
| `g002/pdftool.py` | 1,104 | `text image view merge split extract shrink check outline info doctor` | generated PDFs, pymupdf **and** pypdf-only paths, `--sync-titles` alignment, page-range errors, dry-run purity |
| `g003/pydup.py` | 3,024 | one scanner + `--doctor` | fixture tree, `text/json/ndjson/csv`, dry-run removal plan, exit codes |
| `g004/extract.py` | 563 | (one operation) `--kinds` | 12 outputs on a fixture tree, `--dedupe content`, `--nested --header`, syntax-error → exit 1 |
| `g005/codecs.py` | 1,238 | `compress decompress bench inspect doctor` | 9 codecs × round-trip hash, framed blocks, `--verify`, dry-run, duplicate-target guard |
| `g005/archive.py` | 1,426 | `pack unpack list info check split remove merge convert fix bench doctor` | 13 formats round-trip, zip-slip refusal, `--per-subdir --merge`, 7z member filter |
| `g006/annotations.py` | 1,036 | `add remove check doctor` | heuristic adds, stub adds, 3 remove backends (`tree-sitter` == `libcst` byte-identical), type comments, gate rejections |
| `rmc/rmc.py` | 1,293 | `strip check bench doctor` | engine parity matrix (`-d`/`-t` identical across 3 engines), idempotence, verify() rejection |
| `rmc/deadcode.py` | 614 | `imports unused block doctor` | grouped findings, `--apply` rewrites, `--check exit 1`, per-file compile gate |

**How "the goals still hold" was checked, per file** — the same six checks every
time, on fixtures in `/tmp`:

1. round-trip / parity against the legacy behaviour (engine A vs engine B, or
   old output vs new output);
2. idempotence where it applies (`f(f(x)) == f(x)`);
3. `--dry-run` writes nothing and creates no directories;
4. `--check` exits 1 iff work is pending;
5. `--json` parses and follows the shared envelope (`tool version command elapsed counts files[]`);
6. `--help`, `doctor`, bogus input, missing file, and `-j 8` on a one-file tree.

### Known, deliberate limits

* `libcst` leaves an indent-only line where a docstring/comment was removed;
  tree-sitter and ast agree byte-for-byte. `rmc.py` documents this and does not
  pretend the three engines are interchangeable for `-c`/`-a`.
* PDFs: `--diff` has no meaning for binary output, so `pdftool.py` does not
  offer it; the equivalent safety net is `-n/--dry-run` plus `--backup`.
* `archive.py` does not open `rar/cab/ace/iso` — no installable pure-Python
  reader here. `inspect` still names them.
* The `regex` backend of `comments.py` refuses languages it cannot lex safely
  (`tsx`, vimscript, Rust lifetimes) instead of corrupting them.

### Layout

```
g001/comments.py       g002/pdftool.py      g003/pydup.py      g004/extract.py
g005/codecs.py         g005/archive.py     g006/annotations.py
rmc/rmc.py             rmc/deadcode.py
legacy/g001 … legacy/rmc    # the 96 originals, untouched
PLAN.md
```
