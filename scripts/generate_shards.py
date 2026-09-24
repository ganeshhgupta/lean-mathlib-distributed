# generate_shards.py
# Reads shards.json + a real mathlib4 file listing (scripts/mathlib_files.txt,
# produced by `git ls-tree -r --name-only HEAD` on a shallow mathlib4 clone,
# no build required) and writes each shard's lakefile.toml, lean-toolchain,
# ShardImports.lean, Dockerfile and server.py from the templates/ directory.
#
# Re-run this after editing shards.json or refreshing mathlib_files.txt.

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SHARDS_JSON = ROOT / "shards.json"
FILES_TXT = ROOT / "scripts" / "mathlib_files.txt"
TEMPLATES = ROOT / "scripts" / "templates"
LEAN_TOOLCHAIN = "leanprover/lean4:v4.35.0-rc2"


def module_name(path: str) -> str:
    return path[: -len(".lean")].replace("/", ".")


def load_files():
    if not FILES_TXT.exists():
        sys.exit(f"missing {FILES_TXT} — run scripts/refresh_mathlib_files.sh first")
    return [module_name(l.strip()) for l in FILES_TXT.read_text(encoding="utf-8").splitlines() if l.strip()]


def matches(mod: str, namespaces: list[str]) -> bool:
    return any(mod == ns or mod.startswith(ns + ".") for ns in namespaces)


def main():
    cfg = json.loads(SHARDS_JSON.read_text(encoding="utf-8"))
    all_mods = load_files()
    common_ns = cfg["common_namespaces"]

    covered = set()
    report = []

    for shard in cfg["shards"]:
        sid = shard["id"]
        ns = common_ns + shard["namespaces"]
        mods = sorted(m for m in all_mods if matches(m, ns))
        covered.update(mods)

        shard_dir = ROOT / "shards" / sid
        shard_dir.mkdir(parents=True, exist_ok=True)

        (shard_dir / "ShardImports.lean").write_text(
            "\n".join(f"import {m}" for m in mods) + "\n", encoding="utf-8"
        )
        (shard_dir / "shard_modules.txt").write_text("\n".join(mods) + "\n", encoding="utf-8")
        (shard_dir / "lean-toolchain").write_text(LEAN_TOOLCHAIN + "\n", encoding="utf-8")
        (shard_dir / "lakefile.toml").write_text(
            (TEMPLATES / "lakefile.toml.tmpl").read_text(encoding="utf-8").replace("{{SHARD_ID}}", sid),
            encoding="utf-8",
        )
        (shard_dir / "Dockerfile").write_text(
            (TEMPLATES / "Dockerfile.tmpl").read_text(encoding="utf-8").replace("{{SHARD_ID}}", sid),
            encoding="utf-8",
        )
        (shard_dir / "server.py").write_text(
            (TEMPLATES / "server.py.tmpl").read_text(encoding="utf-8"), encoding="utf-8"
        )
        (shard_dir / "requirements.txt").write_text("fastapi\nuvicorn[standard]\n", encoding="utf-8")

        report.append((sid, len(mods)))

    # router's Docker build context is scoped to router/ (Render root directory),
    # so it needs its own copy of the shard manifest for the fallback resolver.
    router_copy = ROOT / "router" / "shards.json"
    router_copy.write_text(SHARDS_JSON.read_text(encoding="utf-8"), encoding="utf-8")

    missing = sorted(set(all_mods) - covered)
    total = len(all_mods)

    print(f"{'shard':<16} {'modules':>8}")
    for sid, n in report:
        print(f"{sid:<16} {n:>8}")
    print(f"{'TOTAL (union)':<16} {len(covered):>8}  of {total} real mathlib modules")
    if missing:
        print(f"\nWARNING: {len(missing)} modules not covered by any shard:")
        for m in missing[:20]:
            print(f"  {m}")
        if len(missing) > 20:
            print(f"  ... and {len(missing) - 20} more")
        sys.exit(1)
    else:
        print("\nCoverage OK: every real mathlib module is claimed by exactly the common base + >=1 shard.")


if __name__ == "__main__":
    main()
