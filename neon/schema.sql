-- neon/schema.sql
-- Control-plane schema. Does NOT store theorem/function source or .olean
-- artifacts - only "which shard serves this module" + "where is that shard".

CREATE TABLE IF NOT EXISTS shards (
    id          TEXT PRIMARY KEY,
    render_url  TEXT NOT NULL,
    namespaces  TEXT[] NOT NULL
);

CREATE TABLE IF NOT EXISTS modules (
    name        TEXT PRIMARY KEY,
    shard_id    TEXT NOT NULL REFERENCES shards(id)
);

CREATE INDEX IF NOT EXISTS modules_shard_id_idx ON modules (shard_id);
