#!/usr/bin/env bash
set -euo pipefail

# Produce a VolView checkout at an exact sha.
#
# The baseline backend supplies the client commit through
# girder_volview/web_client/package.json.
#
# Resolution order:
#   1. COMPAT_BASELINE_VOLVIEW — an explicit path or $VOLVIEW_ROOT-relative name,
#      asserted against <sha> (an escape hatch, not the normal path)
#   2. any checkout under $VOLVIEW_ROOT already sitting at <sha> — costs nothing
#      and reuses a warm node_modules
#   3. a detached worktree created under $VOLVIEW_ROOT/.compat-baseline/<sha9>
#
# Usage: materialize-volview-baseline.sh <sha> <reference-checkout>
#   <reference-checkout> is any VolView checkout, used only as the git context
#   for looking up and fetching the sha.
#
#   stdout: absolute path to a checkout whose HEAD is <sha>, and nothing else
#   stderr: progress
#
# Env:
#   COMPAT_BASELINE_VOLVIEW  use this checkout instead of resolving one
#   COMPAT_NO_FETCH=1        skip the git fetch refresh (offline)

die() { echo "materialize-volview-baseline: $*" >&2; exit 1; }

SHA=${1:-}
REF_CHECKOUT=${2:-}
[ -n "$SHA" ] || die "usage: materialize-volview-baseline.sh <sha> <reference-checkout>"
[[ $SHA =~ ^[0-9a-f]{40}$ ]] || die "not a 40-char sha: $SHA"
: "${VOLVIEW_ROOT:?not set — copy .env.example to .env and point VOLVIEW_ROOT at your VolView checkouts}"

head_of() { git -C "$1" rev-parse HEAD 2>/dev/null || true; }

# 1. Validate an explicit checkout override against the required sha.
if [ -n "${COMPAT_BASELINE_VOLVIEW:-}" ]; then
    case "$COMPAT_BASELINE_VOLVIEW" in
        /*) WT=$COMPAT_BASELINE_VOLVIEW ;;
        *)  if [ -d "$COMPAT_BASELINE_VOLVIEW" ]; then
                WT=$(realpath "$COMPAT_BASELINE_VOLVIEW")
            else
                WT="$VOLVIEW_ROOT/$COMPAT_BASELINE_VOLVIEW"
            fi ;;
    esac
    [ -f "$WT/package.json" ] || die "COMPAT_BASELINE_VOLVIEW is not a VolView checkout: $WT"
    ACTUAL=$(head_of "$WT")
    [ "$ACTUAL" = "$SHA" ] || die "COMPAT_BASELINE_VOLVIEW ($WT) is at ${ACTUAL:-<not a git checkout>}, but the baseline backend pins $SHA"
    echo "materialize-volview-baseline: using COMPAT_BASELINE_VOLVIEW=$WT (${SHA:0:9})" >&2
    printf '%s\n' "$WT"
    exit 0
fi

# 2. Reuse any checkout already at the required sha.
shopt -s nullglob
for CAND in "$VOLVIEW_ROOT"/*/ "$VOLVIEW_ROOT"/.compat-baseline/*/; do
    [ -f "${CAND}package.json" ] || continue
    if [ "$(head_of "${CAND%/}")" = "$SHA" ]; then
        echo "materialize-volview-baseline: reusing ${CAND%/} (${SHA:0:9})" >&2
        realpath "${CAND%/}"
        exit 0
    fi
done
shopt -u nullglob

# 3. Create a detached worktree using any checkout of the same repository.
if [ -z "$REF_CHECKOUT" ] || { [ ! -d "$REF_CHECKOUT/.git" ] && [ ! -f "$REF_CHECKOUT/.git" ]; }; then
    REF_CHECKOUT=
    shopt -s nullglob
    for CAND in "$VOLVIEW_ROOT"/*/; do
        if [ -f "${CAND}package.json" ] && [ -n "$(head_of "${CAND%/}")" ]; then
            REF_CHECKOUT=${CAND%/}
            break
        fi
    done
    shopt -u nullglob
fi
[ -n "$REF_CHECKOUT" ] || \
    die "no checkout at ${SHA:0:9} under $VOLVIEW_ROOT, and no VolView git checkout there to create one from"

if ! git -C "$REF_CHECKOUT" cat-file -e "$SHA^{commit}" 2>/dev/null; then
    if [ "${COMPAT_NO_FETCH:-0}" != 1 ]; then
        echo "materialize-volview-baseline: ${SHA:0:9} not in the clone, fetching..." >&2
        git -C "$REF_CHECKOUT" fetch --quiet origin 2>/dev/null || true
    fi
    git -C "$REF_CHECKOUT" cat-file -e "$SHA^{commit}" 2>/dev/null || \
        die "the baseline backend pins VolView $SHA, which is not in this clone. Fetch it: git -C $REF_CHECKOUT fetch origin"
fi

DEST="$VOLVIEW_ROOT/.compat-baseline/${SHA:0:9}"

# Key materialized worktrees by sha.
if [ -f "$DEST/package.json" ] && [ "$(head_of "$DEST")" = "$SHA" ]; then
    echo "materialize-volview-baseline: reusing $DEST (${SHA:0:9})" >&2
    printf '%s\n' "$DEST"
    exit 0
fi

# Prune stale registrations before creating the worktree.
git -C "$REF_CHECKOUT" worktree prune
rm -rf "$DEST"
mkdir -p "$(dirname "$DEST")"
echo "materialize-volview-baseline: creating a detached worktree at $DEST (${SHA:0:9})" >&2
echo "materialize-volview-baseline: first use installs node_modules, which is slow; later runs reuse it" >&2
git -C "$REF_CHECKOUT" worktree add --detach --quiet "$DEST" "$SHA"

ACTUAL=$(head_of "$DEST")
[ "$ACTUAL" = "$SHA" ] || die "created worktree is at $ACTUAL, expected $SHA"

printf '%s\n' "$DEST"
