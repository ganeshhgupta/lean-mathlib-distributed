# lean-mathlib-distributed

Runs Lean 4 + mathlib type-checking on Render's free tier by splitting
mathlib across several small services instead of one ~12GB deployment.
Neon Postgres is the control plane (which module lives on which shard) -
it does not store theorem source or `.olean` artifacts.

```
                         ┌──────────────┐
POST /check  ─────────▶ │    router    │
                         │ (Render, free)│
                         └──────┬───────┘
                                │ looks up imports in Neon,
                                │ forwards to the one shard that has them
                 ┌──────────────┼──────────────┬──────────────┬──────────────┐
                 ▼              ▼              ▼              ▼              ▼
            algebra        analysis      numbertheory     topology     categorytheory
           (Render,       (Render,        (Render,        (Render,        (Render,
             free)          free)           free)           free)           free)
```

## Why 5 shards, not 30

mathlib4 has ~30 top-level namespaces (`Mathlib/Algebra`, `Mathlib/Topology`, ...).
Verified against the real mathlib4 source tree (see "How this was generated"
below), they're grouped into 5 shards by mathematical area, each carrying a
shared `common` base (`Data`, `Logic`, `Order`, `Tactic`, `Basic`, `Lean`,
`Control`, `Util` - the namespaces almost everything transitively depends on):

| shard | namespaces | modules (incl. common) |
|---|---|---|
| algebra | Algebra, GroupTheory, RingTheory, FieldTheory, LinearAlgebra, RepresentationTheory | 4332 |
| analysis | Analysis, MeasureTheory, Probability, Dynamics | 2879 |
| numbertheory | NumberTheory, Combinatorics, InformationTheory, Computability, Deprecated, Testing | 2047 |
| topology | Topology, AlgebraicTopology, Geometry, AlgebraicGeometry, Condensed | 2747 |
| categorytheory | CategoryTheory, SetTheory, ModelTheory | 2766 |

100% of mathlib4's 8559 real source files are covered by exactly one shard
(plus the common base) - `scripts/generate_shards.py` fails loudly if that
ever stops being true.

A 6th, finer split (e.g. one shard per namespace, ~30 services) was
considered and rejected: `common` alone is ~1500 modules, so a shard for a
33-module namespace like `Computability` would be >97% duplicate overhead.
Grouping related namespaces keeps the shard count sane and the duplication
ratio reasonable.

## Multi-shard files (the real limitation)

`import Mathlib` (the whole library) or a file whose imports span more than
one shard's *own* namespaces **cannot** be served - `router/server.py`
rejects it with the list of shards it would need. This is a fundamental
constraint, not a bug: Lean resolves imports against its local module
search path, so there's no way for `shard-analysis` to transparently
serve a declaration that only `shard-algebra` has compiled. Workaround if
you hit this often: add a 6th "union" shard that imports two specific
shards' namespaces together - expensive, so only do it for a combination
you actually need.

A file whose imports are entirely within the common base (e.g. plain
`Mathlib.Data.Nat.*` basics, no shard-specific namespace) is **not** an
ambiguity - every shard carries the common base, so the router picks
`DEFAULT_COMMON_SHARD` (`numbertheory`, the fastest/most consistent shard
in testing) automatically rather than making the caller choose.

## What's actually verified vs. still open

**Confirmed false: selective cache fetch.** The first real Render build
logs showed `lake update` triggers mathlib4's own post-update hook, which
runs an *unconditional full-library* `lake exe cache get` (all ~8939
files) regardless of what the downstream project imports. Passing a
module list to `cache get` afterward (the original plan) is both
pointless - the full cache is already on disk - and wrong syntax (the
cache CLI rejects dotted module names). So every shard downloads the same
~8939-file cache during build; sharding buys nothing on build-time
bandwidth.

**What sharding actually buys: post-build pruning.** Since Lean only
loads `.olean`s that are transitively imported by the file it's checking
(not "everything present on disk"), pruning doesn't reduce runtime RAM
per request - but it does reduce the final image's disk footprint, which
is the real free-tier constraint for fitting ~12GB of mathlib anywhere.
`prune_oleans.py` runs after `lake build ShardImports`: it parses the
*real* import graph from mathlib's checked-out `.lean` source (already in
the build context - no separate tool needed) and deletes every compiled
module outside the shard's true transitive closure. Measured against the
actual mathlib4 import graph (8559 modules, ~26800 import edges):

| shard | raw namespace modules | real transitive closure | % of full mathlib |
|---|---|---|---|
| algebra | 4332 | 5829 | 68.1% |
| analysis | 2879 | 5067 | 59.2% |
| numbertheory | 2047 | 5086 | 59.4% |
| topology | 2747 | 5655 | 66.1% |
| categorytheory | 2766 | 4570 | 53.4% |

