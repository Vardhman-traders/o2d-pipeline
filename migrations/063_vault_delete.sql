-- 063: bookkeeping for the guarded delete that follows an encrypted export (app/vault_delete.py). Holds counts and storage keys only.
--   vault_delete_plan   one row per preview ("plan"): what would be deleted, whether anything blocked it, and what happened
--   vault_delete_files  the stored photo/document files of a plan, so deleting them can be retried if storage was unreachable

CREATE TABLE vault_delete_plan (
    plan_id         varchar(40) PRIMARY KEY,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      varchar(60) NOT NULL,
    status          varchar(10) NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'blocked', 'executed', 'failed')),
    confirmed_batch integer,
    counts          jsonb NOT NULL,
    files_total     integer NOT NULL DEFAULT 0,
    delete_files    boolean NOT NULL DEFAULT true,
    blockers        jsonb,
    executed_at     timestamptz,
    result          jsonb
);

CREATE TABLE vault_delete_files (
    plan_id    varchar(40) NOT NULL REFERENCES vault_delete_plan (plan_id) ON DELETE CASCADE,
    object_key text NOT NULL,
    status     varchar(10) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'deleted', 'failed')),
    PRIMARY KEY (plan_id, object_key)
);
