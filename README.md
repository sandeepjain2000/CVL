# CVL Campaign & Scraper Workflow

Operational scripts, SQLite database, and templates for LinkedIn scraping and validated cold-email outreach.

## Prerequisites

```bash
pip install playwright
playwright install chromium
playwright install firefox   # optional; often unreliable on Windows — use Chromium
```

Log into LinkedIn in a dedicated **Firefox “scraper”** profile (Profile Manager) before your first scrape.

## Directory layout

| Path | Purpose |
|------|---------|
| `data/db/linkedin_data.db` | Companies, employees, search queue, validation tables |
| `data/csv/` | CSV exports (companies, employees, criteria, exclusion list) |
| `data/json/linkedin_state.json` | Saved Playwright session (cookies) |
| `templates/` | Email HTML templates |
| `logs/` | Runtime logs |
| `scripts/` | e.g. `apply_validation_views.py` |
| `sql/` | `validation_pipeline_views.sql` |

## LinkedIn scraper

**Engine:** Playwright **Chromium** by default (headed). Playwright Firefox often fails on Windows with D3D11/GPU errors; Chromium is the supported path for scraping.

**Cookies / account:** The browser window is **not** your daily Chrome. Session comes from `data/json/linkedin_state.json` and/or cookies copied from your Firefox **scraper** profile (`cookies.sqlite`). Configure the profile path in `linkedin_scraper.py` (`FIREFOX_PROFILE_DIR`) or pass `--firefox-profile`.

### Commands

```bash
# Test run (1 combo, 1 company, 1 employee) — default
python linkedin_scraper.py

# Production (~20 companies; retries companies with 0 employees)
python linkedin_scraper.py --run --browser chromium

# Log in / switch LinkedIn account in the scraper window
python linkedin_scraper.py --browser chromium --login
```

| Flag | Description |
|------|-------------|
| `--run` | Production limits (omit for test mode) |
| `--browser chromium` | **Default** — recommended on Windows |
| `--browser firefox` | Playwright Firefox only (may hang or stay blank) |
| `--browser auto` | One Firefox attempt, then Chromium |
| `--login` | Force login page and wait for manual sign-in |
| `--firefox-profile PATH` | Firefox profile folder with `cookies.sqlite` |

### Switch LinkedIn account

```powershell
Remove-Item -Force data\json\linkedin_state.json -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\linkedin_scraper_chromium_profile" -ErrorAction SilentlyContinue
python linkedin_scraper.py --browser chromium --login
```

Or log in via the Firefox scraper profile, clear the files above, and run without `--login` (cookies are imported from Firefox).

### Successful test run

Check `logs/linkedin_scraper.log` for:

- `Chromium window up`
- `LinkedIn session active`
- `Companies saved : 1` / `Employees saved : 1` (test mode)
- Console banner: `TOTAL EXECUTION TIME`

Each run is stored in **`scraper_runs`** (duration, companies/employees saved, test vs production):

```powershell
sqlite3 data/db/linkedin_data.db "SELECT id, started_at, duration_display, companies_saved, employees_saved, browser FROM scraper_runs ORDER BY id DESC LIMIT 10;"
```

## Examples (all scripts)

### LinkedIn scraper (`linkedin_scraper.py`)

```powershell
# Quick smoke test (default)
python linkedin_scraper.py

python linkedin_scraper.py --browser chromium

# Production scrape
python linkedin_scraper.py --run --browser chromium

# Custom Firefox cookie profile
python linkedin_scraper.py --firefox-profile "C:\Users\you\AppData\Roaming\Mozilla\Firefox\Profiles\xxxx.scraper"

# Try Playwright Firefox, fall back to Chromium
python linkedin_scraper.py --browser auto
```

### Campaign sender (`send_linkedin_campaigns_params.py`)

Sends to validated guessed emails from the DB walk (companies → employees → formats).

```powershell
# Default: all countries, 5 emails per Gmail profile
python send_linkedin_campaigns_params.py

# One country, 10 per profile
python send_linkedin_campaigns_params.py --country Austria --per-profile 10
python send_linkedin_campaigns_params.py -c Germany -n 5

# List countries in the database
python send_linkedin_campaigns_params.py --list-countries
```

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--country` | `-c` | (all) | Filter by country name |
| `--per-profile` | `-n` | `5` | Max sends per Gmail profile this run |
| `--list-countries` | | | Print countries and exit |

### Validated pool sender (`send_validated_pool.py`)

Sends directly to addresses in `zerobounce_validation` (allowlist), respecting exclusions.

```powershell
# 5 sends per Gmail profile (default)
python send_validated_pool.py

python send_validated_pool.py --per-profile 10
python send_validated_pool.py -n 3

# DB only — ignore email_progress_linkedin.json
python send_validated_pool.py --no-progress-json

# Retry addresses stuck in progress JSON only (duplicate risk if already sent)
python send_validated_pool.py --ignore-progress-json
```

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--per-profile` | `-n` | `5` | Max sends per profile |
| `--no-progress-json` | | | Do not read/write progress JSON |
| `--ignore-progress-json` | | | Do not skip “in progress JSON” only rows |

### Bounce checker (`check_bounces.py`)

```powershell
python check_bounces.py
```

No CLI flags — uses `credentials_FINAL.json` and IMAP settings in the script/config.

### Validation views (`scripts/apply_validation_views.py`)

```powershell
# Default DB + sync employee_email_state.csv, then create views
python scripts/apply_validation_views.py

python scripts/apply_validation_views.py --db data/db/linkedin_data.db

python scripts/apply_validation_views.py --no-sync-state
```

| Flag | Description |
|------|-------------|
| `--db` | SQLite path (default: `data/db/linkedin_data.db`) |
| `--state-csv` | Path to `employee_email_state.csv` |
| `--no-sync-state` | Skip CSV sync before applying SQL views |

## Other scripts (quick reference)

| Script | Role |
|--------|------|
| `send_validated_pool.py` | Send to allowlisted validation pool |
| `send_linkedin_campaigns_params.py` | Campaign sends from DB + guessed formats |
| `check_bounces.py` | IMAP bounce processing |
| `scripts/apply_validation_views.py` | Apply SQLite validation views |

## Secrets (do not commit)

- `credentials_FINAL.json`
- `data/json/linkedin_state.json`
- `email_progress_linkedin.json`

## Troubleshooting

| Symptom | Action |
|---------|--------|
| `add_cookies` / invalid `expires` | Update script; delete session JSON + chromium profile; `--login` |
| Firefox blank, no LinkedIn | Use `--browser chromium` |
| Wrong account | Clear session + `--login` (see above) |
| `Companies saved : 0` | Log may show no new URLs / exhausted combos |
| Stuck browser | Kill `firefox.exe` / `chrome.exe` from Playwright; clear profiles under `%LOCALAPPDATA%` |
