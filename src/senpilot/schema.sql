CREATE SCHEMA IF NOT EXISTS senpilot_private;
REVOKE ALL ON SCHEMA senpilot_private FROM PUBLIC;

CREATE TABLE IF NOT EXISTS senpilot_private.jobs (
    id uuid PRIMARY KEY,
    dedup_key text NOT NULL UNIQUE,
    email jsonb NOT NULL,
    state text NOT NULL DEFAULT 'queued'
        CHECK (state IN ('queued', 'processing', 'ready', 'sending', 'sent', 'failed', 'uncertain')),
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    owner uuid,
    lease_until timestamptz,
    reply_text text,
    artifact_key text,
    artifact_filename text,
    manifest jsonb,
    provider_message_id text,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE senpilot_private.jobs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE senpilot_private.jobs FROM PUBLIC;
CREATE INDEX IF NOT EXISTS jobs_claim_idx
    ON senpilot_private.jobs (state, available_at, created_at);
