#!/usr/bin/env bash
# arena/verify_tools.sh — the static sweep I ran after every edit.
#
# For each consolidated file: pyflakes (zero warnings is the bar), then `--help`,
# then the tool's own dependency probe (`doctor`).  A missing optional dependency
# is *reported*, not failed — these tools are supposed to degrade gracefully.
#
#   arena/verify_tools.sh              # everything, using .venv if present
#   arena/verify_tools.sh --quick      # --help only (no pyflakes, no doctor)
#   PYTHON=python3.12 arena/verify_tools.sh
#   arena/verify_tools.sh g006/annotations.py rmc/rmc_ts.py    # just these
#
# Exit codes: 0 all green · 1 at least one hard failure · 2 bad usage

set -uo pipefail   # NOT -e: one red line must not hide the rest of the table

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
PYQUICK=0
FILES=()

for arg in "$@"; do
    case "$arg" in
        --quick) PYQUICK=1 ;;
        -h|--help) sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "unknown option: $arg" >&2; exit 2 ;;
        *) FILES+=("$arg") ;;
    esac
done

if [ -x "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"
else
    PY="${PYTHON:-python3}"
    echo "note: no .venv — using $PY; run arena/setup_venv.sh for the full sweep" >&2
fi
cd "$ROOT"

# file | how to ask it about its engines ("-" = it has no probe)
TABLE=(
    "g001/comments.py|--doctor"
    "g002/pdftool.py|doctor"
    "g002/pdftool_fitz.py|doctor"
    "g003/pydup.py|--doctor"
    "g004/extract.py|-"
    "g005/codecs.py|doctor"
    "g005/archive.py|doctor"
    "g006/annotations.py|doctor"
    "rmc/rmc.py|doctor"
    "rmc/deadcode.py|doctor"
    "rmc/rmc_ts.py|-"
)

[ "${#FILES[@]}" -gt 0 ] && TABLE=()
if [ "${#FILES[@]}" -gt 0 ]; then
    for f in "${FILES[@]}"; do TABLE+=("$f|-"); done
fi

HAVE_FLAKES=0
[ "$PYQUICK" -eq 0 ] && "$PY" -m pyflakes --version >/dev/null 2>&1 && HAVE_FLAKES=1

pass=0; fail=0; failed_names=()

for row in "${TABLE[@]}"; do
    file="${row%%|*}"
    probe="${row##*|}"
    notes=()

    if [ ! -f "$file" ]; then
        printf '%-24s MISSING\n' "$file"; fail=$((fail+1)); failed_names+=("$file"); continue
    fi

    if [ "$HAVE_FLAKES" -eq 1 ]; then
        if out="$("$PY" -m pyflakes "$file" 2>&1)"; then
            notes+=("pyflakes ok")
        else
            notes+=("pyflakes FAILED: $(printf '%s' "$out" | head -3 | tr '\n' ';')")
            fail=$((fail+1)); failed_names+=("$file")
        fi
    fi

    if "$PY" "$file" --help >/dev/null 2>&1; then
        notes+=("--help ok")
    else
        notes+=("--help FAILED")
        fail=$((fail+1)); failed_names+=("$file")
    fi

    if [ "$PYQUICK" -eq 0 ] && [ "$probe" != "-" ]; then
        if doc="$("$PY" "$file" "$probe" 2>&1)"; then
            notes+=("$probe ok")
        else
            # exit 2 from a doctor usually means "engine not installed", which is legal
            notes+=("$probe exit $? (engine missing?)")
        fi
        FIRST="$(printf '%s' "$doc" | grep -Eiv '^$' | sed -n '1p')"
        [ -n "$FIRST" ] && notes+=("$FIRST")
    fi

    if printf '%s' "${notes[@]}" | grep -q "FAILED"; then
        printf '%-24s \033[31mFAIL\033[0m  %s\n' "$file" "$(IFS=' · '; echo "${notes[*]}")"
    else
        printf '%-24s \033[32mok\033[0m    %s\n' "$file" "$(IFS=' · '; echo "${notes[*]}")"
        pass=$((pass+1))
    fi
done

printf '\n%d file(s) · %d green · %d with failures\n' "$((pass+fail))" "$pass" "$fail"
if [ "$fail" -gt 0 ]; then
    printf 'failed: %s\n' "${failed_names[*]}" >&2
    exit 1
fi
exit 0
