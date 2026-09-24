# prune_oleans.py (copied verbatim into every shard - not templated)
# Runs inside the Docker build, after `lake exe cache get` + `lake build
# ShardImports` have populated .lake/packages/mathlib/.lake/build/lib.
# mathlib's cache tool downloads the FULL library regardless of what this
# shard imports (confirmed: lake update's post-update hook forces a full
# `cache get`), so this step deletes every compiled module outside this
# shard's real transitive import closure - computed from mathlib's own
# checked-out .lean source, not guessed.
import glob
import os
import re
import sys

MATHLIB_SRC = ".lake/packages/mathlib/Mathlib"
BUILD_LIB = ".lake/packages/mathlib/.lake/build/lib"
IMPORT_RE = re.compile(r"^\s*(?:public\s+|private\s+|meta\s+)*import\s+(Mathlib(?:\.[A-Za-z0-9_']+)*)", re.MULTILINE)


def parse_edges():
    edges = {}
    for path in glob.glob(f"{MATHLIB_SRC}/**/*.lean", recursive=True):
        rel = os.path.relpath(path, MATHLIB_SRC)
        mod = "Mathlib." + rel[: -len(".lean")].replace(os.sep, ".").replace("/", ".")
        with open(path, encoding="utf-8", errors="ignore") as f:
            edges[mod] = IMPORT_RE.findall(f.read())
    return edges


def closure(seeds, edges):
    seen, stack = set(), list(seeds)
    while stack:
        m = stack.pop()
        if m in seen:
            continue
        seen.add(m)
        stack.extend(d for d in edges.get(m, []) if d not in seen)
    return seen


def main():
    with open("shard_modules.txt", encoding="utf-8") as f:
        seeds = [l.strip() for l in f if l.strip()]

    edges = parse_edges()
    needed = closure(seeds, edges)
    print(f"shard needs {len(needed)}/{len(edges)} mathlib modules ({100*len(needed)/max(len(edges),1):.1f}%)")

    kept = deleted = freed_bytes = 0
    for path in glob.glob(f"{BUILD_LIB}/Mathlib/**/*.olean", recursive=True):
        rel = os.path.relpath(path, f"{BUILD_LIB}")
        mod = rel[: -len(".olean")].replace(os.sep, ".").replace("/", ".")
        if mod in needed:
            kept += 1
            continue
        for ext in (".olean", ".ilean"):
            p = path[: -len(".olean")] + ext
            if os.path.exists(p):
                freed_bytes += os.path.getsize(p)
                os.remove(p)
        deleted += 1

    print(f"pruned {deleted} unneeded modules, kept {kept}, freed {freed_bytes / 1e6:.0f} MB")
    if kept == 0:
        sys.exit("no oleans matched this shard's closure - path layout assumption is wrong, aborting build")


if __name__ == "__main__":
    main()
