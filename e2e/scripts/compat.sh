#!/usr/bin/env bash
set -euo pipefail

# Backwards-compat orchestration:
#
#   1. materialize the BASELINE girder_volview from git history (no second
#      checkout required) and deploy it with the baseline VolView
#   2. playwright `capture` project — real-UI gestures, content, saves
#      (expected backend + client shas carry the pins past the deploy guard)
#   3. redeploy THIS worktree + its paired VolView
#   4. playwright `verify` project — sessions must restore + re-save
#   5. playwright `current` project — fresh current sessions, restart/history,
#      grouped DICOM launches, and job submission, all on isolated folders
#
# The stack itself (a running docker compose project, dsa-plus by default) must
# already exist; script/deploy only swaps the code it serves. Mongo survives the
# redeploy, so the girder folders/sessions captured in step 2 are still there
# for step 4.
#
# The baseline is a `git archive` export under the gitignored e2e/.compat/,
# pinned by e2e/compat-baseline.json. Neither the old sources nor the session
# zips they produce are ever committed — both are reproducible from a sha.
#
# Usage: compat.sh [--phase all|capture|verify|current] [--skip-deploy] [--link] [--keep]
#
#   --phase        which half to run (default all)
#   --skip-deploy  don't deploy (the stack already serves the right code)
#   --link         pass --link to script/deploy (fast client copy; default pack)
#   --keep         keep the run folder + state after verify (iteration)
#
# Env overrides: COMPAT_BASELINE_REF (baseline ref instead of the pin),
# COMPAT_NO_FETCH, COMPAT_OLD_CHECKOUT/COMPAT_OLD_SHA, COMPAT_BRANCH_VOLVIEW,
# COMPAT_BASELINE_VOLVIEW, COMPAT_BRANCH_VOLVIEW_SHA,
# COMPAT_BASELINE_VOLVIEW_SHA, COMPAT_DEPLOY.

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)
E2E="$REPO/e2e"
MANIFEST="$E2E/compat-baseline.json"

BASELINE_VOLVIEW=${COMPAT_BASELINE_VOLVIEW:-main}
BRANCH_VOLVIEW=${COMPAT_BRANCH_VOLVIEW:-just-jobs}
DEPLOY=${COMPAT_DEPLOY:-$REPO/script/deploy}

PHASE=all
SKIP_DEPLOY=0
LINK_FLAG=""
KEEP=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --phase) PHASE=$2; shift 2 ;;
        --skip-deploy) SKIP_DEPLOY=1; shift ;;
        --link) LINK_FLAG=--link; shift ;;
        --keep) KEEP=1; shift ;;
        -h|--help) sed -n '/^# Usage:/,/^$/p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown flag: $1" >&2; exit 2 ;;
    esac
done
case "$PHASE" in all|capture|verify|current) ;; *) echo "--phase must be all|capture|verify|current" >&2; exit 2 ;; esac

die() { echo "compat: $*" >&2; exit 1; }

manifest_sha() {
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]]["sha"])' \
        "$MANIFEST" "$1" || die "could not read $1.sha from $MANIFEST"
}

