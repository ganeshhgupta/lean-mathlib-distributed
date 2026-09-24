# Dockerfile (generated - edit scripts/templates/Dockerfile.tmpl, not this file)
FROM ubuntu:22.04

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl git build-essential python3 python3-pip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV ELAN_HOME=/root/.elan
ENV PATH=$ELAN_HOME/bin:$PATH
RUN curl https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh -sSf \
    | sh -s -- -y --default-toolchain none

WORKDIR /app
COPY lean-toolchain lakefile.toml ./
RUN lake update

COPY ShardImports.lean shard_modules.txt ./

# `lake update`'s post-update hook already fetched mathlib's FULL .olean
# cache above (confirmed: it ignores which modules this shard needs), so
# there is nothing selective left to do here - just build this shard's
# umbrella file against what's already on disk.
RUN lake build ShardImports

# `lake env`/`lake build` re-verify the whole dependency graph on every
# invocation - fast on Render's build machine, but confirmed to hang
# past 100s+ on the free-tier runtime's throttled CPU even for a no-op
# build. Bypass lake entirely at request time: capture the LEAN_PATH it
# resolves to now (while lake is fast) and have server.py invoke bare
# `lean` with that LEAN_PATH directly - confirmed instant either way.
RUN lake env printenv LEAN_PATH > lean_path.txt && cat lean_path.txt

# Now prune: delete every compiled module outside this shard's real
# transitive import closure (computed from mathlib's checked-out source,
# not guessed) so the final image only carries what this shard needs.
COPY prune_oleans.py ./
RUN python3 prune_oleans.py

COPY server.py requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt

ENV SHARD_ID=algebra
EXPOSE 8000
CMD ["python3", "-m", "uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
