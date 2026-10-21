-- 049: Delegation extra work joins the scorecard. An entry now carries the hours spent, a decision by a manager / admin (Pending, Approved,
-- Rejected) and the points it earned (set at approval, like a task score, so a later change of the setting never rewrites history).
-- Entries logged before this migration are marked Approved with 0 points: they stay visible but do not change anybody's score.

ALTER TABLE fact_extra_work
    ADD COLUMN hours               numeric(5,2) CHECK (hours IS NULL OR (hours >= 0 AND hours <= 24)),
    ADD COLUMN status              varchar(10) NOT NULL DEFAULT 'Pending' CHECK (status IN ('Pending', 'Approved', 'Rejected')),
    ADD COLUMN points              integer,
    ADD COLUMN remark              varchar(500),
    ADD COLUMN decided_by_user_key integer REFERENCES dim_user (user_key),
    ADD COLUMN decided_at          timestamptz;

UPDATE fact_extra_work SET status = 'Approved', points = 0;

CREATE INDEX idx_extra_work_user ON fact_extra_work (user_key, work_date_key);
CREATE INDEX idx_extra_work_pending ON fact_extra_work (status) WHERE status = 'Pending';
