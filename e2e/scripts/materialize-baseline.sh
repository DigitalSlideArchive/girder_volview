#!/usr/bin/env bash
set -euo pipefail

# Recreate the baseline girder_volview tree from git history.
#
# The baseline defaults to the repo's integration branch (`origin/HEAD`) and can
# be fixed to a commit with COMPAT_BASELINE_REF.
#
# `git archive` avoids adding an entry to the shared worktree registry. The
# export has no .git, so its sha is passed to script/deploy explicitly.
#
# Exports are keyed by sha under gitignored e2e/.compat/; `npm run compat:clean`
# clears them.
#
# Usage: materialize-baseline.sh
#   stdout: the resolved 40-char sha, and nothing else (callers capture it)
#   stderr: progress
#
# Env:
#   COMPAT_BASELINE_REF   resolve this ref instead of the integration branch
#   COMPAT_NO_FETCH=1     skip the `git fetch` refresh (offline)
#   COMPAT_OLD_CHECKOUT   use this git checkout as-is; requires COMPAT_OLD_SHA
#   COMPAT_OLD_SHA        required HEAD of COMPAT_OLD_CHECKOUT

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)

die() { echo "materialize-baseline: $*" >&2; exit 1; }

# Escape hatch for iterating on a baseline that is not a committed ref (the
# PostgreSQL cross-version suite exposes the same seam as `oldinstall`).
if [ -n "${COMPAT_OLD_CHECKOUT:-}" ]; then
    [ -n "${COMPAT_OLD_SHA:-}" ] || die "COMPAT_OLD_CHECKOUT requires COMPAT_OLD_SHA"
    [ -f "$COMPAT_OLD_CHECKOUT/setup.py" ] || die "COMPAT_OLD_CHECKOUT is not a girder_volview tree: $COMPAT_OLD_CHECKOUT"
    ACTUAL_SHA=$(git -C "$COMPAT_OLD_CHECKOUT" rev-parse HEAD 2>/dev/null) || \
        die "COMPAT_OLD_CHECKOUT must be a git checkout: $COMPAT_OLD_CHECKOUT"
    EXPECTED_SHA=$(git -C "$COMPAT_OLD_CHECKOUT" rev-parse --verify "$COMPAT_OLD_SHA^{commit}" 2>/dev/null) || \
        die "COMPAT_OLD_SHA is not a commit in $COMPAT_OLD_CHECKOUT: $COMPAT_OLD_SHA"
    [ "$ACTUAL_SHA" = "$EXPECTED_SHA" ] || \
        die "COMPAT_OLD_CHECKOUT is at $ACTUAL_SHA, expected $EXPECTED_SHA"
    echo "materialize-baseline: using COMPAT_OLD_CHECKOUT=$COMPAT_OLD_CHECKOUT (${ACTUAL_SHA:0:9})" >&2
    echo "$ACTUAL_SHA"
    exit 0
fi

# Resolve the integration branch from origin/HEAD, falling back to origin/main.
default_ref() {
    local head
    if head=$(git -C "$REPO" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null); then
        printf '%s\n' "$head"
    else
        echo "materialize-baseline: origin/HEAD is unset in this clone; assuming origin/main." >&2
        echo "materialize-baseline: record it once with \`git remote set-head origin -a\`." >&2
        printf 'origin/main\n'
    fi
}

REF=${COMPAT_BASELINE_REF:-$(default_ref)}

if [ "${COMPAT_NO_FETCH:-0}" != 1 ]; then
    FETCH_FAILED=
    case "$REF" in
        origin/*) git -C "$REPO" fetch --quiet origin "${REF#origin/}" 2>/dev/null || FETCH_FAILED=1 ;;
        *)        git -C "$REPO" fetch --quiet origin 2>/dev/null || FETCH_FAILED=1 ;;
    esac
    if [ -n "$FETCH_FAILED" ]; then
        echo "materialize-baseline: fetch failed; falling back to local objects" >&2
    fi
fi

SHA=$(git -C "$REPO" rev-parse --verify "$REF^{commit}" 2>/dev/null) || \
    die "cannot resolve the baseline ref '$REF'. Set COMPAT_BASELINE_REF to an existing ref."

if [ -n "${COMPAT_BASELINE_REF:-}" ]; then
    echo "materialize-baseline: COMPAT_BASELINE_REF=$REF -> ${SHA:0:9}" >&2
else
    echo "materialize-baseline: baseline $REF -> ${SHA:0:9}" >&2
fi

DEST="$REPO/e2e/.compat/checkout-${SHA:0:9}"

# A sha-keyed directory avoids replacing a possibly root-owned mounted export.
if [ -f "$DEST/setup.py" ]; then
    echo "materialize-baseline: reusing $DEST" >&2
else
    rm -rf "$DEST"
    mkdir -p "$DEST"
    git -C "$REPO" archive "$SHA" | tar -x -C "$DEST"
    echo "materialize-baseline: exported ${SHA:0:9} -> $DEST" >&2
fi

# Guard against incomplete exports, including export-ignore changes.
[ -f "$DEST/setup.py" ] && [ -d "$DEST/girder_volview" ] || \
    die "export at $DEST is missing setup.py or girder_volview/ — delete it and retry"

printf '{"resolvedSha":"%s","requestedRef":"%s"}\n' "$SHA" "$REF" > "$DEST/.compat-source.json"

echo "$SHA"
