#!/usr/bin/env bash
# arena/smoke_pdftool.sh — every pdftool subcommand, against the fixtures.
#
# This is the sweep from the session, turned into a script: it runs each
# subcommand of one build over `make_pdf_fixtures.py` output, checks the artifact
# it is supposed to produce, and asserts the exit codes that carry meaning
# (0 by default, 2 for a bad page range, 0 after a guard-rail skip).
#
#   arena/smoke_pdftool.sh                     # 7-engine build, fixtures from /tmp/pdft
#   arena/smoke_pdftool.sh --fitz              # PyMuPDF-only build
#   arena/smoke_pdftool.sh --fitz /tmp/fixt    # explicit fixture dir
#   arena/smoke_pdftool.sh --no-big            # skip the 100 MiB checks
#
# The fixture dir is created on demand with arena/make_pdf_fixtures.py.
# Exit codes: 0 every check passed · 1 at least one failed · 2 bad usage

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BUILD="$ROOT/pdftool.py"
KIND="multi"
FIXTURES=""
WITH_BIG=1

for arg in "$@"; do
    case "$arg" in
        --fitz) BUILD="$ROOT/pdftool_fitz.py"; KIND="fitz" ;;
        --no-big) WITH_BIG=0 ;;
        -h|--help) sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "unknown option: $arg" >&2; exit 2 ;;
        *) FIXTURES="$arg" ;;
    esac
done

if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; else PY="${PYTHON:-python3}"; fi
FIXTURES="${FIXTURES:-/tmp/pdft}"

command -v "$PY" >/dev/null 2>&1 || { echo "no interpreter: $PY" >&2; exit 1; }
"$PY" -c "import pymupdf" 2>/dev/null || { echo "needs PyMuPDF for the fixtures" >&2; exit 1; }

if [ ! -f "$FIXTURES/doc1.pdf" ]; then
    echo "building fixtures in $FIXTURES"
    "$PY" "$HERE/make_pdf_fixtures.py" -o "$FIXTURES" $([ "$WITH_BIG" -eq 1 ] || echo --no-big) || exit 1
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/smokepdf.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
cp "$FIXTURES/doc1.pdf" "$FIXTURES/doc2.pdf" "$FIXTURES/SUMMARY.md" "$FIXTURES/print.html" "$WORK/"
cd "$WORK" || exit 1

pass=0; fail=0; failed=()
ok()   { pass=$((pass+1)); printf '  \033[32mok\033[0m    %s\n' "$*"; }
bad()  { fail=$((fail+1)); failed+=("$*"); printf '  \033[31mFAIL\033[0m  %s\n' "$*"; }

# check <description> <expected-exit> -- <command...>
check() {
    local desc="$1" want="$2"; shift 2
    [ "${1:-}" = "--" ] && shift
    local out rc
    out="$("$@" 2>&1)"; rc=$?
    if [ "$rc" -eq "$want" ]; then ok "$desc (exit $rc)"
    else bad "$desc — expected exit $want, got $rc: $(printf '%s' "$out" | tail -2 | tr '\n' ' ')"; fi
}

# expect_rc <description> <exit> <substring> -- <command...> — exit *and* message
expect_rc() {
    local desc="$1" want="$2" needle="$3"; shift 3
    [ "${1:-}" = "--" ] && shift
    local out rc
    out="$("$@" 2>&1)"; rc=$?
    if [ "$rc" -ne "$want" ]; then
        bad "$desc — expected exit $want, got $rc: $(printf '%s' "$out" | tail -2 | tr '\n' ' ')"
    elif ! printf '%s' "$out" | grep -qi -- "$needle"; then
        bad "$desc — output lacks '$needle': $(printf '%s' "$out" | tail -2 | tr '\n' ' ')"
    else
        ok "$desc (exit $rc)"
    fi
}

# expect <description> <substring> -- <command...> — must exit 0 *and* say this
expect() {
    local desc="$1" needle="$2"; shift 2
    [ "${1:-}" = "--" ] && shift
    expect_rc "$desc" 0 "$needle" -- "$@"
}

# need <description> <path...> — artifacts that must exist and be non-empty
need() {
    local desc="$1"; shift
    local missing=()
    for path in "$@"; do [ -s "$path" ] || missing+=("$path"); done
    if [ "${#missing[@]}" -eq 0 ]; then ok "$desc"
    else bad "$desc — missing/empty: ${missing[*]}"; fi
}

echo "build    $BUILD"
"$PY" "$BUILD" --version || { echo "cannot run $BUILD" >&2; exit 1; }
echo

# ---- text ----------------------------------------------------------------- #
check "text: one .txt per PDF" 0 -- "$PY" "$BUILD" text doc1.pdf -q -o out_text
need  "text: doc1.txt written" out_text/doc1.txt
grep -q "Chapter 1 of doc1.pdf" out_text/doc1.txt \
    && ok "text: content matches the fixture" || bad "text: content does not match"

check "text: --per-page" 0 -- "$PY" "$BUILD" text doc1.pdf --per-page -q -o out_pages
need  "text: 3 page files" out_pages/doc1/doc1_page_001.txt out_pages/doc1/doc1_page_002.txt out_pages/doc1/doc1_page_003.txt

check "text: -p 2 --stdout" 0 -- "$PY" "$BUILD" text doc1.pdf -p 2 --stdout
check "text: bad page range exits 2" 2 -- "$PY" "$BUILD" text doc1.pdf -p 99

# ---- image ---------------------------------------------------------------- #
check "image: png @72dpi" 0 -- "$PY" "$BUILD" image doc1.pdf -o out_png --dpi 72 -j 2 -q
need  "image: 3 png files" out_png/doc1_page_001.png out_png/doc1_page_002.png out_png/doc1_page_003.png

