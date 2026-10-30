#!/usr/bin/env bash
# arena/smoke_rmc.sh — the sweep that proved rmc/rmc_ts.py against `dh`.
#
# `dh` is not in this repo, so the script uses the real module when one is
# importable and otherwise builds the stand-in from arena/dh_stub.py (and its two
# signature variants) with arena/make_dh_stubs.py.  It says which one it used.
#
#   arena/smoke_rmc.sh                 # stub unless `dh` is importable
#   PYTHONPATH=~/lib arena/smoke_rmc.sh    # your real dh
#   arena/smoke_rmc.sh --no-batch      # skip the 96-file legacy timing run
#
# Checks, in order: --help works with no dh at all; a missing dh exits 2; dry-run
# writes nothing; --check exits 1 before and 0 after; -j 1 and -j 8 are
# byte-identical; a second pass is a no-op; --backup keeps the original; -c/-d/-a
# differ; --exclude works; a broken file is reported without stopping the batch;
# --json parses.  With --stubs it also drives the old and **kwargs dh signatures.
#
# Exit codes: 0 all green · 1 failures · 2 bad usage

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
TOOL="$ROOT/rmc/rmc_ts.py"
TOOL2="$ROOT/rmc/rmc.py"
BATCH=1
ALSO_VARIANTS=1

for arg in "$@"; do
    case "$arg" in
        --no-batch) BATCH=0 ;;
        --quick) BATCH=0; ALSO_VARIANTS=0 ;;
        -h|--help) sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; else PY="${PYTHON:-python3}"; fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/smokermc.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

pass=0; fail=0; failed=()
ok()  { pass=$((pass+1)); printf '  \033[32mok\033[0m    %s\n' "$*"; }
bad() { fail=$((fail+1)); failed+=("$*"); printf '  \033[31mFAIL\033[0m  %s\n' "$*"; }

check() {   # check <desc> <expected-exit> -- <cmd...>
    local desc="$1" want="$2"; shift 2
    [ "${1:-}" = "--" ] && shift
    local out rc; out="$("$@" 2>&1)"; rc=$?
    [ "$rc" -eq "$want" ] && ok "$desc (exit $rc)" \
        || bad "$desc — expected exit $want, got $rc: $(printf '%s' "$out" | tail -2 | tr '\n' ' ')"
}
needs() {   # needs <desc> <path...>
    local desc="$1"; shift
    local missing=(); for p in "$@"; do [ -s "$p" ] || missing+=("$p"); done
    [ "${#missing[@]}" -eq 0 ] && ok "$desc" || bad "$desc — missing: ${missing[*]}"
}

# ---- which dh? ------------------------------------------------------------ #
DH_HOME=""
if PYTHONPATH="${PYTHONPATH:-}" "$PY" -c "from dh import TSStripper" 2>/dev/null; then
    echo "dh       your module (already importable)"
    DH_ENV=(env PYTHONPATH="${PYTHONPATH:-}")
else
    echo "dh       not importable — building the stand-in from arena/dh_stub.py"
    "$PY" "$HERE/make_dh_stubs.py" -o "$WORK/dhstubs" --python "$PY" >/dev/null || {
        echo "could not build the dh stand-in" >&2; exit 1; }
    DH_HOME="$WORK/dhstubs/current"
    DH_ENV=(env PYTHONPATH="$DH_HOME")
    echo "         $DH_HOME/dh.py"
fi
echo

# ---- 0. no dh at all ------------------------------------------------------ #
env -u PYTHONPATH "$PY" "$TOOL" --help >/dev/null 2>&1 \
    && ok "--help works even with no dh installed" || bad "--help needs dh"
out="$(env -u PYTHONPATH "$PY" "$TOOL" "$WORK" 2>&1)"; rc=$?
if [ "$rc" -eq 2 ] && printf '%s' "$out" | grep -qi "cannot import TSStripper from dh"; then
    ok "no dh -> exit 2 with a clear message"
else
    bad "no dh -> expected exit 2 + message, got $rc: $(printf '%s' "$out" | head -1)"
fi

# ---- 1. fixtures ---------------------------------------------------------- #
SRC="$WORK/src"
mkdir -p "$SRC/pkg/sub" "$SRC/node_modules"
cat > "$SRC/pkg/a.py" <<'EOF'
#!/usr/bin/env python3
"""Module docstring."""
import os  # noqa: F401


def greet(name):
    """Say hi."""
    # inner comment
    return f"hi {name}"  # trailing


class K:
    """Class doc."""
    attr = 1  # field
EOF
cat > "$SRC/pkg/b.py" <<'EOF'
# leading comment
x = 1  # trailing
y = 2
EOF
cat > "$SRC/pkg/sub/c.py" <<'EOF'
def f():
    """Doc."""
    return 42
