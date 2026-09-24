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
one shard **cannot** be served - `router/server.py` rejects it with the
list of shards it would need. This is a fundamental constraint, not a bug:
Lean resolves imports against its local module search path, so there's no
way for `shard-analysis` to transparently serve a declaration that only
`shard-algebra` has compiled. See the "Multi-shard files" discussion in the
design conversation this repo came from. Workaround if you hit this often:
add a 6th "union" shard that imports two specific shards' namespaces
together - expensive, so only do it for a combination you actually need.

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

**Still unverified: runtime RAM.** Free Render web services have 512MB
RAM. Even a pruned shard's compiled environment might not fit when Lean
loads it to check a proof that touches a lot of the closure - this hasn't
been load-tested. If `/check` OOMs, pruning doesn't fix it (see above);
the fix is a smaller shard or a paid Render plan for that shard.

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
