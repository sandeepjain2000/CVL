@echo off
setlocal EnableExtensions
title CVL - LinkedIn Scraper and Email Campaign Menu
cd /d "%~dp0"

REM Prefer Python launcher if available
set "PY=python"
where py >nul 2>&1 && set "PY=py -3"

:MAIN
cls
echo.
echo  ========================================================================
echo   CVL MENU  -  LinkedIn scraper, validation DB, cold email campaigns
echo   Folder: %CD%
echo  ========================================================================
echo.
echo   KEY METRIC (after scraper summary or option 6 below):
echo     ^>^>^> STILL REACHABLE: no successful send, formats not all exhausted
echo     (NOT the 7,247 outreach-ready line - that includes already-contacted)
echo.
echo   [1] LinkedIn scraper          (Playwright - scrape companies/employees)
echo   [2] Email campaigns           (send_linkedin_campaigns_params.py)
echo   [3] Validated pool sender     (pre-validated addresses only)
echo   [4] Database / validation     (sync state + SQL views)
echo   [5] Check bounces             (IMAP - update bounced status in DB)
echo   [6] Pipeline summary only     (no scrape - shows STILL REACHABLE count)
echo   [7] Help - which summary numbers mean what
echo   [8] CHEAT SHEET - run order, flags, when to use each script  ^<-- read this
echo   [0] Exit
echo.
set /p CHOICE="  Select option: "
if "%CHOICE%"=="1" goto MENU_SCRAPER
if "%CHOICE%"=="2" goto MENU_EMAIL
if "%CHOICE%"=="3" goto MENU_POOL
if "%CHOICE%"=="4" goto MENU_DB
if "%CHOICE%"=="5" goto RUN_BOUNCES
if "%CHOICE%"=="6" goto RUN_SUMMARY
if "%CHOICE%"=="7" goto HELP_METRICS
if "%CHOICE%"=="8" goto CHEAT_SHEET
if "%CHOICE%"=="0" exit /b 0
goto MAIN

:MENU_SCRAPER
cls
echo.
echo  --- LINKEDIN SCRAPER (linkedin_scraper.py) ---
echo   WHEN: Fill linkedin_data.db with companies + employees from LinkedIn.
echo   ORDER: After login works, run TEST once, then PRODUCTION on schedule.
echo   TIP: Close Chrome fully before USB backup; use Chromium on Windows.
echo.
echo   [1] TEST mode - 1 company, 1 employee (smoke test)
echo       Command: %PY% linkedin_scraper.py --browser chromium
echo.
echo   [2] PRODUCTION - full caps, Chromium (recommended on Windows)
echo       Command: %PY% linkedin_scraper.py --run --browser chromium
echo.
echo   [3] PRODUCTION - full caps, Firefox only
echo       Command: %PY% linkedin_scraper.py --run --browser firefox
echo.
echo   [4] PRODUCTION - try Firefox once, fall back to Chromium
echo       Command: %PY% linkedin_scraper.py --run --browser auto
echo.
echo   [5] Force LinkedIn login screen (switch account), then scrape TEST mode
echo       Command: %PY% linkedin_scraper.py --login --browser chromium
echo.
echo   [6] PRODUCTION + custom Firefox cookie profile path
echo       (copies cookies.sqlite from your Firefox profile for Chromium)
echo.
echo   [7] PRODUCTION - Chromium - NO BEEP (silent mode)
echo       Command: %PY% linkedin_scraper.py --run --browser chromium --no-beep
echo.
echo   [0] Back to main menu
echo.
set /p SCH="  Select option: "
if "%SCH%"=="1" goto SCRAPER_TEST
if "%SCH%"=="2" goto SCRAPER_PROD_CHROMIUM
if "%SCH%"=="3" goto SCRAPER_PROD_FIREFOX
if "%SCH%"=="4" goto SCRAPER_PROD_AUTO
if "%SCH%"=="5" goto SCRAPER_LOGIN
if "%SCH%"=="6" goto SCRAPER_CUSTOM_PROFILE
if "%SCH%"=="7" goto SCRAPER_PROD_CHROMIUM_SILENT
if "%SCH%"=="0" goto MAIN
goto MENU_SCRAPER

