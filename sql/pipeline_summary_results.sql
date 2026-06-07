-- Pre-computed pipeline summary (single current row, id = 1).
-- Populated by scripts/pipeline_summary_cache.py after views rebuild or bounce check.
-- Read by scripts/print_pipeline_summary.py for instant dashboard output.

CREATE TABLE IF NOT EXISTS pipeline_summary_results (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    refreshed_at TEXT NOT NULL,
    total_employee_rows INTEGER NOT NULL DEFAULT 0,
    scrapeable_employees INTEGER NOT NULL DEFAULT 0,
    rows_in_employee_email_state INTEGER NOT NULL DEFAULT 0,
    never_in_validation_cycle INTEGER NOT NULL DEFAULT 0,
    resolved_valid_count INTEGER NOT NULL DEFAULT 0,
    still_eligible_for_validation INTEGER NOT NULL DEFAULT 0,
    eligible_firstname_lastname INTEGER NOT NULL DEFAULT 0,
    eligible_firstname INTEGER NOT NULL DEFAULT 0,
    eligible_firstinitial_lastname INTEGER NOT NULL DEFAULT 0,
    eligible_firstname_lastinitial INTEGER NOT NULL DEFAULT 0,
    cascade_exhausted_no_valid INTEGER NOT NULL DEFAULT 0,
    allowlisted_addresses INTEGER NOT NULL DEFAULT 0,
    pool_sendable_addresses INTEGER NOT NULL DEFAULT 0,
    email_attempts_total INTEGER NOT NULL DEFAULT 0,
    email_attempts_sent INTEGER NOT NULL DEFAULT 0,
    email_attempts_bounced INTEGER NOT NULL DEFAULT 0,
    still_reachable INTEGER NOT NULL DEFAULT 0,
    never_emailed_once INTEGER NOT NULL DEFAULT 0,
    all_attempts_failed INTEGER NOT NULL DEFAULT 0,
    validation_unprocessed INTEGER NOT NULL DEFAULT 0
);

CREATE VIEW IF NOT EXISTS v_pipeline_summary_results AS
SELECT * FROM pipeline_summary_results WHERE id = 1;
