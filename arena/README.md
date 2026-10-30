# `arena/` — the helpers used to build this repo

These are the throwaway scripts from the consolidation session, kept because they
are the evidence: each one re-runs a check whose result is claimed in `PLAN.md`.
Nothing here is needed to *use* the tools in `g001/` … `rmc/`. Nothing here is
imported by them.

```bash
arena/setup_venv.sh            # rebuild .venv (pymupdf pypdf libcst tree-sitter pyflakes)
arena/verify_tools.sh          # pyflakes + --help + doctor over all 11 files
arena/make_pdf_fixtures.py     # /tmp/pdft: doc1.pdf doc2.pdf big.pdf SUMMARY.md print.html
arena/smoke_pdftool.sh         # 37 checks against the 7-engine build
arena/smoke_pdftool.sh --fitz  # 41 checks against the PyMuPDF-only build
arena/smoke_rmc.sh             # 30 checks for rmc_ts.py, using dh or a stand-in
arena/parity_g006.py           # tree-sitter output == libcst output, byte for byte
```

## What each file is

| file | what it does | why it exists |
|---|---|---|
| `setup_venv.sh` | creates `.venv` and installs the five optional packages | `.venv/` is in the snapshot exclusion list and vanished between turns; this is the 14-second recipe |
| `verify_tools.sh` | runs `pyflakes`, `--help` and the tool's own `doctor` on all 11 files | the bar after every edit was "pyflakes clean, help works, engine probe honest" |
| `make_pdf_fixtures.py` | writes `doc1.pdf` (3 pages), `doc2.pdf` (2), `big.pdf` (100.1 MiB), `SUMMARY.md`, `print.html` | every PDF claim was made against these five files; `big.pdf` is a real PDF with a 100 MiB incompressible attachment so the size guard has something to trip on |
| `make_dh_stubs.py` | builds three importable `dh` packages: current, old 7-kwarg, `**kwargs` | `dh` is private and absent here; `rmc_ts.py` filters kwargs by signature, so all three shapes have to be exercised |
| `dh_stub.py` | the stand-in `dh` itself: `SKIP_DIRS`, `TSStripper(**kwargs).strip(src)`, `atomic_write` on the real tree-sitter grammar | lets `rmc/rmc_ts.py` and `legacy/rmc/rmc_termux.py` run end-to-end without your module |
| `smoke_pdftool.sh` | every subcommand of one pdftool build, with expected exit codes and artifact checks | the manual sweep, turned into something repeatable |
| `smoke_rmc.sh` | dry-run purity, `--check` gate, `-j 1` == `-j 8`, idempotence, `--backup`, all three `dh` signatures, 96-file timing | same |
| `sync_branch.sh` | saves uncommitted work, `git reset --hard` to the remote tip, restores the work, prints what is dirty | `.git/` reverted to the initial commit between turns; the branch tip lived only on the remote |
| `patches/pdftool_limits.diff` | the `--max-size` / `--max-pages` change on its own | reviewable in isolation from the 7,000-line consolidation |
| `patches/plan_variants.diff` | the `PLAN.md` §9 change on its own | same |
| `parity_g006.py` | rewrites each file twice, once per backend, and `cmp`s the results | the "tree-sitter == libcst" claim, mechanically checkable |

## Using a helper

`setup_venv.sh` and `verify_tools.sh` need nothing from you. The rest assume the
repo root is the working directory:

```bash
cd /path/to/arena
arena/setup_venv.sh
arena/verify_tools.sh
arena/smoke_pdftool.sh --fitz          # builds /tmp/pdft itself if it is missing
arena/smoke_rmc.sh                     # uses arena/dh_stub.py when dh is absent
```

`smoke_rmc.sh` uses your real `dh` when it is importable:

```bash
PYTHONPATH=$HOME/lib arena/smoke_rmc.sh
```

## Two things these helpers changed

Both were found *by* these scripts, after the tools had already been signed off:

1. **`g006/annotations.py`** — `x: str  # note` came out of the tree-sitter
   backend as `x# note`: the span trimmer ate the spaces in front of the comment.
   `_ts_span()` no longer trims trailing whitespace (the spaces belong to the
   line, not to the annotation). Parity is now 104/104 across every parseable
   Python file in the repo — all 11 tools plus 93 of the 96 `legacy/` originals
   (the other three never parsed on 3.11) — not just the original five fixtures.
2. **`rmc/rmc_ts.py`** — it was passing a `strip_all` kwarg that no `dh` revision
   ever documented, and silently ignoring `--remove-blank-lines` when the
   installed `TSStripper` had no such kwarg. It now passes only the seven knobs
   `legacy/rmc/rmc_termux.py` sets, and collapses blank lines itself (with an AST
   equality guard) when the module cannot.

## Known limit, deliberately not papered over

`annotations.py remove` agrees byte for byte on every file in this repo and on
ordinary formatting, but on *unusual* whitespace around `=` the two backends
differ by design:

```python
spaced: int    = 1     # tree-sitter -> "spaced    = 1"   libcst -> "spaced = 1"
tight:  int=2          # tree-sitter -> "tight=2"         libcst -> "tight = 2"
```

tree-sitter deletes the annotation span and nothing else; libcst re-prints the
`=` clause and normalises its spacing. Both results parse; only cosmetics differ.
The `parity_g006.py` fixture set avoids that shape on purpose, and this paragraph
is why.