:SCRAPER_TEST
echo.
echo  Running TEST scrape...
%PY% linkedin_scraper.py --browser chromium
goto PAUSE_RETURN_MAIN

:SCRAPER_PROD_CHROMIUM
echo.
echo  Running PRODUCTION scrape - Chromium...
%PY% linkedin_scraper.py --run --browser chromium
goto PAUSE_RETURN_MAIN

:SCRAPER_PROD_CHROMIUM_SILENT
echo.
echo  Running PRODUCTION scrape - Chromium - NO BEEP...
%PY% linkedin_scraper.py --run --browser chromium --no-beep
goto PAUSE_RETURN_MAIN

:SCRAPER_PROD_FIREFOX
echo.
echo  Running PRODUCTION scrape - Firefox...
%PY% linkedin_scraper.py --run --browser firefox
goto PAUSE_RETURN_MAIN

:SCRAPER_PROD_AUTO
echo.
echo  Running PRODUCTION scrape - auto browser...
%PY% linkedin_scraper.py --run --browser auto
goto PAUSE_RETURN_MAIN

:SCRAPER_LOGIN
echo.
echo  Opening login flow, then TEST scrape...
%PY% linkedin_scraper.py --login --browser chromium
goto PAUSE_RETURN_MAIN

:SCRAPER_CUSTOM_PROFILE
set /p FFP="  Firefox profile folder path: "
echo.
echo  Running PRODUCTION scrape with custom profile...
%PY% linkedin_scraper.py --run --browser chromium --firefox-profile "%FFP%"
goto PAUSE_RETURN_MAIN

:MENU_EMAIL
cls
echo.
echo  --- EMAIL CAMPAIGNS (send_linkedin_campaigns_params.py) ---
echo   WHEN: After scrape + validation views; sends guessed emails per company.
echo   ORDER: Run [4] DB views, then [6] summary, then this. Next day run [5] bounces.
echo   CONFIG: data\json\credentials_FINAL.json + ..\EmailJson\email_config.json
echo.
echo   [1] List countries in database (no emails sent)
echo       Command: %PY% send_linkedin_campaigns_params.py --list-countries
echo.
echo   [2] Send to ALL countries - 5 emails per Gmail profile (default)
echo       Command: %PY% send_linkedin_campaigns_params.py -n 5
echo.
echo   [3] Send to ALL countries - 10 emails per Gmail profile
echo       Command: %PY% send_linkedin_campaigns_params.py -n 10
echo.
echo   [4] Custom: pick country + emails per profile (prompts)
echo.
echo   [5] Interactive sender (older script - prompts in terminal)
echo       Command: %PY% send_linkedin_campaigns.py
echo.
echo   [0] Back
echo.
set /p ECH="  Select option: "
if "%ECH%"=="1" goto EMAIL_LIST_COUNTRIES
if "%ECH%"=="2" goto EMAIL_ALL_N5
if "%ECH%"=="3" goto EMAIL_ALL_N10
if "%ECH%"=="4" goto EMAIL_CUSTOM
if "%ECH%"=="5" goto EMAIL_INTERACTIVE
if "%ECH%"=="0" goto MAIN
goto MENU_EMAIL

:EMAIL_LIST_COUNTRIES
%PY% send_linkedin_campaigns_params.py --list-countries
goto PAUSE_RETURN_MAIN

:EMAIL_ALL_N5
%PY% send_linkedin_campaigns_params.py -n 5
goto PAUSE_RETURN_MAIN

:EMAIL_ALL_N10
%PY% send_linkedin_campaigns_params.py -n 10
goto PAUSE_RETURN_MAIN

:EMAIL_CUSTOM
set /p ECOUNTRY="  Country name - exact, or blank for ALL: "
set /p EN="  Emails per Gmail profile - e.g. 5: "
if "%ECOUNTRY%"=="" goto EMAIL_CUSTOM_ALL
%PY% send_linkedin_campaigns_params.py -c "%ECOUNTRY%" -n %EN%
goto PAUSE_RETURN_MAIN

