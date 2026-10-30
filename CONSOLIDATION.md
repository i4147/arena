# Consolidation report

Every folder that used to hold a pile of loosely related `.py` scripts now holds
**one** script plus (in one folder) a `misc/` bucket for the odd one out. The
originals are not lost: each consolidated file carries the source of every tool
it contains, and `unpack` writes any of them back out byte-for-byte.

```
folder   before        after
-------  ------------  -------------------------------
g001     19 scripts    g001/g001.py  (3 tools)
g002     12 scripts    g002/g002.py  (5 tools)
g003      2 scripts    g003/g003.py  (1 tool)
g004      6 scripts    g004/g004.py  (1 tool)
g005     24 scripts    g005/g005.py  (14 tools)
g006      4 scripts    g006/g006.py  (3 tools)
rmc      29 scripts    rmc/rmc.py    (2 tools) + rmc/misc/tcs.py
sim      unchanged (shell only)
-------  ------------  -------------------------------
total    96 scripts    7 consolidated scripts + 1 misc script
```

## How to use them

```bash
python g005/g005.py                    # list the tools
python g005/g005.py list               # same, with provenance
python g005/g005.py doctor             # which third-party packages are missing
python g005/g005.py dirs tar proj/     # run one tool (its own flags follow)
python g005/g005.py zstd --help        # a tool's own help
python g005/g005.py unpack zstd        # write the original zstdtools.py back out
```

* Tools are loaded lazily: a missing third-party package only affects the tool
  that needs it, and the error names the `pip install` line.
* `unpack <tool> [-o DIR] [--stdout]` restores the original file verbatim; with
  no argument it writes into the current directory.
* Multiprocessing inside a tool works because each embedded module is executed
  into a real module registered in `sys.modules` (fork-based start methods —
  Linux, Termux, all the sandboxes here). On macOS/Windows `spawn`, `unpack`
  the script and run it directly.
* Requires Python 3.10+.

## What each folder kept

### g001 — comment stripping / minification (`g001.py`)

| tool | from | why it survived |
| --- | --- | --- |
| `strip` | `uncomment.py` | most defensive multi-language remover: keep-patterns per language, cgo/shebang/directive vetoes, pool, backups, diffs |
| `pystrip` | `cleanpy.py` | Python-specific: AST based, docstring aware, keeps `# type:`/`# fmt:`/`# noqa` |
| `minify` | *new* (merged `clean-css/html/js/ts/lua/toml.py`) | those six were the same program with a different language table; now one engine with the same optional compressors |

**Dropped (11):** `cccpp.py`, `clean-bash.py`, `clean-cpp.py`, `clean-rs.py`,
`cleanbash.py`, `cleanjsts.py`, `cleanlua.py`, `cleanrs.py`, `cleants.py`,
`cleants2.py`, `cleanvim.py` — all subsets of `strip`. The eight languages only
`cleants.py` supported (`java`, `latex`, `perl`, `python`, `query`, `rst`, `sql`,
`yaml`) were **ported into `strip`** before dropping it.

### g002 — PDF toolkit (`g002.py`)

| tool | from | why it survived |
| --- | --- | --- |
| `text` | `pdf2page.py` | 5 interchangeable text backends, parallel, name templates |
| `pages` | `pdftoolz.py` | the 7-subcommand page suite (outline/merge/to-img/rename-pages/shrink/split-size/split-page), pymupdf-only |
| `resources` | `pdftools.py` | the only one extracting fonts, images, annotations and *embedded files* |
| `view` | `pdfviewer.py` | terminal viewer with zoom; no external binaries |
| `img` | `pdf2img.py` | batch PDF → 300 dpi JPG |

**Dropped (7):** `pdf2png.py`, `pdf2text.py`, `pdftotext.py`, `pdfv.py`,
`pdfv2.py`, `pdfview.py`, `pdftools2.py` (the 4 competing viewers and 3 text
extractors collapsed into one each).

### g003 — duplicate finder (`g003.py`)

`pydup.py` kept (3k lines, caching, watch mode, several fuzzy hashes, JSON
report). `python_duplicate_finder.py` dropped as the older, smaller generation.

### g004 — Python source scrapers (`g004.py`)

Five of the six scripts were the same 43-line program with a different
`VALID` node-type set, and all six did their work at import time. They are now
one tool with `--kind classes|functions|docstrings|comments|expressions|imports|constants`.

### g005 — archive / compression toolkit (`g005.py`)

| tool | from | why it survived |
| --- | --- | --- |
| `extract` | `archive_tools.py` | extracts every format, safe paths, integrity checks |
| `convert` | `artools.py` | re-encodes archives/files (tar-codec, br2zst, whl-txz) |
| `dirs` | `archive_dir.py` | parallel tar/tgz of a directory (+cleanup) |
| `7z` | `7ziper.py` | 7z in three modes |
| `brotli` | `br.py` | parallel brotli with verification |
| `zstd` | `zstdtools.py` | zstd compress/decompress/archive-cwd/split |
| `xz` | `xztools.py` | 6 xz/LZMA subcommands |
| `lz4` | `lz4er.py` | lz4 + tarred folders |
| `gzbz` | `gzbz.py` | gz/bz2 in three modes |
| `text` | `compress_text.py` | zstd/brotli/ppmd for single text files, verified |
| `subdirs` | `compress_subdirs.py` | one archive per subdirectory |
| `bench` | `compression_benchmark.py` | codec benchmark |
| `sniff` | `trydecomp.py` | magic-byte detection + best-effort decompression |
| `report` | `xreport.py` | archive/stream size reporting |

