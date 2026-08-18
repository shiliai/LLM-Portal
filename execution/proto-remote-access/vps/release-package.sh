#!/usr/bin/env bash
# Build an immutable source package plus SHA-256 inventory from one Git commit.
set -euo pipefail

COMMIT=${1:?usage: release-package.sh <commit> <output-dir>}
OUT=${2:?usage: release-package.sh <commit> <output-dir>}
ROOT=$(git rev-parse --show-toplevel)
COMMIT=$(git -C "$ROOT" rev-parse "${COMMIT}^{commit}")
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

git -C "$ROOT" archive --format=tar "$COMMIT" execution/proto-remote-access > "$OUT/private-llm-$COMMIT.tar"
tar -xf "$OUT/private-llm-$COMMIT.tar" -C "$TMP"
(
  cd "$TMP/execution/proto-remote-access"
  find . -type f -print0 | LC_ALL=C sort -z | xargs -0 shasum -a 256
) > "$OUT/private-llm-$COMMIT.files.sha256"
(cd "$OUT" && shasum -a 256 "private-llm-$COMMIT.tar" \
  > "private-llm-$COMMIT.tar.sha256")
printf '%s\n' "$COMMIT" > "$OUT/private-llm-$COMMIT.commit"
echo "$OUT/private-llm-$COMMIT.tar"
