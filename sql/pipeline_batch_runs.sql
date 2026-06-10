-- Unattended pipeline batch history (orchestrator: scripts/pipeline_batch_runner.py)
CREATE TABLE IF NOT EXISTS pipeline_batch_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          TEXT    NOT NULL,
    finished_at         TEXT,
    status              TEXT    NOT NULL DEFAULT 'running',
    scraper_runs_planned INTEGER NOT NULL DEFAULT 1,
    log_file            TEXT    NOT NULL DEFAULT '',
    failed_step         TEXT    DEFAULT '',
    failed_exit_code    INTEGER,
    notes               TEXT    DEFAULT ''
);

CREATE TABLE IF NOT EXISTS pipeline_batch_steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    INTEGER NOT NULL,
    step_order  INTEGER NOT NULL,
    step_name   TEXT    NOT NULL,
    started_at  TEXT    NOT NULL,
    finished_at TEXT,
    exit_code   INTEGER,
    status      TEXT    NOT NULL DEFAULT 'running',
    FOREIGN KEY (batch_id) REFERENCES pipeline_batch_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_batch_started
    ON pipeline_batch_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_pipeline_batch_steps_batch
    ON pipeline_batch_steps(batch_id, step_order);