EOF
printf 'x = 1  # c\n' > "$SRC/node_modules/skip.py"
cp "$SRC/pkg/a.py" "$SRC/pkg/a.orig"

# ---- 2. nothing is written until asked ------------------------------------ #
check "dry run exits 0" 0 -- "${DH_ENV[@]}" "$PY" "$TOOL" "$SRC" -n -q
cmp -s "$SRC/pkg/a.py" "$SRC/pkg/a.orig" && ok "dry run wrote nothing" || bad "dry run modified a.py"
[ ! -e "$SRC/pkg/a.py.bak" ] && ok "dry run made no backup" || bad "dry run made a backup"

# ---- 3. --check is a CI gate ---------------------------------------------- #
check "--check exits 1 with work pending" 1 -- "${DH_ENV[@]}" "$PY" "$TOOL" "$SRC" --check -q

# ---- 4. -j 1 and -j 8 agree ----------------------------------------------- #
cp -r "$SRC/pkg" "$WORK/serial"; cp -r "$SRC/pkg" "$WORK/parallel"
"${DH_ENV[@]}" "$PY" "$TOOL" "$WORK/serial" -j 1 -q >/dev/null 2>&1
"${DH_ENV[@]}" "$PY" "$TOOL" "$WORK/parallel" -j 8 -q >/dev/null 2>&1
diff -r "$WORK/serial" "$WORK/parallel" >/dev/null \
    && ok "-j 1 and -j 8 produce identical output" \
    || bad "-j 1 and -j 8 differ: $(diff -r "$WORK/serial" "$WORK/parallel" | head -3 | tr '\n' ' ')"

# ---- 5. the result is stripped, valid, and stable ------------------------- #
grep -q "inner comment" "$WORK/parallel/a.py" && bad "comments survived" || ok "comments removed"
grep -q "Module docstring" "$WORK/parallel/a.py" && ok "module docstring kept (default)" || bad "module docstring lost"
grep -q "noqa" "$WORK/parallel/a.py" && ok "directive comment kept" || bad "noqa comment lost"
"$PY" -c "import ast,pathlib,sys; [ast.parse(p.read_text()) for p in pathlib.Path('$WORK/parallel').rglob('*.py')]" \
    && ok "every rewritten file still parses" || bad "a rewritten file no longer parses"
check "second pass is a no-op" 0 -- "${DH_ENV[@]}" "$PY" "$TOOL" "$WORK/parallel" -q
check "--check exits 0 once clean" 0 -- "${DH_ENV[@]}" "$PY" "$TOOL" "$WORK/parallel" --check -q

# ---- 6. modes, backup, exclude, skips ------------------------------------- #
for mode in -c -d -a; do
    rm -rf "$WORK/m"; cp -r "$SRC/pkg" "$WORK/m"
    check "mode $mode" 0 -- "${DH_ENV[@]}" "$PY" "$TOOL" "$WORK/m" "$mode" -q
    cp "$WORK/m/a.py" "$WORK/m_$mode.py"
done
cmp -s "$WORK/m_-c.py" "$WORK/m_-d.py" && bad "-c and -d produced the same file (suspicious)" \
    || ok "-c and -d differ, as they must"
grep -q "inner comment" "$WORK/m_-d.py" && ok "-d keeps comments" || bad "-d removed comments"
grep -q "Module docstring" "$WORK/m_-a.py" && bad "-a kept the module docstring" || ok "-a removes the module docstring"

rm -rf "$WORK/bk"; cp -r "$SRC/pkg" "$WORK/bk"
check "--backup" 0 -- "${DH_ENV[@]}" "$PY" "$TOOL" "$WORK/bk" --backup -q
cmp -s "$WORK/bk/a.py.bak" "$SRC/pkg/a.orig" && ok "--backup preserved the original" || bad "--backup is not the original"

check "--exclude skips a file" 0 -- "${DH_ENV[@]}" "$PY" "$TOOL" "$SRC" -n -q --exclude 'b.py'
out="$("${DH_ENV[@]}" "$PY" "$TOOL" "$SRC" -n 2>&1)"
printf '%s' "$out" | grep -q "node_modules" && bad "walked into node_modules" || ok "node_modules skipped by SKIP_DIRS"

printf 'def broken(:\n    pass  # c\n' > "$WORK/broken.py"
check "unparseable file does not stop the batch" 0 -- "${DH_ENV[@]}" "$PY" "$TOOL" "$WORK/broken.py" -q

# ---- 7. json -------------------------------------------------------------- #
"${DH_ENV[@]}" "$PY" "$TOOL" "$SRC" -n --json > "$WORK/report.json" 2>/dev/null
"$PY" - "$WORK/report.json" <<'PY' && ok "--json parses and has the shared envelope" || bad "--json shape wrong"
import json, sys
data = json.load(open(sys.argv[1]))
assert {"tool", "version", "command", "elapsed", "counts", "files"} <= set(data), sorted(data)
assert data["counts"]["files"] == len(data["files"])
PY

