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

## Two things to verify before you trust this in production

1. **Selective cache fetch.** `Dockerfile` runs
   `lake exe cache get <shard's module list>` instead of a bare
   `lake exe cache get`, on the documented assumption that mathlib's cache
   tool only downloads the `.olean`s needed for the given modules (and
   their transitive deps) rather than the entire library. Watch the first
   Render build log's download size to confirm - if it pulls close to the
   full cache regardless, the shards stop saving anything and this
   architecture needs rethinking (e.g. a paid Render disk instead of free).
2. **Runtime RAM.** Free Render web services have 512MB RAM. Even a
   trimmed shard's compiled environment might not fit when Lean loads it
   to check a proof - this hasn't been load-tested. If `/check` OOMs, the
   fix is either a smaller shard or a paid Render plan for that shard only.

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

If you want finer-grained shards than "by top-level namespace" (e.g. actual
`.olean` size and import-edge based partitioning), don't hand-roll a
partitioner - mathlib4 already depends on
[`leanprover-community/importGraph`](https://github.com/leanprover-community/importGraph),
which is the real tool for extracting the import DAG. That's a local,
`lake build`-requiring exercise (needs the full environment compiled once)
and was out of scope for this pass.
