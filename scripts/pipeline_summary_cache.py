"""
Pre-compute pipeline summary metrics into pipeline_summary_results (single row).

Refreshed automatically after validation views rebuild and after bounce checks.
print_pipeline_summary.py reads this table by default (instant).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS_SQL = ROOT / "sql" / "pipeline_summary_results.sql"

VIEW_METRIC_KEYS = (
    "total_employee_rows",
    "scrapeable_employees",
    "rows_in_employee_email_state",
    "never_in_validation_cycle",
    "resolved_valid_count",
    "still_eligible_for_validation",
    "eligible_firstname_lastname",
    "eligible_firstname",
    "eligible_firstinitial_lastname",
    "eligible_firstname_lastinitial",
    "cascade_exhausted_no_valid",
    "allowlisted_addresses",
    "pool_sendable_addresses",
    "email_attempts_total",
    "email_attempts_sent",
    "email_attempts_bounced",
)

OUTREACH_KEYS = (
    "still_reachable",
    "never_emailed_once",
    "all_attempts_failed",
)

EXTRA_KEYS = ("validation_unprocessed",)

ZERO_REGISTRY_DB = ROOT.parent / "zeroclone" / "cycles" / "state" / "pipeline_registry.db"

TABLE_NAME = "pipeline_summary_results"

# Human-readable labels for console / log output (shared by summary scripts)
SUMMARY_LABEL_WIDTH = 72
SUMMARY_LABELS: dict[str, str] = {
    "still_reachable": (
        "STILL REACHABLE - no successful send, formats not all exhausted"
    ),
    "total_employee_rows": "All employee rows in database",
    "scrapeable_employees": (
        "Outreach-ready (domain + name) - includes already-contacted employees"
    ),
    "rows_in_employee_email_state": (
        "Employees tracked in email-validation state table"
    ),
    "never_in_validation_cycle": (
        "Outreach-ready employees never started in validation cycle"
    ),
    "resolved_valid_count": (
        "Employees with confirmed valid email - both sent and unsent"
    ),
    "still_eligible_for_validation": (
        "Still eligible for another validation / format attempt"
    ),
    "eligible_firstname_lastname": (
        "Next format to try: firstname.lastname@domain"
    ),
    "eligible_firstname": "Next format to try: firstname@domain",
    "eligible_firstinitial_lastname": (
        "Next format to try: f.lastname@domain (first initial)"
    ),
    "eligible_firstname_lastinitial": (
        "Next format to try: firstname.l@domain (last initial)"
    ),
    "cascade_exhausted_no_valid": (
        "All format patterns tried - no valid address found"
    ),
    "allowlisted_addresses": (
        "Allowlisted addresses (trusted / skip re-validation)"
    ),
    "pool_sendable_addresses": (
        "Addresses in send pool (ready for campaign)"
    ),
    "email_attempts_total": (
        "Total email_attempts rows (every send + bounce record)"
    ),
    "email_attempts_sent": "Attempts with status sent (SMTP accepted)",
    "email_attempts_bounced": (
        "Attempts with status bounced / delivery failed"
    ),
    "never_emailed_once": "Never emailed - not even one attempt",
    "all_attempts_failed": (
        "Emailed before but every attempt failed (0 sent, has bounce rows)"
    ),
    "validation_unprocessed": (
        "Validation unprocessed (zeroclone partial runs)"
    ),
}


def _count_validation_unprocessed() -> int:
    if not ZERO_REGISTRY_DB.is_file():
        return 0
    zc = sqlite3.connect(ZERO_REGISTRY_DB)
    try:
        if not zc.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='validation_unprocessed'"
        ).fetchone():
            return 0
        return zc.execute(
            "SELECT count(*) FROM validation_unprocessed WHERE resolved_at IS NULL"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return 0
    finally:
        zc.close()


def _ensure_extra_columns(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({TABLE_NAME})")}
    for key in EXTRA_KEYS:
        if key not in cols:
            conn.execute(
                f"ALTER TABLE {TABLE_NAME} ADD COLUMN {key} INTEGER NOT NULL DEFAULT 0"
            )
    conn.commit()


def ensure_results_table(conn: sqlite3.Connection) -> None:
    if RESULTS_SQL.is_file():
        conn.executescript(RESULTS_SQL.read_text(encoding="utf-8"))
        conn.commit()
    _ensure_extra_columns(conn)
    # One-time rename from earlier pipeline_summary_snapshot table
    old = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pipeline_summary_snapshot'"
    ).fetchone()
    if old:
        has_new = conn.execute(
            f"SELECT 1 FROM {TABLE_NAME} WHERE id = 1"
        ).fetchone()
        if not has_new:
            cols = ", ".join(
                ["refreshed_at", *VIEW_METRIC_KEYS, *OUTREACH_KEYS]
            )
            conn.execute(
                f"""
                INSERT INTO {TABLE_NAME} (id, {cols})
                SELECT 1, {cols} FROM pipeline_summary_snapshot WHERE id = 1
                """
            )
            conn.commit()
        conn.execute("DROP TABLE pipeline_summary_snapshot")
        conn.commit()


def _normalize_domain_sql(expr: str) -> str:
    return f"""
        lower(trim(replace(replace(replace(
            CASE WHEN instr({expr}, '/') > 0
                 THEN substr({expr}, 1, instr({expr}, '/') - 1)
                 ELSE {expr} END,
            'https://', ''), 'http://', ''), 'www.', '')))
    """


def _still_reachable(conn: sqlite3.Connection) -> dict[str, int]:
    empty = {
        "still_reachable": 0,
        "never_emailed_once": 0,
        "all_attempts_failed": 0,
    }
    try:
        conn.execute("SELECT 1 FROM email_attempts LIMIT 1")
    except sqlite3.OperationalError:
        return empty

    dom_ea = _normalize_domain_sql("ea.company_domain")
    dom_s = "lower(trim(s.company_domain))"
    row = conn.execute(
        f"""
        WITH base AS (
            SELECT DISTINCT
                s.employee_id AS employee_id,
                trim(s.employee_name) AS employee_name,
                {dom_s} AS company_domain,
                COALESCE(v.cascade_exhausted_no_valid, 0) AS cascade_exhausted_no_valid
            FROM v_scrapeable_employees s
            LEFT JOIN v_employee_validation_status v
                ON v.employee_key = s.employee_key
        ),
        attempt_stats AS (
            SELECT
                {dom_ea} AS company_domain,
                trim(ea.employee_name) AS employee_name,
                COUNT(ea.id) AS attempt_count,
                SUM(
                    CASE WHEN lower(COALESCE(ea.status, '')) = 'sent' THEN 1 ELSE 0 END
                ) AS sent_count
            FROM email_attempts ea
            WHERE ea.company_domain IS NOT NULL
              AND trim(ea.company_domain) != ''
              AND ea.employee_name IS NOT NULL
              AND trim(ea.employee_name) != ''
            GROUP BY 1, 2
        ),
        per_employee AS (
            SELECT
                b.employee_id,
                b.cascade_exhausted_no_valid,
                COALESCE(ast.attempt_count, 0) AS attempt_count,
                COALESCE(ast.sent_count, 0) AS sent_count
            FROM base b
            LEFT JOIN attempt_stats ast ON (
                ast.company_domain = b.company_domain
                AND ast.employee_name = b.employee_name
            )
        )
        SELECT
            SUM(CASE WHEN cascade_exhausted_no_valid = 0 AND sent_count = 0 THEN 1 ELSE 0 END),
            SUM(CASE WHEN cascade_exhausted_no_valid = 0 AND attempt_count = 0 THEN 1 ELSE 0 END),
            SUM(CASE WHEN cascade_exhausted_no_valid = 0
                      AND attempt_count > 0 AND sent_count = 0 THEN 1 ELSE 0 END)
        FROM per_employee
        """
    ).fetchone()
    if not row:
        return empty
    return {
        "still_reachable": int(row[0] or 0),
        "never_emailed_once": int(row[1] or 0),
        "all_attempts_failed": int(row[2] or 0),
    }


def _has_view(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name=?",
        (name,),
    ).fetchone()
    return row is not None


def refresh_pipeline_summary_snapshot(conn: sqlite3.Connection) -> str:
    """Recompute all summary metrics and persist one row in pipeline_summary_results."""
    if not _has_view(conn, "v_validation_pipeline_summary"):
        raise RuntimeError(
            "v_validation_pipeline_summary not found — run apply_validation_views.py first"
        )

    ensure_results_table(conn)
    refreshed_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    row = conn.execute("SELECT * FROM v_validation_pipeline_summary").fetchone()
    if not row:
        raise RuntimeError("v_validation_pipeline_summary returned no row")
    names = [
        d[0]
        for d in conn.execute("SELECT * FROM v_validation_pipeline_summary").description
    ]
    view_metrics = dict(zip(names, row))
    outreach = _still_reachable(conn)
    extras = {"validation_unprocessed": _count_validation_unprocessed()}

    values = [refreshed_at]
    for key in VIEW_METRIC_KEYS:
        values.append(int(view_metrics.get(key) or 0))
    for key in OUTREACH_KEYS:
        values.append(int(outreach.get(key) or 0))
    for key in EXTRA_KEYS:
        values.append(int(extras.get(key) or 0))

    all_keys = [*VIEW_METRIC_KEYS, *OUTREACH_KEYS, *EXTRA_KEYS]
    conn.execute(f"DELETE FROM {TABLE_NAME}")
    conn.execute(
        f"""
        INSERT INTO {TABLE_NAME} (
            id, refreshed_at,
            {", ".join(all_keys)}
        ) VALUES (1, {", ".join("?" * (1 + len(all_keys)))})
        """,
        values,
    )
    conn.commit()
    return refreshed_at


def load_pipeline_summary_snapshot(conn: sqlite3.Connection) -> dict[str, int | str] | None:
    """Return saved results row or None if table empty."""
    ensure_results_table(conn)
    all_keys = [*VIEW_METRIC_KEYS, *OUTREACH_KEYS, *EXTRA_KEYS]
    row = conn.execute(
        f"""
        SELECT refreshed_at, {", ".join(all_keys)}
        FROM {TABLE_NAME}
        WHERE id = 1
        """
    ).fetchone()
    if not row:
        return None
    keys = ["refreshed_at", *all_keys]
    out = dict(zip(keys, row))
    # Always live — zeroclone partial runs may change without a CVL snapshot refresh.
    out["validation_unprocessed"] = _count_validation_unprocessed()
    return out
