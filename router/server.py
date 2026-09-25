# router/server.py
# Public entrypoint. Parses the `import Mathlib.*` lines out of a submitted
# Lean snippet, resolves each import to a shard via Neon, and forwards the
# whole snippet to that shard's /check (or /submit) endpoint. Rejects (v1)
# submissions whose imports span more than one shard - see README
# "Multi-shard files".
#
# /check is synchronous and shares the ~300-350s hard ceiling Render's own
# edge enforces on any request through it (confirmed non-configurable).
# /submit + /result proxy to the shard's own background-job endpoints,
# where the actual long-running `lean` subprocess never crosses that edge
# at all - only the fast submit/poll calls do. Use /submit for anything
# that might not finish in well under 5 minutes.
import json
import os
import re
import pathlib

import httpx
import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()

DATABASE_URL = os.environ["DATABASE_URL"]
IMPORT_RE = re.compile(r"^\s*import\s+(Mathlib(?:\.[A-Za-z0-9_']+)*)\s*$", re.MULTILINE)

# local fallback for modules newer than the last Neon seed (see
# scripts/refresh_mathlib_files.sh + neon/seed.py to keep the DB current)
# NOTE: shards.json is copied alongside server.py in router/Dockerfile,
# so it lives in the same directory inside the image - not one level up.
SHARDS_JSON = json.loads((pathlib.Path(__file__).resolve().parent / "shards.json").read_text())

# Picked for a common-only file, where any shard would do: empirically the
# fastest/most consistent shard in testing (~70-150s for a trivial goal,
# vs. ~280-295s for categorytheory).
DEFAULT_COMMON_SHARD = "numbertheory"


class ProveRequest(BaseModel):
    source: str
    # Matches the shard default in scripts/templates/server.py.tmpl - see
    # that file's comment for the confirmed platform ceiling (~300-350s,
    # enforced upstream regardless of this value) /check stays under.
    # Ignored by /submit, which always gives the shard a generous window
    # since the wait happens via polling, not a held-open request.
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


def resolve_target_shard(source: str) -> tuple[str | None, dict | None]:
    """Returns (shard_id, None) on success, or (None, error_body) on failure."""
    imports = extract_imports(source)
    if not imports:
        return None, {"ok": False, "error": "no `import Mathlib.*` lines found (bare `import Mathlib` is not supported by this deployment - see README)"}

    shards, unresolved = resolve_shards(imports)
    if unresolved:
        return None, {"ok": False, "error": "unresolved imports, not present in any shard", "unresolved": unresolved}
    if len(shards) == 0:
        # Common-only file (e.g. plain Nat/Data basics) - every shard
        # carries the common base, so any of them can serve it.
        return DEFAULT_COMMON_SHARD, None
    if len(shards) > 1:
        return None, {
            "ok": False,
            "error": "imports span multiple shards - not supported in v1",
            "shards": sorted(shards),
            "hint": "split the file so all imports resolve to one shard, or see README 'Multi-shard files' for the union-worker option",
        }
    return next(iter(shards)), None


def shard_url(shard_id: str) -> str | None:
    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute("SELECT render_url FROM shards WHERE id = %s", (shard_id,)).fetchone()
    return row[0] if row else None


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/check")
def check(req: ProveRequest):
    shard_id, err = resolve_target_shard(req.source)
    if err:
        return err
    return _forward_check(shard_id, req)


@app.post("/check/{shard_id}")
def check_explicit(shard_id: str, req: ProveRequest):
    return _forward_check(shard_id, req)


@app.post("/submit")
def submit(req: ProveRequest):
    shard_id, err = resolve_target_shard(req.source)
    if err:
        return err
    return _forward_submit(shard_id, req)


@app.post("/submit/{shard_id}")
def submit_explicit(shard_id: str, req: ProveRequest):
    return _forward_submit(shard_id, req)


@app.get("/result/{shard_id}/{job_id}")
def result(shard_id: str, job_id: str):
    url = shard_url(shard_id)
    if url is None:
        raise HTTPException(404, f"unknown shard '{shard_id}'")
    try:
        resp = httpx.get(f"{url}/result/{job_id}", timeout=30)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"could not reach shard: {e}")
    if resp.status_code == 404:
        raise HTTPException(404, "unknown job_id")
    try:
        return resp.json()
    except ValueError:
        raise HTTPException(502, f"shard returned non-JSON response (status {resp.status_code})")


def _forward_check(shard_id: str, req: ProveRequest):
    url = shard_url(shard_id)
    if url is None:
        return {"ok": False, "error": f"unknown shard '{shard_id}'"}
    try:
        resp = httpx.post(f"{url}/check", json=req.model_dump(), timeout=req.timeout_seconds + 10)
    except httpx.TimeoutException:
        return {"ok": False, "error": "timeout", "routed_to": shard_id}
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"could not reach shard: {e}", "routed_to": shard_id}

    try:
        body = resp.json()
    except ValueError:
        # Shard returned a non-JSON response (e.g. a cold-start/proxy error
        # page) instead of crashing the whole router on resp.json().
        return {
            "ok": False,
            "error": f"shard returned non-JSON response (status {resp.status_code})",
            "shard_response_snippet": resp.text[:300],
            "routed_to": shard_id,
        }
    body["routed_to"] = shard_id
    return body


def _forward_submit(shard_id: str, req: ProveRequest):
    url = shard_url(shard_id)
    if url is None:
        return {"ok": False, "error": f"unknown shard '{shard_id}'"}
    try:
        # Submitting is instant (just schedules the shard's background
        # task) - a short timeout here is fine and correct.
        resp = httpx.post(f"{url}/submit", json=req.model_dump(), timeout=30)
        body = resp.json()
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"could not reach shard: {e}", "routed_to": shard_id}
    except ValueError:
        return {"ok": False, "error": f"shard returned non-JSON response (status {resp.status_code})", "routed_to": shard_id}
    body["routed_to"] = shard_id
    body["poll_url"] = f"/result/{shard_id}/{body.get('job_id')}"
    return body
