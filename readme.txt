CVL Campaign & Scraper Workflow
===============================

Purpose
-------
Operational scripts, SQLite database, email templates, and batch files for
scraping LinkedIn companies/employees and running validated cold-email campaigns.


Prerequisites
-------------
  pip install playwright
  playwright install chromium
  playwright install firefox    # optional; often fails on Windows GPU — see Browser section

  Log into LinkedIn once in a dedicated Firefox profile (see LinkedIn Scraper below).


Directory structure
-------------------
  data/db/              SQLite database (linkedin_data.db)
  data/csv/             Exported criteria, companies, employees, exclusion list
  data/json/            linkedin_state.json (scraper session), email progress JSON
  templates/            Email HTML templates
  logs/                 Runtime logs (linkedin_scraper.log, etc.)
  scripts/              apply_validation_views.py and other helpers
  sql/                  validation_pipeline_views.sql
  archive/ / backups/   Historical copies


1. LinkedIn Scraper (linkedin_scraper.py)
-----------------------------------------
Scrapes companies and employees via Playwright into linkedin_data.db.

Browser (important)
  - Default: Chromium (reliable on Windows).
  - Playwright Firefox often fails on this machine (D3D11/GPU); use Chromium for scraping.
  - LinkedIn login cookies come from:
      (1) data/json/linkedin_state.json  (saved scraper session), or
      (2) Firefox "scraper" profile cookies.sqlite (see FIREFOX_PROFILE_DIR in script).
  - Your normal Chrome/Firefox apps are NOT used as the scraper window.

Firefox profile (cookie source)
  - Create a Firefox Profile Manager profile named e.g. "scraper".
  - Log into LinkedIn there; the script copies cookies.sqlite each run.
  - Default path in script: ...\Firefox\Profiles\YMCBoBDv.Profile 3
  - Override: --firefox-profile "C:\...\Profiles\your.profile"

How to run
  # Test smoke run (1 combo, 1 company, 1 employee) — default
  python linkedin_scraper.py

  # Same, explicit Chromium
  python linkedin_scraper.py --browser chromium

  # Production limits (~20 companies, backfill zero-employee companies)
  python linkedin_scraper.py --run --browser chromium

  # Switch LinkedIn account in the scraper browser
  python linkedin_scraper.py --browser chromium --login

  # Force Playwright Firefox (may hang or show blank page on some PCs)
  python linkedin_scraper.py --browser firefox

  # Try Firefox once, then fall back to Chromium
  python linkedin_scraper.py --browser auto

CLI flags
  --run              Production caps (no flag = test mode)
  --browser          chromium | firefox | auto  (default: chromium)
  --login            Open login page; wait for manual sign-in
  --firefox-profile  Path to Firefox profile folder with cookies.sqlite

Switching LinkedIn account (Chromium)
  1. Remove old session (optional but clean):
       del data\json\linkedin_state.json
       rmdir /s /q "%LOCALAPPDATA%\linkedin_scraper_chromium_profile"
  2. Either log in via Firefox scraper profile, then run scraper (imports cookies),
     OR run: python linkedin_scraper.py --browser chromium --login
     and sign in in the Playwright Chromium window.

Logs
  logs/linkedin_scraper.log — check "RUN SUMMARY", Companies saved, Employees saved.

Anti-blocking
  - Delays between pages, combos, and companies (tune DELAY_* in script).
  - Windows sleep prevention during runs.
  - JS keepalive / timer prefs for headed browser.


2. Validated pool sender (send_validated_pool.py)
-------------------------------------------------
Sends to addresses allowlisted in zerobounce_validation (SQLite).

  python send_validated_pool.py --per-profile 5

Respects exclusion_list.csv and per-profile daily caps.


3. Campaign sender (send_linkedin_campaigns_params.py)
----------------------------------------------------
Walks companies/employees in DB; queues sends for validated email formats.

  python send_linkedin_campaigns_params.py

Uses credentials_FINAL.json, email_progress_linkedin.json, templates/.


4. Bounce checker (check_bounces.py)
------------------------------------
Checks IMAP for bounces and updates email_attempts.

  python check_bounces.py


Validation pipeline (zeroclone + DB views)
------------------------------------------
  sql/validation_pipeline_views.sql
  python scripts/apply_validation_views.py

Summary view: v_validation_pipeline_summary (printed at end of scraper runs).


Configuration & secrets (do not commit)
---------------------------------------
  credentials_FINAL.json     SMTP / Gmail profiles
  data/json/linkedin_state.json   Scraper session (cookies)
  email_progress_linkedin.json    Send progress

Legacy: linkedin_state.json in project root is still read if data/json file is missing.


Troubleshooting
---------------
  Browser could not start / add_cookies expires error
    → Update linkedin_scraper.py; delete linkedin_state.json and chromium profile folder; retry with --login

  Firefox opens but LinkedIn blank
    → Use --browser chromium

  Wrong LinkedIn account in Chromium
    → See "Switching LinkedIn account" above

  Companies saved : 0
    → Check log for "No new companies"; combos may be exhausted or URLs already in DB

  Stuck firefox.exe / chrome.exe
    → End Task on Playwright processes; delete %LOCALAPPDATA%\linkedin_scraper_profile
      and linkedin_scraper_chromium_profile if needed
