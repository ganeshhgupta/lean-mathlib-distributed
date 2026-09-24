# router/server.py
# Public entrypoint. Parses the `import Mathlib.*` lines out of a submitted
# Lean snippet, resolves each import to a shard via Neon, and forwards the
# whole snippet to that shard's /check endpoint. Rejects (v1) submissions
# whose imports span more than one shard - see README "Multi-shard files".
import json
import os
import re
import pathlib

import httpx
import psycopg
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

DATABASE_URL = os.environ["DATABASE_URL"]
IMPORT_RE = re.compile(r"^\s*import\s+(Mathlib(?:\.[A-Za-z0-9_']+)*)\s*$", re.MULTILINE)

# local fallback for modules newer than the last Neon seed (see
# scripts/refresh_mathlib_files.sh + neon/seed.py to keep the DB current)
# NOTE: shards.json is copied alongside server.py in router/Dockerfile,
# so it lives in the same directory inside the image - not one level up.
SHARDS_JSON = json.loads((pathlib.Path(__file__).resolve().parent / "shards.json").read_text())


class ProveRequest(BaseModel):
    source: str
    # Confirmed on Render free tier: even a trivial goal against a single
    # real mathlib import takes ~2m30s (CPU-throttled). Matches the shard
    # default in scripts/templates/server.py.tmpl.
    timeout_seconds: int = 280


def extract_imports(source: str) -> list[str]:
    return IMPORT_RE.findall(source)


def fallback_shard(module: str) -> str | None:
    for shard in SHARDS_JSON["shards"]:
        for ns in shard["namespaces"]:
            if module == ns or module.startswith(ns + "."):
                return shard["id"]
    for ns in SHARDS_JSON["common_namespaces"]:
        if module == ns or module.startswith(ns + "."):
            return "__common_only__"
    return None


def resolve_shards(imports: list[str]) -> tuple[set[str], list[str]]:
    """Returns (candidate shard ids, unresolved import names)."""
    if not imports:
        return set(), []

    with psycopg.connect(DATABASE_URL) as conn:
        rows = conn.execute(
            "SELECT name, shard_id FROM modules WHERE name = ANY(%s)", (imports,)
        ).fetchall()
    found = {name: shard_id for name, shard_id in rows}

    shards: set[str] = set()
    unresolved: list[str] = []
    for m in imports:
        sid = found.get(m) or fallback_shard(m)
        if sid is None:
            unresolved.append(m)
        elif sid != "__common_only__":
            shards.add(sid)
    return shards, unresolved


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/check")
def check(req: ProveRequest):
    imports = extract_imports(req.source)
    if not imports:
        return {"ok": False, "error": "no `import Mathlib.*` lines found (bare `import Mathlib` is not supported by this deployment - see README)"}

    shards, unresolved = resolve_shards(imports)
    if unresolved:
        return {"ok": False, "error": "unresolved imports, not present in any shard", "unresolved": unresolved}
    if len(shards) == 0:
        return {"ok": False, "error": "all imports resolved to the common base only; any shard can serve this - pick one explicitly via /check/{shard_id}"}
    if len(shards) > 1:
        return {
            "ok": False,
            "error": "imports span multiple shards - not supported in v1",
            "shards": sorted(shards),
            "hint": "split the file so all imports resolve to one shard, or see README 'Multi-shard files' for the union-worker option",
        }

    return _forward(next(iter(shards)), req)


@app.post("/check/{shard_id}")
def check_explicit(shard_id: str, req: ProveRequest):
    return _forward(shard_id, req)


def _forward(shard_id: str, req: ProveRequest):
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute("SELECT render_url FROM shards WHERE id = %s", (shard_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": f"unknown shard '{shard_id}'"}
    (url,) = row
    resp = httpx.post(f"{url}/check", json=req.model_dump(), timeout=req.timeout_seconds + 10)
    body = resp.json()
    body["routed_to"] = shard_id
    return body
