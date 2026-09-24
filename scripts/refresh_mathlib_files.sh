#!/usr/bin/env bash
# refresh_mathlib_files.sh
# Re-fetches the real mathlib4 file listing used by generate_shards.py.
# Blobless + shallow: pulls tree metadata only, not file contents, not .olean
# build artifacts. Safe to re-run whenever mathlib4 gains/removes files.
set -euo pipefail

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

git clone --depth 1 --filter=blob:none --sparse \
  https://github.com/leanprover-community/mathlib4.git "$TMPDIR/mathlib4-src"

cd "$TMPDIR/mathlib4-src"
git ls-tree -r --name-only HEAD | grep '^Mathlib/' | grep '\.lean$' \
  > "$OLDPWD/scripts/mathlib_files.txt"

echo "wrote $(wc -l < "$OLDPWD/scripts/mathlib_files.txt") module paths to scripts/mathlib_files.txt"
echo "current mathlib lean-toolchain: $(cat lean-toolchain)"
echo "  (update LEAN_TOOLCHAIN in generate_shards.py if this changed)"
