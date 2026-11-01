#!/usr/bin/env bash
# arena/sync_branch.sh — put this checkout back on the session branch tip.
#
# Written because the sandbox this repo was built in reverts .git/ to the initial
# commit between turns: the branch tip survives only on the remote, while the
# *working tree* stays correct (files are snapshotted, .git/ is not).  Running
# this after such a revert restores the history without touching your work.
#
# What it does, in order:
#   1. git fetch origin <branch>
#   2. save every modified/untracked file to a tarball under $KEEP
#   3. git reset --hard FETCH_HEAD        (only after (2) succeeded)
#   4. unpack the tarball back over the tree, so uncommitted edits survive
#   5. print what moved and what is still dirty
#
#   arena/sync_branch.sh                 # sync the session branch
#   arena/sync_branch.sh --dry-run       # show what would be saved/reset, change nothing
#   BRANCH=main arena/sync_branch.sh     # any other branch
#   KEEP=/tmp/mysave arena/sync_branch.sh
#
# Exit codes: 0 synced or already current · 1 nothing to fetch / reset refused · 2 bad usage

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
BRANCH="${BRANCH:-arena/a00324b3-arena}"
KEEP="${KEEP:-/tmp/keep}"
DRY_RUN=0

case "${1:-}" in
    --dry-run|-n) DRY_RUN=1 ;;
    -h|--help) sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    "") ;;
    *) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
esac

cd "$ROOT"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
    echo "not a git checkout: $ROOT" >&2; exit 1;
}

printf 'repo     %s\nbranch   %s\n' "$ROOT" "$BRANCH"
git fetch --quiet origin "$BRANCH" || { echo "fetch failed (network/auth?)" >&2; exit 1; }
TARGET="$(git rev-parse FETCH_HEAD)"
HEAD_NOW="$(git rev-parse HEAD)"
printf 'local    %s\nremote   %s  %s\n' \
    "$(git log --oneline -1 "$HEAD_NOW" 2>/dev/null || echo "$HEAD_NOW")" \
    "$TARGET" "$(git log --oneline -1 "$TARGET" | cut -d' ' -f2-)"

if [ "$TARGET" = "$HEAD_NOW" ]; then
    echo "already at the branch tip — nothing to do"
    git status --short | sed -n '1,10p'
    exit 0
fi

# ---- 1. save whatever is uncommitted -------------------------------------- #
# -z + core.quotepath=off keeps spaces and unicode in filenames intact.
DIRTY_LIST="$KEEP/dirty.txt"
TARBALL="$KEEP/work.tar"
mkdir -p "$KEEP"
: > "$DIRTY_LIST"

while IFS= read -r -d '' entry; do
    status="${entry:0:2}"
    path="${entry:3}"
    case "$path" in
        .venv/*|*/__pycache__/*|__pycache__/*|.git/*) continue ;;
    esac
    [ "$status" = " D" ] && continue        # deleted files cannot be tarred
    printf '%s\n' "$path" >> "$DIRTY_LIST"
done < <(git -c core.quotepath=off status --porcelain -z)

COUNT=$(wc -l < "$DIRTY_LIST" | tr -d ' ')
echo "uncommitted files: $COUNT"

if [ "$COUNT" -gt 0 ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "would save these to $TARBALL:"; sed 's/^/  /' "$DIRTY_LIST"
    else
        tar -cf "$TARBALL" -C "$ROOT" -T "$DIRTY_LIST"
        echo "saved -> $TARBALL ($(wc -c < "$TARBALL" | tr -d ' ') bytes)"
    fi
fi

[ "$DRY_RUN" -eq 1 ] && { echo "dry run: no reset performed"; exit 0; }

# ---- 2. fast-forward the history ------------------------------------------ #
git reset --hard --quiet "$TARGET"
echo "reset to $(git log --oneline -1)"

# ---- 3. put the uncommitted edits back ------------------------------------ #
if [ "$COUNT" -gt 0 ] && [ -f "$TARBALL" ]; then
    tar -xf "$TARBALL" -C "$ROOT"
    echo "restored $COUNT uncommitted file(s) over the new tip"
fi

printf '\nnow:\n  %s\n' "$(git log --oneline -3 | sed 's/^/  /')"
STATUS="$(git status --short)"
if [ -n "$STATUS" ]; then
    printf '\nstill dirty (review, then commit + push):\n%s\n' "$(printf '%s\n' "$STATUS" | sed -n '1,20p')"
    printf '\n  git add -A && git commit -m "..." && git push origin %s\n' "$BRANCH"
else
    echo "working tree clean"
fi
