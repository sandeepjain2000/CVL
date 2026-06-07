-- Validation pipeline views for linkedin_data.db
-- Apply: python scripts/apply_validation_views.py
--
-- Requires employee_email_state table (populated from zeroclone CSV by apply script).

-- ---------------------------------------------------------------------------
-- Helpers (expression patterns used in views)
-- mv_valid:  lower(trim(status)) IN ('ok','valid','deliverable')
-- mv_invalid: trim(status) != '' AND NOT mv_valid
-- ---------------------------------------------------------------------------

-- Companies with usable domain
CREATE VIEW IF NOT EXISTS v_companies_with_domain AS
SELECT
    company_name,
    linkedin_url,
    lower(trim(replace(replace(company_domain, 'www.', ''), ' ', ''))) AS company_domain_clean,
    country,
    industry
FROM companies
WHERE company_domain IS NOT NULL
    AND trim(company_domain) != '';

-- Employees matching linkedin_scraper / extract_cycle rules (top 5 per company by title priority)
CREATE VIEW IF NOT EXISTS v_scrapeable_employees AS
WITH ranked AS (
    SELECT
        e.id AS employee_id,
        lower(trim(c.company_name)) || '|' || lower(trim(
            CASE
                WHEN e.employee_name LIKE '%·%' THEN ''
                ELSE e.employee_name
            END
        )) AS employee_key,
        c.company_name,
        c.linkedin_url AS company_linkedin_url,
        c.company_domain_clean AS company_domain,
        c.country,
        e.employee_name,
        e.job_title,
        row_number() OVER (
            PARTITION BY c.company_name
            ORDER BY
                CASE
                    WHEN lower(coalesce(e.job_title, '')) LIKE '%ceo%'
                      OR lower(coalesce(e.job_title, '')) LIKE '%chief%'
                      OR lower(coalesce(e.job_title, '')) LIKE '%managing director%'
                      OR lower(coalesce(e.job_title, '')) LIKE '%geschäftsführer%'
                      OR lower(coalesce(e.job_title, '')) LIKE '%founder%'
                      OR lower(coalesce(e.job_title, '')) LIKE '%owner%'
                      OR lower(coalesce(e.job_title, '')) LIKE '%director%'
                    THEN 0
                    ELSE 1
                END,
                e.employee_name
        ) AS rn
    FROM employees e
    INNER JOIN v_companies_with_domain c
        ON c.company_name = e.company_name
        OR (c.linkedin_url IS NOT NULL AND c.linkedin_url = e.company_linkedin_url)
    WHERE e.employee_name IS NOT NULL
        AND length(trim(e.employee_name)) > 3
        AND e.employee_name NOT LIKE '%·%'
        AND e.employee_name NOT LIKE '% 2nd%'
        AND e.employee_name NOT LIKE '% 3rd%'
        AND instr(trim(e.employee_name), ' ') > 0
)
SELECT
    employee_id,
    employee_key,
    company_name,
    company_linkedin_url,
    company_domain,
    country,
    employee_name,
    job_title
FROM ranked
WHERE rn <= 5;

-- Zerobounce / Million Verifier allowlist (same as send scripts)
CREATE VIEW IF NOT EXISTS v_zerobounce_allowlisted AS
SELECT
    lower(trim(email_address)) AS email_address,
    zb_status,
    mv_status,
    mv_quality,
    source_batch,
    pool_campaign_sent_at,
    pool_campaign_from_profile,
    imported_at
FROM zerobounce_validation
WHERE lower(trim(coalesce(zb_status, ''))) = 'valid'
   OR lower(trim(coalesce(mv_status, ''))) IN ('ok', 'valid', 'deliverable');

-- Pool sender queue: allowlisted, no email_attempts row
CREATE VIEW IF NOT EXISTS v_validated_pool_sendable AS
SELECT
    z.email_address,
    z.zb_status,
    z.mv_status,
    z.source_batch
FROM v_zerobounce_allowlisted z
WHERE NOT EXISTS (
    SELECT 1
    FROM email_attempts ea
    WHERE lower(trim(ea.email_address)) = z.email_address
);

