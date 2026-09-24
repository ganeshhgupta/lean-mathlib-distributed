# server.py (generated - edit scripts/templates/server.py.tmpl, not this file)
# Minimal Lean checker for this shard. Accepts a Lean source snippet, writes
# it to a scratch file inside the shard's lake workspace (so `import Mathlib.*`
# resolves against this shard's already-built ShardImports), runs `lake env
# lean` on it, and returns whether it type-checked.
import os
import subprocess
import tempfile
import uuid

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()
WORKSPACE = os.path.dirname(os.path.abspath(__file__))
SHARD_ID = os.environ.get("SHARD_ID", "unknown")


class CheckRequest(BaseModel):
    source: str
    timeout_seconds: int = 60


@app.get("/health")
def health():
    return {"ok": True, "shard": SHARD_ID}


@app.get("/debug")
def debug():
    probes = [
        ("lean_version", ["lean", "--version"]),
        ("net_github", ["curl", "-s", "-m", "5", "-o", "/dev/null", "-w", "%{http_code}", "https://github.com"]),
        ("net_lean_releases", ["curl", "-s", "-m", "5", "-o", "/dev/null", "-w", "%{http_code}", "https://releases.lean-lang.org"]),
        ("net_dns_github", ["getent", "hosts", "github.com"]),
        ("env_vars", ["sh", "-c", "env | grep -E 'ELAN|LEAN|PATH' | sort"]),
        ("lake_env_lean_version", ["lake", "env", "lean", "--version"]),
        ("lake_build_noop", ["lake", "build", "ShardImports"]),
    ]
    results = {}
    for name, cmd in probes:
        try:
            proc = subprocess.run(
                cmd, cwd=WORKSPACE, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL
            )
            results[name] = {"returncode": proc.returncode, "stdout": proc.stdout[:500], "stderr": proc.stderr[:500]}
        except subprocess.TimeoutExpired as e:
            results[name] = {"timeout": True, "partial_stdout": str(e.stdout)[:500], "partial_stderr": str(e.stderr)[:500]}
    return results


@app.post("/check")
def check(req: CheckRequest):
    fname = f"scratch_{uuid.uuid4().hex}.lean"
    fpath = os.path.join(WORKSPACE, fname)
    try:
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(req.source)

        proc = subprocess.run(
            ["lake", "env", "lean", fpath],
            cwd=WORKSPACE,
            capture_output=True,
            text=True,
            timeout=req.timeout_seconds,
            stdin=subprocess.DEVNULL,  # avoid blocking on uvicorn's inherited stdin
        )
        return {
            "ok": proc.returncode == 0,
            "shard": SHARD_ID,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "shard": SHARD_ID,
            "error": "timeout",
            "partial_stdout": (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else e.stdout,
            "partial_stderr": (e.stderr or b"").decode("utf-8", "replace") if isinstance(e.stderr, bytes) else e.stderr,
        }
    finally:
        if os.path.exists(fpath):
            os.remove(fpath)