:EMAIL_CUSTOM_ALL
%PY% send_linkedin_campaigns_params.py -n %EN%
goto PAUSE_RETURN_MAIN

:EMAIL_INTERACTIVE
%PY% send_linkedin_campaigns.py
goto PAUSE_RETURN_MAIN

:MENU_POOL
cls
echo.
echo  --- VALIDATED POOL SENDER (send_validated_pool.py) ---
echo   WHEN: Extra sends for zerobounce allowlist NOT yet in email_attempts.
echo   ORDER: Optional after main campaign [2]; does NOT walk companies again.
echo.
echo   [1] Send - 5 per Gmail profile (default)
echo       Command: %PY% send_validated_pool.py -n 5
echo.
echo   [2] Send - 10 per Gmail profile
echo       Command: %PY% send_validated_pool.py -n 10
echo.
echo   [3] Custom emails per profile (prompt)
echo.
echo   [4] Send without reading/writing email_progress_linkedin.json
echo       Command: %PY% send_validated_pool.py -n 5 --no-progress-json
echo.
echo   [5] Send ignoring progress JSON only (duplicate risk if unsure)
echo       Command: %PY% send_validated_pool.py -n 5 --ignore-progress-json
echo.
echo   [0] Back
echo.
set /p PCH="  Select option: "
if "%PCH%"=="1" goto POOL_N5
if "%PCH%"=="2" goto POOL_N10
if "%PCH%"=="3" goto POOL_CUSTOM
if "%PCH%"=="4" goto POOL_NO_JSON
if "%PCH%"=="5" goto POOL_IGNORE_JSON
if "%PCH%"=="0" goto MAIN
goto MENU_POOL

:POOL_N5
%PY% send_validated_pool.py -n 5
goto PAUSE_RETURN_MAIN

:POOL_N10
%PY% send_validated_pool.py -n 10
goto PAUSE_RETURN_MAIN

:POOL_CUSTOM
set /p PN="  Emails per profile: "
%PY% send_validated_pool.py -n %PN%
goto PAUSE_RETURN_MAIN

:POOL_NO_JSON
%PY% send_validated_pool.py -n 5 --no-progress-json
goto PAUSE_RETURN_MAIN

:POOL_IGNORE_JSON
%PY% send_validated_pool.py -n 5 --ignore-progress-json
goto PAUSE_RETURN_MAIN

:MENU_DB
cls
echo.
echo  --- DATABASE / VALIDATION VIEWS ---
echo   WHEN: After zeroclone updates employee_email_state.csv (validation run).
echo   ORDER: Run BEFORE [6] summary or interpreting STILL REACHABLE counts.
echo.
echo   [1] Sync employee_email_state from zeroclone CSV + rebuild views
echo       Command: %PY% scripts\apply_validation_views.py
echo.
echo   [2] Rebuild views only (skip CSV sync)
echo       Command: %PY% scripts\apply_validation_views.py --no-sync-state
echo.
echo   [0] Back
echo.
set /p DCH="  Select option: "
if "%DCH%"=="1" goto DB_SYNC_VIEWS
if "%DCH%"=="2" goto DB_VIEWS_ONLY
if "%DCH%"=="0" goto MAIN
goto MENU_DB

:DB_SYNC_VIEWS
%PY% scripts\apply_validation_views.py
goto PAUSE_RETURN_MAIN

:DB_VIEWS_ONLY
%PY% scripts\apply_validation_views.py --no-sync-state
goto PAUSE_RETURN_MAIN

:RUN_BOUNCES
cls
echo.
echo  Running check_bounces.py - IMAP scan all Gmail profiles...
echo.
%PY% check_bounces.py
goto PAUSE_RETURN_MAIN

:RUN_SUMMARY
cls
echo.
echo  Printing pipeline summary (no browser)...
echo  Look for: ^>^>^> STILL REACHABLE
echo.
%PY% linkedin_scraper.py --summary-only
goto PAUSE_RETURN_MAIN