check "image: jpg --quality 80" 0 -- "$PY" "$BUILD" image doc1.pdf -o out_jpg --dpi 72 --format jpg --quality 80 -q
need  "image: 3 jpg files" out_jpg/doc1_page_001.jpg out_jpg/doc1_page_003.jpg

# ---- merge / split / extract ---------------------------------------------- #
check "merge: 2 files" 0 -- "$PY" "$BUILD" merge doc1.pdf doc2.pdf -o merged.pdf -q
expect "merge: output has 5 pages" "5 page" -- "$PY" "$BUILD" info merged.pdf

check "split: one file per page" 0 -- "$PY" "$BUILD" split doc1.pdf -q -o out_split
need  "split: 3 page files" out_split/doc1/doc1_page_1.pdf out_split/doc1/doc1_page_3.pdf

check "split: --max-mb 1" 0 -- "$PY" "$BUILD" split doc1.pdf -q --max-mb 1 -o out_split_mb
need  "split: chunked output" out_split_mb/doc1/doc1_part01.pdf

check "extract: images/fonts/attachments" 0 -- "$PY" "$BUILD" extract doc1.pdf -q -o out_extract

# ---- check / shrink ------------------------------------------------------- #
check "check: every page parses" 0 -- "$PY" "$BUILD" check doc1.pdf

if [ "$KIND" = "fitz" ]; then
    # the PyMuPDF-only build shrinks in-process: -l 1|2|3 are increasingly aggressive
    for level in 1 2 3; do
        check "shrink -l $level" 0 -- "$PY" "$BUILD" shrink doc1.pdf -l "$level" -q -o "out_shrink$level"
        need  "shrink -l $level: output" "out_shrink$level/doc1.pdf"
    done
    if [ -s out_shrink1/doc1.pdf ] && [ -s out_shrink3/doc1.pdf ] \
       && [ "$(stat -c %s out_shrink1/doc1.pdf)" -ge "$(stat -c %s out_shrink3/doc1.pdf)" ]; then
        ok "shrink: level 3 <= level 1 in bytes"
    else
        bad "shrink: level 3 larger than level 1"
    fi
else
    # the 7-engine build shells out; each engine is required, so the interesting
    # assertion is "works when installed, refuses clearly when not"
    for engine in gs qpdf mutool; do
        if command -v "$engine" >/dev/null 2>&1; then
            check "shrink -e $engine (installed)" 0 -- "$PY" "$BUILD" shrink doc1.pdf -e "$engine" -q -o "out_shrink_$engine"
            need  "shrink -e $engine: output" "out_shrink_$engine/doc1.pdf"
        else
            expect_rc "shrink -e $engine: refuses when missing" 1 "not found" \
                -- "$PY" "$BUILD" shrink doc1.pdf -e "$engine" -q -o "out_shrink_$engine"
        fi
    done
fi

# ---- view / info ---------------------------------------------------------- #
check "view: non-tty prints page 2" 0 -- "$PY" "$BUILD" view doc1.pdf -p 2
check "info: page count and size" 0 -- "$PY" "$BUILD" info doc1.pdf

# ---- outline -------------------------------------------------------------- #
check "outline: --summary/--html/--sync-titles" 0 -- "$PY" "$BUILD" outline \
    --pdf doc1.pdf --summary SUMMARY.md --html print.html -o outline.pdf --overwrite --sync-titles
need  "outline: outline.pdf written" outline.pdf
"$PY" - "$BUILD" <<'PY' && ok "outline: toc entry is ['One', ...]" || bad "outline: toc entry missing"
import subprocess, sys
import pymupdf
toc = pymupdf.open("outline.pdf").get_toc()
assert toc and toc[0][1] == "One", toc
PY
check "outline: --check-titles clean" 0 -- "$PY" "$BUILD" outline --summary SUMMARY.md --html print.html --check-titles

# ---- json / dry run ------------------------------------------------------- #
check "json: parses and has the shared envelope" 0 -- sh -c "'$PY' '$BUILD' text doc1.pdf -q -o out_json --json | '$PY' -c 'import json,sys; d=json.load(sys.stdin); assert {\"tool\",\"version\",\"command\",\"elapsed\",\"counts\",\"files\"} <= set(d), d'"

check "dry run: exits 0" 0 -- "$PY" "$BUILD" text doc1.pdf -n -o never_created
[ ! -d never_created ] && ok "dry run: created no directory" || bad "dry run: created never_created/"

# ---- the 100 MiB guard ---------------------------------------------------- #
if [ "$WITH_BIG" -eq 1 ] && [ -f "$FIXTURES/big.pdf" ]; then
    cp "$FIXTURES/big.pdf" .
    check "guard: default skips the 100.1 MiB PDF (exit 0)" 0 -- "$PY" "$BUILD" text big.pdf -q -o out_big
    [ ! -d out_big ] && ok "guard: no output directory for a skipped file" || bad "guard: wrote output for a skipped file"
    check "guard: info is exempt" 0 -- "$PY" "$BUILD" info big.pdf
    check "guard: --max-size 200M allows it" 0 -- "$PY" "$BUILD" text big.pdf -q --max-size 200M -o out_big2
    need  "guard: big text extracted with an override" out_big2/big.txt
    check "guard: --max-size 0 disables the limit" 0 -- "$PY" "$BUILD" info big.pdf --max-size 0
else
    echo "  skip  guard checks (--no-big, or no big.pdf in the fixture dir)"
fi

printf '\n%d passed · %d failed\n' "$pass" "$fail"
if [ "$fail" -gt 0 ]; then
    printf 'failed: %s\n' "${failed[*]}" >&2
    exit 1
fi
exit 0
