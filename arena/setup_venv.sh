#!/usr/bin/env bash
# arena/setup_venv.sh — rebuild the throwaway virtualenv used to test the tools.
#
# The sandbox that produced this repo drops .venv/ between turns (it is in the
# snapshot exclusion list, together with .git/).  This is the exact recipe that
# put it back in ~14 s.  Nothing here is needed to *use* the tools: every one of
# them treats third-party imports as optional and reports what is missing from
# its `doctor` command.  This is only for running the test sweeps.
#
#   arena/setup_venv.sh              # full: pymupdf pypdf libcst tree-sitter pyflakes
#   arena/setup_venv.sh --minimal    # only pymupdf + pyflakes (~9 s)
#   PYTHON=python3.12 arena/setup_venv.sh
#   VENV=/tmp/venv  arena/setup_venv.sh
#
# Exit codes: 0 ok · 1 pip/venv failure · 2 bad usage

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VENV="${VENV:-$ROOT/.venv}"
PYTHON="${PYTHON:-python3}"
MINIMAL=0

case "${1:-}" in
    --minimal) MINIMAL=1 ;;
    -h|--help) sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    "") ;;
    *) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
esac

command -v "$PYTHON" >/dev/null 2>&1 || { echo "no such interpreter: $PYTHON" >&2; exit 1; }

# Everything is optional at runtime; this set is what the sweeps exercise.
#   pymupdf            -> g002/pdftool.py, g002/pdftool_fitz.py
#   pypdf              -> g002/pdftool.py fallback path ("pypdf-only" test)
#   libcst             -> g006/annotations.py, g001/comments.py, rmc/rmc.py backends
#   tree_sitter[_python]-> the tree-sitter backends everywhere + the dh stand-in
#   pyflakes           -> the static check every file in this repo passes
CORE=(pymupdf pypdf pyflakes)
EXTRA=(libcst tree_sitter tree_sitter_python)

say() { printf '  %s\n' "$*"; }

printf 'interpreter  %s (%s)\n' "$("$PYTHON" -V 2>&1)" "$PYTHON"
printf 'venv         %s\n' "$VENV"

if [ ! -x "$VENV/bin/python" ]; then
    say "creating $VENV"
    "$PYTHON" -m venv "$VENV" || { echo "venv creation failed" >&2; exit 1; }
else
    say "reusing existing $VENV"
fi

PKGS=("${CORE[@]}")
[ "$MINIMAL" -eq 1 ] || PKGS+=("${EXTRA[@]}")

say "installing: ${PKGS[*]}"
"$VENV/bin/pip" install --quiet --disable-pip-version-check "${PKGS[@]}" || {
    echo "pip install failed (offline?) — the tools still run, with fewer engines" >&2
    exit 1
}

printf '\ninstalled:\n'
"$VENV/bin/pip" list --disable-pip-version-check 2>/dev/null \
    | grep -Ei '^(pymupdf|pypdf|libcst|tree[-_]sitter|pyflakes) ' \
    | sed 's/^/  /' || true

printf '\nnext:\n  %s g002/pdftool_fitz.py doctor\n  %s/g002/pdftool.py doctor\n' \
    "$VENV/bin/python" "$ROOT"
printf '  arena/verify_tools.sh          # static + --help sweep of all 11 files\n'
