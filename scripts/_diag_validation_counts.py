#!/usr/bin/env python3
import sqlite3
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "db" / "linkedin_data.db"
c = sqlite3.connect(DB)

def one(sql):
    return c.execute(sql).fetchone()[0]

print("=== KEY COUNTS ===")
pairs = [
    ("total employees", "SELECT count(*) FROM employees"),
    ("scrapeable employees", "SELECT count(*) FROM v_scrapeable_employees"),
    ("employee_email_state rows", "SELECT count(*) FROM employee_email_state"),
    (
        "resolved_valid_email set (state table)",
        "SELECT count(*) FROM employee_email_state "
        "WHERE trim(coalesce(resolved_valid_email, '')) != ''",
    ),
    ("view resolved_valid_count", "SELECT resolved_valid_count FROM v_validation_pipeline_summary"),
    (
        "scrapeable + has_resolved_valid",
        "SELECT count(*) FROM v_employee_validation_status WHERE has_resolved_valid = 1",
    ),
    ("allowlisted addresses", "SELECT count(*) FROM v_zerobounce_allowlisted"),
    ("pool sendable", "SELECT count(*) FROM v_validated_pool_sendable"),
    ("zerobounce_validation rows", "SELECT count(*) FROM zerobounce_validation"),
]
for label, sql in pairs:
    print(f"  {label}: {one(sql):,}")

print("\n=== GAP: valid in zerobounce but not employee resolved ===")
print(
    "  allowlisted not in any resolved_valid_email:",
    one(
        """
        SELECT count(*) FROM v_zerobounce_allowlisted z
        WHERE NOT EXISTS (
            SELECT 1 FROM employee_email_state e
            WHERE lower(trim(e.resolved_valid_email)) = z.email_address
        )
        """
    ),
)

print("\n=== Recent MV validation (zerobounce_validation) ===")
for row in c.execute(
    """
    SELECT date(mv_validated_at), count(*)
    FROM zerobounce_validation
    WHERE mv_validated_at IS NOT NULL AND trim(mv_validated_at) != ''
    GROUP BY 1 ORDER BY 1 DESC LIMIT 10
    """
):
    print(f"  {row[0]}: {row[1]}")

print("\n=== Recent employee_email_state updates ===")
for row in c.execute(
    """
    SELECT date(last_updated),
           sum(CASE WHEN trim(coalesce(resolved_valid_email,'')) != '' THEN 1 ELSE 0 END),
           count(*)
    FROM employee_email_state
    WHERE last_updated IS NOT NULL
    GROUP BY 1 ORDER BY 1 DESC LIMIT 10
    """
):
    print(f"  {row[0]}: resolved={row[1]} total_updated={row[2]}")

c.close()
