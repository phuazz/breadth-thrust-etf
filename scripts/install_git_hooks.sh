#!/bin/sh
# Install the tracked .githooks/pre-commit into .git/hooks/pre-commit.
#
# Git does not track .git/hooks itself, so each clone needs this
# script run once to enable the pre-commit conflict-marker check.
#
# Idempotent: safe to re-run.

set -e

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

SRC=".githooks/pre-commit"
DST=".git/hooks/pre-commit"

if [ ! -f "$SRC" ]; then
    echo "ERROR: $SRC not found. Are you in the repo root?" >&2
    exit 1
fi

mkdir -p "$(dirname "$DST")"
cp "$SRC" "$DST"
chmod +x "$DST"
echo "Installed $SRC -> $DST"
echo "Pre-commit hook will now run scripts/check_conflict_markers.py "
echo "on every commit. Bypass once with: git commit --no-verify"