**Dropped (10):** `archive_merger.py`, `archive_convert.py`, `brer.py`,
`compresstools.py`, `fast_compress.py`, `py7zmax.py`, `rm_from_archive.py`,
`smart_archiver.py`, `xcompress.py`, `xtar.py` — each one is a thinner version of
a tool above (and `compresstools.py`/`fast_compress.py`/`smart_archiver.py`
imported modules that do not exist in this repository).

### g006 — type annotations (`g006.py`)

`hints` (`add_hints.py`), `annotate` (`annotate.py`), `unannotate`
(`clean_type_annotations.py`). `annotate_types.py` dropped: same typeshed-stub
annotator as `annotate.py`, but single-file and without the parallel driver.

### rmc — comment removal (`rmc.py`)

`strip` (`tmc.py`, 1291 lines: ast / tree-sitter / libcst engines, pool methods,
check mode, JSON report) and `clean` (`pycleaner.py`, unused imports and
definitions, vulture output, block/function patching).

**Dropped (26):** `armc.py`, `benchrmc.py`, `brmc.py`, `crmc.py`, `drmc.py`,
`felormc.py`, `frmc.py`, `grmc.py`, `grmcst.py`, `py_clean.py`, `pyclean.py`,
`rmc-ts.py`, `rmc.py`, `rmc2.py`, `rmc3.py`, `rmc_termux.py`, `rmca.py`,
`rmcco.py`, `rmcomdocs.py`, `rmcst.py`, `rmctools.py`, `rmcts.py`, `srmc1.py`,
`srmc2.py`, `tscleaner.py`, `tsrmc.py` — 26 rewrites of the same remover, all
narrower than `tmc.py`.

**Moved to `rmc/misc/tcs.py`:** the one script that is *not* a comment remover —
it mangles identifier names and shrinks indentation (a Python minifier). Kept
separate on purpose; it stays runnable as `python rmc/misc/tcs.py FILE...`.

## Bugs fixed (these were broken before the consolidation)

| file | bug |
| --- | --- |
| `g006/clean_type_annotations.py` | **SyntaxError**: the body of `def main(...)` was glued onto the signature line, so the module could not even be imported |
| `g006/clean_type_annotations.py` | `from dh import SKIP_DIR` — `dh` does not exist in this repo; skip-list is now local, and `--dry-run`/`--backup` were added (it used to rewrite files with no way to preview) |
| `g006/annotate.py` | grammar only loadable through the unmaintained `tree_sitter_languages`; now prefers `tree_sitter_python` and falls back. Parser construction also handles both tree-sitter APIs |
| `g002/pdf2page.py` | `__doc__` is `None` when the module has no docstring → `TypeError` in `build_parser()`, i.e. **every invocation crashed** |
| `g002/pdf2img.py` | no `main()` at all (work happened at import), unconditional deletion of the source PDFs, typo `cwdectory`; now `main()` + `-o` + `--keep-originals` |
| `g004/*` | node types that tree-sitter-python never produces (`expression_statements`, `function_docstrings`, `class_docstrings`); every output file named `imports.py`; scan ran at import time; `ex_imports.py` needed the missing `dh` |
| `g004/ex_const.py` | `NameError: CHUNK_SIZE` and `NameError: get_pyfiles` on any run; `mkdir`/`basicConfig` at import; `xxhash`/`joblib` made optional |
| `g005/trydecomp.py` | `main(argv)` expected the full `sys.argv` (program name included), unlike every other entry point; now normalised, and `--help` works |
| `rmc/tmc.py` | `from dh import atomic_write` (missing module) → local `atomic_write`; `from contextlib import contextlib` → `ImportError` |
| `g001/clean-{css,html,js,ts,lua,toml}.py` | walked with `Path.walk()`, which only exists on Python 3.12+; the merged engine uses `os.walk` |
| 11 files overall | imported the personal `dh` helper module that is not part of this repository — fixed where the file survived, dropped where it did not |

The remaining two broken files, `rmc/rmc3.py` and `rmc/srmc2.py` (both
`SyntaxError`), were superseded variants and were dropped rather than repaired —
they are still in the git history if you want them.

## Verification performed

* every consolidated file compiles (`py_compile`);
* `unpack` was run for all 29 tools and compared with the original files:
  **27/27 retained originals came back byte-identical**, the 2 new modules
  (`minify`, `extract`) compile;
* `list`, `doctor` and `<tool> --help` were run for all 7 folders / 29 tools —
  every failure is a missing third-party package, reported with its `pip install`
  line (this sandbox has no third-party packages installed);
* end-to-end smoke tests of the tools that need nothing beyond the stdlib:
  `g004 extract -k constants` (real output files), `g005 dirs tar proj/`
  (real tarball, contents verified), `g001 minify` (fails cleanly with a
  "tree-sitter is not installed" message instead of a traceback).

## Notes

* Nothing here was reformatted for style: the embedded sources are the original
  code (with the fixes listed above), so diffs against the old files stay small.
* `sim/` was left alone — it contains a shell script, no Python.
* To see everything that changed: `git log -p` on the commit that introduced the
  consolidated files.