resolve_volview() {
    local arg=$1
    if [[ $arg = /* ]]; then
        printf '%s\n' "$arg"
    elif [[ -d $arg ]]; then
        realpath "$arg"
    else
        : "${VOLVIEW_ROOT:?set VOLVIEW_ROOT in .env or pass an explicit VolView checkout path}"
        printf '%s/%s\n' "$VOLVIEW_ROOT" "$arg"
    fi
}

require_volview_sha() {
    local checkout=$1 expected=$2 label=$3 actual
    [[ -f $checkout/package.json ]] || die "$label VolView checkout not found: $checkout"
    actual=$(git -C "$checkout" rev-parse HEAD 2>/dev/null) || \
        die "$label VolView path is not a git checkout: $checkout"
    [[ $actual = "$expected" ]] || \
        die "$label VolView is at $actual, but expected $expected ($checkout)"
}

[[ -x $DEPLOY ]] || die "deploy script not found/executable: $DEPLOY (set COMPAT_DEPLOY)"
command -v uv >/dev/null || die "uv is required (seed.py seed-small runs via 'uv run')"
[[ -f $MANIFEST ]] || die "missing $MANIFEST"

BASELINE_VOLVIEW_SHA=${COMPAT_BASELINE_VOLVIEW_SHA:-$(manifest_sha volview)}
[[ $BASELINE_VOLVIEW_SHA =~ ^[0-9a-f]{40}$ ]] || die "invalid baseline VolView sha: $BASELINE_VOLVIEW_SHA"

if [[ -f $REPO/.env ]]; then
    set -a
    # shellcheck disable=SC1091
    . "$REPO/.env"
    set +a
fi

if [[ $SKIP_DEPLOY -eq 0 && ($PHASE == all || $PHASE == capture) ]]; then
    BASELINE_VOLVIEW=$(resolve_volview "$BASELINE_VOLVIEW")
    require_volview_sha "$BASELINE_VOLVIEW" "$BASELINE_VOLVIEW_SHA" baseline
fi
if [[ $PHASE == all || $PHASE == verify || $PHASE == current ]]; then
    BRANCH_VOLVIEW=$(resolve_volview "$BRANCH_VOLVIEW")
    BRANCH_VOLVIEW_HEAD=$(git -C "$BRANCH_VOLVIEW" rev-parse HEAD 2>/dev/null) || \
        die "branch VolView path is not a git checkout: $BRANCH_VOLVIEW"
    BRANCH_VOLVIEW_SHA=${COMPAT_BRANCH_VOLVIEW_SHA:-$BRANCH_VOLVIEW_HEAD}
    [[ $BRANCH_VOLVIEW_SHA =~ ^[0-9a-f]{40}$ ]] || die "invalid branch VolView sha: $BRANCH_VOLVIEW_SHA"
    require_volview_sha "$BRANCH_VOLVIEW" "$BRANCH_VOLVIEW_SHA" branch
fi

# Unconditional, including under --skip-deploy: the capture phase exports
# E2E_EXPECT_GIRDER_SHA either way, and this is a cache hit that needs no docker.
BASELINE_DIR_SHA=$("$E2E/scripts/materialize-baseline.sh")
MAIN_SHA=$BASELINE_DIR_SHA
BASELINE_DIR="$E2E/.compat/checkout-${MAIN_SHA:0:9}"
[[ -n ${COMPAT_OLD_CHECKOUT:-} ]] && BASELINE_DIR=$COMPAT_OLD_CHECKOUT
CUSTOM_BASELINE=0
[[ -n ${COMPAT_OLD_CHECKOUT:-} ]] && CUSTOM_BASELINE=1

BRANCH_SHA=$(git -C "$REPO" rev-parse HEAD)
if [[ $MAIN_SHA == "$BRANCH_SHA" ]]; then
    echo "compat: WARNING — the baseline and this worktree are the same commit; the run is vacuous" >&2
fi

echo "compat: baseline ${MAIN_SHA:0:9} at $BASELINE_DIR (VolView: ${BASELINE_VOLVIEW_SHA:0:9} at $BASELINE_VOLVIEW)"
if [[ $PHASE == all || $PHASE == verify || $PHASE == current ]]; then
    echo "compat: branch   ${BRANCH_SHA:0:9} at $REPO (VolView: ${BRANCH_VOLVIEW_SHA:0:9} at $BRANCH_VOLVIEW)"
fi

run_capture() {
    echo "compat: ensuring the small-tier DICOM cache (fetch --small is idempotent)..."
    uv run "$E2E/seed/seed.py" fetch --small

    if [[ $SKIP_DEPLOY -eq 0 ]]; then
        echo "compat: deploying the baseline..."
        if [[ $CUSTOM_BASELINE -eq 1 ]]; then
            # A custom baseline is a real git checkout whose HEAD was verified
            # by materialize-baseline.sh, so deploy derives its receipt normally.
            "$DEPLOY" $LINK_FLAG -- "$BASELINE_DIR" "$BASELINE_VOLVIEW"
        else
            # The normal export is a plain tree with no .git to ask.
            "$DEPLOY" $LINK_FLAG --girder-sha "$MAIN_SHA" -- "$BASELINE_DIR" "$BASELINE_VOLVIEW"
        fi
    fi
    echo "compat: running capture specs against the baseline (${MAIN_SHA:0:9})..."
    (
        cd "$E2E"
        COMPAT_PHASE=capture E2E_EXPECT_GIRDER_SHA=$MAIN_SHA \
            E2E_EXPECT_VOLVIEW_SHA=$BASELINE_VOLVIEW_SHA \
            npx playwright test --config playwright.config.ts --project capture
    )
}

run_verify() {
    if [[ $SKIP_DEPLOY -eq 0 ]]; then
        echo "compat: deploying THIS worktree..."
        "$DEPLOY" $LINK_FLAG "$REPO" "$BRANCH_VOLVIEW"
    fi
    echo "compat: running verify specs against this worktree (${BRANCH_SHA:0:9})..."
    (
        cd "$E2E"
        if [[ $KEEP -eq 1 ]]; then export COMPAT_KEEP=1; fi
        if [[ $PHASE == verify ]]; then export COMPAT_CLEANUP=1; fi
        COMPAT_PHASE=verify E2E_EXPECT_GIRDER_SHA=$BRANCH_SHA \
            E2E_EXPECT_VOLVIEW_SHA=$BRANCH_VOLVIEW_SHA \
            npx playwright test --config playwright.config.ts --project verify
    )
}

run_current() {
    echo "compat: running current-version lifecycle and job scenarios (${BRANCH_SHA:0:9})..."
    (
        cd "$E2E"
        if [[ $KEEP -eq 1 ]]; then export COMPAT_KEEP=1; fi
        COMPAT_PHASE=current COMPAT_CLEANUP=1 E2E_EXPECT_GIRDER_SHA=$BRANCH_SHA \
            E2E_EXPECT_VOLVIEW_SHA=$BRANCH_VOLVIEW_SHA \
            npx playwright test --config playwright.config.ts --project current
    )
}

if [[ $PHASE == all || $PHASE == capture ]]; then run_capture; fi
if [[ $PHASE == all || $PHASE == verify ]]; then run_verify; fi
if [[ $PHASE == all || $PHASE == current ]]; then run_current; fi

echo "compat: done — source sessions verified and current lifecycles exercised on ${BRANCH_SHA:0:9}."
echo "compat: report: cd e2e && npm run report"