-- Per-employee cascade status (join scrapeable + zeroclone state)
CREATE VIEW IF NOT EXISTS v_employee_validation_status AS
SELECT
    s.employee_id,
    s.employee_key,
    s.company_name,
    s.employee_name,
    s.company_domain,
    s.country,
    CASE WHEN st.employee_key IS NOT NULL THEN 1 ELSE 0 END AS in_validation_state,
    CASE WHEN trim(coalesce(st.resolved_valid_email, '')) != '' THEN 1 ELSE 0 END AS has_resolved_valid,
    st.validation_status AS last_validation_status,
    st.email_format AS last_email_format,
    st.email AS last_validated_email,
    st.last_updated,
    st.format_firstname_lastname_status,
    st.format_firstname_status,
    st.format_firstinitial_lastname_status,
    st.format_firstname_lastinitial_status,
    -- Eligible for format stage 1: no resolved valid; no format column set yet
    CASE
        WHEN trim(coalesce(st.resolved_valid_email, '')) != '' THEN 0
        WHEN trim(coalesce(st.format_firstname_lastname_status, '')) != ''
          OR trim(coalesce(st.format_firstname_status, '')) != ''
          OR trim(coalesce(st.format_firstinitial_lastname_status, '')) != ''
          OR trim(coalesce(st.format_firstname_lastinitial_status, '')) != ''
        THEN 0
        ELSE 1
    END AS eligible_firstname_lastname,
    -- Stage 2: firstname.lastname has result and is not valid (matches email_formats.py)
    CASE
        WHEN trim(coalesce(st.resolved_valid_email, '')) != '' THEN 0
        WHEN trim(coalesce(st.format_firstname_lastname_status, '')) = '' THEN 0
        WHEN lower(trim(st.format_firstname_lastname_status)) IN ('ok', 'valid', 'deliverable') THEN 0
        ELSE 1
    END AS eligible_firstname,
    -- Stage 3: stages 1-2 have results and both invalid
    CASE
        WHEN trim(coalesce(st.resolved_valid_email, '')) != '' THEN 0
        WHEN trim(coalesce(st.format_firstname_lastname_status, '')) = '' THEN 0
        WHEN lower(trim(st.format_firstname_lastname_status)) IN ('ok', 'valid', 'deliverable') THEN 0
        WHEN trim(coalesce(st.format_firstname_status, '')) = '' THEN 0
        WHEN lower(trim(st.format_firstname_status)) IN ('ok', 'valid', 'deliverable') THEN 0
        ELSE 1
    END AS eligible_firstinitial_lastname,
    -- Stage 4: stages 1-3 have results and all invalid
    CASE
        WHEN trim(coalesce(st.resolved_valid_email, '')) != '' THEN 0
        WHEN trim(coalesce(st.format_firstname_lastname_status, '')) = ''
          OR lower(trim(st.format_firstname_lastname_status)) IN ('ok', 'valid', 'deliverable')
        THEN 0
        WHEN trim(coalesce(st.format_firstname_status, '')) = ''
          OR lower(trim(st.format_firstname_status)) IN ('ok', 'valid', 'deliverable')
        THEN 0
        WHEN trim(coalesce(st.format_firstinitial_lastname_status, '')) = ''
          OR lower(trim(st.format_firstinitial_lastname_status)) IN ('ok', 'valid', 'deliverable')
        THEN 0
        ELSE 1
    END AS eligible_firstname_lastinitial,
    CASE
        WHEN trim(coalesce(st.resolved_valid_email, '')) != '' THEN 0
        WHEN (
            trim(coalesce(st.format_firstname_lastname_status, '')) = ''
            AND trim(coalesce(st.format_firstname_status, '')) = ''
            AND trim(coalesce(st.format_firstinitial_lastname_status, '')) = ''
            AND trim(coalesce(st.format_firstname_lastinitial_status, '')) = ''
        ) THEN 1
        WHEN trim(coalesce(st.format_firstname_lastname_status, '')) != ''
         AND lower(trim(st.format_firstname_lastname_status)) NOT IN ('ok', 'valid', 'deliverable')
        THEN 1
        WHEN trim(coalesce(st.format_firstname_lastname_status, '')) != ''
         AND lower(trim(st.format_firstname_lastname_status)) NOT IN ('ok', 'valid', 'deliverable')
         AND trim(coalesce(st.format_firstname_status, '')) != ''
         AND lower(trim(st.format_firstname_status)) NOT IN ('ok', 'valid', 'deliverable')
        THEN 1
        WHEN trim(coalesce(st.format_firstname_lastname_status, '')) != ''
         AND lower(trim(st.format_firstname_lastname_status)) NOT IN ('ok', 'valid', 'deliverable')
         AND trim(coalesce(st.format_firstname_status, '')) != ''
         AND lower(trim(st.format_firstname_status)) NOT IN ('ok', 'valid', 'deliverable')
         AND trim(coalesce(st.format_firstinitial_lastname_status, '')) != ''
         AND lower(trim(st.format_firstinitial_lastname_status)) NOT IN ('ok', 'valid', 'deliverable')
        THEN 1
        ELSE 0
    END AS eligible_any_format,
    CASE
        WHEN trim(coalesce(st.resolved_valid_email, '')) != '' THEN 0
        WHEN trim(coalesce(st.format_firstname_lastname_status, '')) != ''
         AND trim(coalesce(st.format_firstname_status, '')) != ''
         AND trim(coalesce(st.format_firstinitial_lastname_status, '')) != ''
         AND trim(coalesce(st.format_firstname_lastinitial_status, '')) != ''
         AND lower(trim(st.format_firstname_lastname_status)) NOT IN ('ok', 'valid', 'deliverable')
         AND lower(trim(st.format_firstname_status)) NOT IN ('ok', 'valid', 'deliverable')
         AND lower(trim(st.format_firstinitial_lastname_status)) NOT IN ('ok', 'valid', 'deliverable')
         AND lower(trim(st.format_firstname_lastinitial_status)) NOT IN ('ok', 'valid', 'deliverable')
        THEN 1
        ELSE 0
    END AS cascade_exhausted_no_valid