:CHEAT_SHEET
cls
echo.
echo  ========================================================================
echo   CHEAT SHEET - order, parameters, files (for when you forget)
echo  ========================================================================
echo.
echo  RECOMMENDED RUN ORDER (typical week)
echo  ------------------------------------
echo   1. Scraper [1] -^> PRODUCTION Chromium  (--run --browser chromium)
echo   2. Zeroclone validation (external) -^> updates employee_email_state.csv
echo   3. Database [4] -^> apply_validation_views.py  (sync CSV + rebuild views)
echo   4. Summary [6] -^> read ^>^>^> STILL REACHABLE  (how many left to pursue)
echo   5. Campaign [2] -^> send_linkedin_campaigns_params.py -n 5
echo   6. Next day: Bounces [5] -^> check_bounces.py  (marks bounced in DB)
echo   7. Optional: Pool [3] -^> send_validated_pool.py  (allowlist leftovers)
echo   8. Summary [6] again -^> see updated counts
echo.
echo  FIRST TIME / NEW MACHINE
echo  ------------------------
echo   - pip install playwright  &&  playwright install firefox
echo   - Log into LinkedIn in Firefox "scraper" profile (cookies.sqlite)
echo   - Menu [1] TEST scrape (no --run) to verify browser + login
echo   - Menu [4] apply views once CSV exists
echo.
echo  SCRIPT QUICK REFERENCE
echo  ----------------------
echo   linkedin_scraper.py
echo     (no flags)     TEST: 1 company, 1 employee
echo     --run          PRODUCTION limits + backfill zero-employee companies
echo     --browser chromium^|firefox^|auto   default chromium on Windows
echo     --login        Force re-login in browser window
echo     --firefox-profile PATH   Cookie source for Chromium
echo     --summary-only Print pipeline stats only (no browser)
echo     --no-beep      Disable all beeps (including company-change beep)
echo.
echo   send_linkedin_campaigns_params.py
echo     -n 5           Max 5 sends per Gmail profile this run
echo     -c "Austria"   Filter one country (exact name from --list-countries)
echo     --list-countries   Print countries and exit
echo.
echo   send_validated_pool.py
echo     -n 5           Sends per profile from zerobounce allowlist only
echo     --no-progress-json   Do not touch email_progress_linkedin.json
echo.
echo   scripts\apply_validation_views.py
echo     (default)      Sync zeroclone CSV + create SQL views
echo     --no-sync-state   Rebuild views only
echo.
echo   check_bounces.py   No flags - scans all Gmail in email_config.json
echo.
echo  IMPORTANT FILES
echo  ---------------
echo   data\db\linkedin_data.db              Main database
echo   data\json\email_progress_linkedin.json Resume / already-sent tracking
echo   ..\EmailJson\email_config.json        Gmail app passwords
echo   ..\zeroclone\cycles\state\employee_email_state.csv  Validation state
echo   logs\                                 Scraper and sender logs
echo.
echo  WHICH SUMMARY NUMBER TO USE
echo  ---------------------------
echo   ^>^>^> STILL REACHABLE  = still worth pursuing (your main "left to do")
echo   Outreach-ready 7,xxx   = max pool INCLUDES already emailed - do NOT add
echo.
pause
goto MAIN

:HELP_METRICS
cls
echo.
echo  WHICH ROW TO READ
echo  =================
echo.
echo   USE THIS (top of summary):
echo     ^>^>^> STILL REACHABLE: no successful send, formats not all exhausted
echo     = employees you can still pursue (no successful send yet,
echo       and not stuck after all 4 email format validations failed).
echo.
echo   NOT your main target:
echo     Outreach-ready (domain + name) ... 7,247
echo     = maximum pool with domain + name; INCLUDES already contacted
echo       and successfully emailed people. Do not treat as "left to do".
echo.
echo   Other useful lines:
echo     Never emailed - zero attempts
echo     Emailed before but every attempt failed - only bounces, no sent
echo     Addresses in send pool - ready to send right now (validated)
echo     All format patterns tried - no valid address (excluded from STILL REACHABLE)
echo.
echo   Run option [6] on main menu to print summary without scraping.
echo.
pause
goto MAIN

:PAUSE_RETURN_MAIN
echo.
echo  ------------------------------------------------------------------------
echo  Script finished. Results retained on screen.
echo  Press any key to close menu and return to command prompt.
pause
exit /b 0
