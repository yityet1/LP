#!/usr/bin/env bash
set -euo pipefail

REMOTE_URL="${1:-https://github.com/yityet1/LP.git}"
BRANCH="${2:-main}"

if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$REMOTE_URL"
else
  git remote add origin "$REMOTE_URL"
fi

echo "Pushing arb-platform (Sprint 0 - Sprint 6, 28 Issues, 30 Tests Passing) to $REMOTE_URL ($BRANCH)..."
git push -u origin "$BRANCH"
echo "Push completed!"
