CVL Campaign & Scraper Workflow
===============================

Purpose
-------
Operational scripts, database files, email templates, and batch files for scraping LinkedIn data and running cold email outreach campaigns.

Core Scripts
------------

1. LinkedIn Scraper (linkedin_scraper.py)
   --------------------------------------
   - Scrapes companies and employees from LinkedIn using a headed Firefox browser.
   - Includes anti-blocking settings to prevent browser sleeping/freezing when the window is out of focus (via Firefox occlusion preferences).
   - How to run:
     python linkedin_scraper.py

2. Validated Pool Sender (send_validated_pool.py)
   ----------------------------------------------
   - Drains the validated email address pool from `zerobounce_validation` in the database.
   - Excludes emails/domains matching the `exclusion_list.csv`.
   - Automatically handles sending limits (default: max 5 emails per Gmail profile, hard cap of 30 sends/day per profile).
   - Displays percentage progress updates after each success/failure send attempt (e.g. "Progress: 1/15 (6.7%)").
   - How to run:
     python send_validated_pool.py --per-profile 5

Directory Structure
-------------------
- data/db/           Contains the SQLite database (`linkedin_data.db`) storing combinations, companies, employees, and validation tables.
- data/csv/          Contains `exclusion_list.csv` to suppress domains, emails, or names.
- templates/         Contains email templates (`email_template_with_link.htm`).
- logs/              Runtime execution logs.
- archive/ / backups/ Historical archives of files.

Notes
-----
- Keep `email_config.json` locally containing SMTP profiles and application passwords. Never commit this file.
- Prevent Windows sleep behavior is automatically handled during script execution to ensure long runs complete successfully.
