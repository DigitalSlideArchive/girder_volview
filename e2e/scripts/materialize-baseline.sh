#!/usr/bin/env bash
set -euo pipefail

# Recreate the baseline (old) girder_volview tree from git history.
#
# The compat suite needs to deploy an OLDER version of this plugin so it can
# save sessions the way that version did. The old tree is derived from the repo's
# own history so the suite does not depend on a second checkout existing on the
# developer's machine.
#
# The baseline is the repo's INTEGRATION BRANCH, resolved fresh on every run.
# That is the pairing users actually get, so it is the one worth proving
# backwards-compat against. It is asked of the repo (origin/HEAD) rather than
# spelled out, so a repo that renames its integration branch needs no edit here,
# and no in-progress branch name is ever baked into checked-in tooling.
#
# An earlier version pinned a frozen sha instead, reasoning that a red run should
# be bisectable. The reasoning was sound and the mechanism was not: the pin drifted
# out of agreement with what the plugin actually ships, so the suite proved
# compat against a pairing nobody ran. Bisectability lives in COMPAT_BASELINE_REF
# now — pass a sha to freeze the baseline for as long as you are bisecting.
#
# `git archive` rather than `git worktree add`: the export is ~36 files, it
# takes milliseconds, and — the deciding reason — it leaves no entry in the
# shared .git/worktrees registry. A crashed run therefore cannot strand a
# registration that a later `worktree add` trips over, and the tree can be
# deleted with plain rm once ownership is sane. The cost is that the export has
# no .git, so its sha must be passed to the deploy explicitly; script/deploy
# still proves the mounted backend matches the tree it was handed.
#
# Nothing this produces is ever committed: e2e/.compat/ is gitignored, because a
# checkout reproducible from a sha is not source. Exports are keyed by sha and
# accumulate as the integration branch moves; `npm run compat:clean` clears them.
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

# The integration branch, asked of the repo rather than hardcoded. origin/HEAD is
# what `git remote set-head` records; a clone that never learned it falls back to
# origin/main with a note, because guessing silently is how this drifts.
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

# Keyed on the sha, so the integration branch moving extracts into a virgin
# directory instead of needing the old one removed first — which matters because
# the old one may still be root-owned from a container mount.
if [ -f "$DEST/setup.py" ]; then
    echo "materialize-baseline: reusing $DEST" >&2
else
    rm -rf "$DEST"
    mkdir -p "$DEST"
    git -C "$REPO" archive "$SHA" | tar -x -C "$DEST"
    echo "materialize-baseline: exported ${SHA:0:9} -> $DEST" >&2
fi

# A truncated export would deploy a half-plugin and fail much later as a
# confusing test error. `git archive` honours .gitattributes export-ignore, so
# this also catches someone adding one.
[ -f "$DEST/setup.py" ] && [ -d "$DEST/girder_volview" ] || \
    die "export at $DEST is missing setup.py or girder_volview/ — delete it and retry"

printf '{"resolvedSha":"%s","requestedRef":"%s"}\n' "$SHA" "$REF" > "$DEST/.compat-source.json"

echo "$SHA"