FROM v_scrapeable_employees s
LEFT JOIN employee_email_state st ON st.employee_key = s.employee_key;

-- Single-row dashboard (one pass over v_employee_validation_status, not six)
CREATE VIEW IF NOT EXISTS v_validation_pipeline_summary AS
SELECT
    (SELECT count(*) FROM employees) AS total_employee_rows,
    (SELECT count(*) FROM v_scrapeable_employees) AS scrapeable_employees,
    (SELECT count(*) FROM employee_email_state) AS rows_in_employee_email_state,
    (SELECT count(*)
     FROM v_scrapeable_employees s
     LEFT JOIN employee_email_state st ON st.employee_key = s.employee_key
     WHERE st.employee_key IS NULL) AS never_in_validation_cycle,
    evs.resolved_valid_count,
    evs.still_eligible_for_validation,
    evs.eligible_firstname_lastname,
    evs.eligible_firstname,
    evs.eligible_firstinitial_lastname,
    evs.eligible_firstname_lastinitial,
    evs.cascade_exhausted_no_valid,
    (SELECT count(*) FROM v_zerobounce_allowlisted) AS allowlisted_addresses,
    (SELECT count(*) FROM v_validated_pool_sendable) AS pool_sendable_addresses,
    (SELECT count(*) FROM email_attempts) AS email_attempts_total,
    (SELECT count(*) FROM email_attempts WHERE status = 'sent') AS email_attempts_sent,
    (SELECT count(*) FROM email_attempts WHERE status = 'bounced') AS email_attempts_bounced
FROM (
    SELECT
        sum(CASE WHEN has_resolved_valid = 1 THEN 1 ELSE 0 END) AS resolved_valid_count,
        sum(CASE WHEN eligible_any_format = 1 THEN 1 ELSE 0 END) AS still_eligible_for_validation,
        sum(CASE WHEN eligible_firstname_lastname = 1 THEN 1 ELSE 0 END) AS eligible_firstname_lastname,
        sum(CASE WHEN eligible_firstname = 1 THEN 1 ELSE 0 END) AS eligible_firstname,
        sum(CASE WHEN eligible_firstinitial_lastname = 1 THEN 1 ELSE 0 END) AS eligible_firstinitial_lastname,
        sum(CASE WHEN eligible_firstname_lastinitial = 1 THEN 1 ELSE 0 END) AS eligible_firstname_lastinitial,
        sum(CASE WHEN cascade_exhausted_no_valid = 1 THEN 1 ELSE 0 END) AS cascade_exhausted_no_valid
    FROM v_employee_validation_status
) evs;

-- Allowlist vs send tracking
CREATE VIEW IF NOT EXISTS v_allowlist_send_status AS
SELECT
    z.email_address,
    z.zb_status,
    z.mv_status,
    z.source_batch,
    CASE WHEN ea.email_address IS NOT NULL THEN 1 ELSE 0 END AS in_email_attempts,
    ea.status AS attempt_status,
    ea.sent_timestamp,
    ea.from_profile
FROM v_zerobounce_allowlisted z
LEFT JOIN email_attempts ea
    ON lower(trim(ea.email_address)) = z.email_address;

-- Employees with zero valid emails (no format with status ok/valid/deliverable)
CREATE VIEW IF NOT EXISTS Employees_with0_Valid_emails AS
SELECT *
FROM employee_email_state
WHERE lower(trim(coalesce(format_firstname_lastname_status, ''))) NOT IN ('ok', 'valid', 'deliverable')
  AND lower(trim(coalesce(format_firstname_status, ''))) NOT IN ('ok', 'valid', 'deliverable')
  AND lower(trim(coalesce(format_firstinitial_lastname_status, ''))) NOT IN ('ok', 'valid', 'deliverable')
  AND lower(trim(coalesce(format_firstname_lastinitial_status, ''))) NOT IN ('ok', 'valid', 'deliverable')
  AND trim(coalesce(resolved_valid_email, '')) = '';