Two things worth noting: (1) the transitive closure is always bigger than
the raw per-namespace module count - mathlib is deeply interconnected, so
even a "small" shard still pulls in over half the library; (2) this is a
disk-only win. If Render's constraint turns out to be RAM rather than
image size, pruning doesn't help at all, and there's no way to know which
constraint actually binds without deploying.

**Confirmed: runtime RAM is fine, CPU is the real bottleneck - and it's
severe.** Load-tested against all 5 live shards. `lean` itself loads and
elaborates correctly (RAM was never the issue). But `lake env`/`lake
build` re-verify the whole dependency graph on *every* invocation - fast
on Render's build machine, confirmed to hang past 100s+ at runtime on the
free tier's throttled CPU even for a no-op build. Fix: bypass lake
entirely at request time - `LEAN_PATH` is captured once during the Docker
build (while lake is still fast) into `lean_path.txt`, and `server.py`
invokes bare `lean <file>` with that env var set directly.

Even with that fix, elaborating a *single trivial goal* against one real
mathlib import measured **70 seconds to 5 minutes**, varying by shard and
by which specific file gets pulled in (e.g. `Analysis.SpecialFunctions.
Pow.Real` alone took over 7 minutes - real/exp/log machinery is
inherently heavy, independent of shard size). `numbertheory` was
consistently the fastest shard in testing and is the router's default
for common-only files.

**Hard ceiling, not configurable:** requests were repeatedly cut off
around 300-350s regardless of the `timeout_seconds` passed in the
request body (tested up to 600s) - this is enforced upstream of the
application (Render's edge / Cloudflare), not by anything in this repo.
Practically: this architecture can only serve proofs that elaborate
within about 5 minutes. Anything genuinely complex enough to exceed that
will fail with an HTTP timeout no matter how the server-side timeout is
tuned - a synchronous request/response API is the wrong shape for
"complex" proofs on this platform. An async job-submission API (submit,
poll, fetch result) would be required to go further, and wasn't built
here.

## Deploy

1. Push this repo to GitHub (already done if you're reading this from the repo).
2. In Render: **New > Blueprint**, pick this repo. It reads `render.yaml`
   and creates the router + 5 shard services.
3. When prompted for `DATABASE_URL` on the router service, paste the Neon
   connection string (pooled connection, `neondb` database).
4. First build per shard will be slow (mathlib cache fetch + build) -
   budget real time and watch Render's free-tier build-minute pool
   (shared across all 6 services, roughly 500-750 min/month).
5. Confirm each shard's URL in the Render dashboard matches
   `https://lean-shard-<id>.onrender.com` (the naming `neon/build_seed_sql.py`
   assumed). If Render appended a suffix because the name collided, run:
   ```sql
   UPDATE shards SET render_url = 'https://<actual-url>' WHERE id = '<shard id>';
   ```

## Test it

```bash
curl -X POST https://lean-mathlib-router.onrender.com/check \
  -H 'Content-Type: application/json' \
  -d '{
    "source": "import Mathlib.Algebra.Group.Basic\n\nexample (a b : Nat) : a + b = b + a := by ring"
  }'
```

### For anything that might take more than ~5 minutes: /submit + /result

`/check` shares Render's own edge timeout (~300-350s, confirmed by testing
up to 600s - the platform cuts the connection regardless of what
`timeout_seconds` is set to). For a genuinely complex proof, submit it as
a background job instead - the actual `lean` subprocess then runs inside
the shard's own process with no outbound HTTP call in the long-running
part, so nothing enforces that ceiling on it:

```bash
job=$(curl -s -X POST https://lean-mathlib-router.onrender.com/submit \
  -H 'Content-Type: application/json' \
  -d '{"source": "import Mathlib.Algebra.Group.Basic\n\n...a long proof..."}')
echo "$job"   # {"job_id": "...", "shard": "algebra", "status": "pending", "poll_url": "/result/algebra/..."}

# poll until status is "done" (or "error")
curl -s https://lean-mathlib-router.onrender.com/result/algebra/<job_id>
```

Jobs live in the shard's own process memory (each service runs a single
worker, so no cross-worker visibility problem) - they don't survive a
restart, and there's no cleanup/expiry implemented, so this is meant for
interactive use, not a production job queue.

## Regenerating shards

Everything under `shards/*` and `neon/seed.sql` is generated, not hand
written - `shards.json` is the single source of truth.

```bash
scripts/refresh_mathlib_files.sh   # re-pull the real mathlib4 file list (no build)
python3 scripts/generate_shards.py # regenerate shards/*/{lakefile.toml,Dockerfile,ShardImports.lean,server.py}
python3 neon/build_seed_sql.py     # regenerate neon/seed.sql from the same data
```

The namespace groupings in `shards.json` only decide the *seed* modules
per shard (and hence what `router/server.py` accepts). The actual pruning
in `prune_oleans.py` always works off the real transitive closure, parsed
from mathlib's own source at build time - so shrinking the namespace
groups further would shrink the closures too, at the cost of more
services and more duplicated build-minute spend on the shared full-cache
download every shard pays regardless.