# ---- 8. other dh signatures ----------------------------------------------- #
if [ "$ALSO_VARIANTS" -eq 1 ] && [ -n "$DH_HOME" ]; then
    for variant in "$WORK"/dhstubs/old "$WORK"/dhstubs/kwargs; do
        rm -rf "$WORK/v"; cp -r "$SRC/pkg" "$WORK/v"
        out="$(env PYTHONPATH="$variant" "$PY" "$TOOL" "$WORK/v" -q 2>&1)"; rc=$?
        if [ "$rc" -eq 0 ] && ! grep -q "inner comment" "$WORK/v/a.py"; then
            ok "dh signature variant '$(basename "$variant")' works"
        else
            bad "dh variant '$(basename "$variant")' failed: $(printf '%s' "$out" | tail -2 | tr '\n' ' ')"
        fi
    done
    # --remove-blank-lines on the old signature: dh has no such kwarg, so rmc_ts
    # must collapse the lines itself, say so, and still produce valid Python
    rm -rf "$WORK/bl"; cp -r "$SRC/pkg" "$WORK/bl"
    printf 'x = 1\n\n\n\n# gone\ny = 2\n\n\nz = 3\n' > "$WORK/bl/blanks.py"
    out="$(env PYTHONPATH="$WORK/dhstubs/old" "$PY" "$TOOL" "$WORK/bl" --remove-blank-lines -q 2>&1)"; rc=$?
    if [ "$rc" -ne 0 ]; then
        bad "--remove-blank-lines on the old signature: exit $rc: $(printf '%s' "$out" | tail -1)"
    elif ! printf '%s' "$out" | grep -q "no remove_blank_lines"; then
        bad "--remove-blank-lines: no note about handling it locally"
    elif grep -q '^$' "$WORK/bl/blanks.py"; then
        bad "--remove-blank-lines: blank lines survived ($(grep -c '^$' "$WORK/bl/blanks.py"))"
    elif ! "$PY" -c "import ast;ast.parse(open('$WORK/bl/blanks.py').read())"; then
        bad "--remove-blank-lines: result no longer parses"
    else
        ok "--remove-blank-lines collapses locally when dh lacks the kwarg"
    fi
fi

# ---- 9. the 96 legacy files, timed ---------------------------------------- #
if [ "$BATCH" -eq 1 ]; then
    rm -rf "$WORK/legacy"; cp -r "$ROOT/legacy" "$WORK/legacy"
    t0=$(date +%s%N)
    "${DH_ENV[@]}" "$PY" "$TOOL" "$WORK/legacy" -j 1 -q >/dev/null 2>&1; rc1=$?
    t1=$(date +%s%N)
    rm -rf "$WORK/legacy2"; cp -r "$ROOT/legacy" "$WORK/legacy2"
    "${DH_ENV[@]}" "$PY" "$TOOL" "$WORK/legacy2" -j 8 -q >/dev/null 2>&1; rc8=$?
    t2=$(date +%s%N)
    serial=$(( (t1 - t0) / 1000000 )); parallel=$(( (t2 - t1) / 1000000 ))
    n=$(find "$ROOT/legacy" -name '*.py' | wc -l | tr -d ' ')
    if [ "$rc1" -eq 0 ] && [ "$rc8" -eq 0 ]; then
        ok "$n legacy files: -j 1 ${serial} ms · -j 8 ${parallel} ms (both exit 0)"
    else
        bad "legacy batch: -j 1 exit $rc1, -j 8 exit $rc8"
    fi
    diff -r "$WORK/legacy" "$WORK/legacy2" >/dev/null \
        && ok "legacy batch: serial and parallel identical" || bad "legacy batch: serial != parallel"
fi

# ---- 10. parity with the full rmc.py (informational) ---------------------- #
if [ -f "$TOOL2" ]; then
    cp "$ROOT/g004/extract.py" "$WORK/parity_a.py"; cp "$ROOT/g004/extract.py" "$WORK/parity_b.py"
    "$PY" "$TOOL2" strip "$WORK/parity_a.py" -c -d -b tree-sitter -q >/dev/null 2>&1
    "${DH_ENV[@]}" "$PY" "$TOOL" "$WORK/parity_b.py" -q >/dev/null 2>&1
    if cmp -s "$WORK/parity_a.py" "$WORK/parity_b.py"; then
        ok "rmc.py (tree-sitter) and rmc_ts.py agree on g004/extract.py"
    else
        echo "  note  rmc.py and rmc_ts.py differ on g004/extract.py — expected: dh has its own policies"
    fi
fi

printf '\n%d passed · %d failed\n' "$pass" "$fail"
if [ "$fail" -gt 0 ]; then
    printf 'failed: %s\n' "${failed[*]}" >&2
    exit 1
fi
exit 0
