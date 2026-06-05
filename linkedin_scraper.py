"""
linkedin_scraper.py  –  v4.2
==============================
Production-ready LinkedIn scraper using Playwright (Firefox).

DATABASE DESIGN (4 criteria → 3 reference tables → 1 queue table)
-------------------------------------------------------------------

criteria_countries   – all 47 European countries with LinkedIn geo IDs
criteria_industries  – all ~150 industries with LinkedIn vertical IDs
criteria_sizes       – 7 company size bands with LinkedIn size codes
search_combinations  – the actual permutation queue
                       (country × industry × size, one row each)
                       tracks: status / last_page / total_found

companies            – scraped company records
employees            – scraped employee records

HOW THE QUEUE WORKS
--------------------
1. On first run: seed_criteria() fills the three reference tables.
2. build_combinations() generates the Cartesian product of
   (active countries × active industries × active sizes)
   and inserts each as a 'pending' row in search_combinations.
   INSERT OR IGNORE means re-runs never duplicate or reset progress.
3. discover_companies() picks the next pending/in_progress combination,
   fetches pages until it has enough NEW companies, and advances
   last_page after every page so runs resume exactly where they stopped.
4. When a combination returns 0 results it is marked 'exhausted'.

TO ACTIVATE / DEACTIVATE criteria edit the `active` column in the
reference tables (1 = active, 0 = skip) and call build_combinations()
again — only new rows are added.

CSV FILES EXPORTED
------------------
  criteria_countries.csv
  criteria_industries.csv
  criteria_sizes.csv
  search_combinations.csv   ← full permutation queue with status
  companies.csv
  employees.csv

Usage
-----
  pip install playwright
  playwright install firefox
  python linkedin_scraper.py              # test mode; Chromium (cookies from Firefox profile)
  python linkedin_scraper.py --run        # production caps (20 companies, etc.)
  python linkedin_scraper.py --browser firefox   # force Playwright Firefox (often fails on GPU)
  python linkedin_scraper.py --login      # force login in browser (works with either mode)
  python linkedin_scraper.py --firefox-profile "C:\\...\\Profiles\\..."
"""

import argparse
import asyncio
import json
import os
import sys
import csv
import logging
import random
import re
import shutil
import sqlite3
import time
import winsound
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator
from urllib.parse import urlparse, quote

BEEPS_ENABLED = True

# ---------------------------------------------------------------------------
# AUDIO ALERTS  —  Windows beeps at key script events
# ---------------------------------------------------------------------------
def _beep(freq_hz: int, duration_ms: int) -> None:
    """Play a Windows beep unless beeps are disabled by CLI."""
    if not BEEPS_ENABLED:
        return
    winsound.Beep(freq_hz, duration_ms)


def beep_ok() -> None:
    """Short high beep — company saved OK."""
    _beep(1000, 200)   # 1000 Hz, 200 ms

def beep_error() -> None:
    """Low double-beep — error on a company."""
    _beep(400, 300)    # 400 Hz, 300 ms
    _beep(300, 400)    # 300 Hz, 400 ms


def beep_company_change() -> None:
    """Brief transition beep when moving to next company."""
    _beep(800, 120)
    _beep(1000, 120)

def beep_done() -> None:
    """Rising triple beep + long finish tone — entire run finished."""
    _beep(600, 200)
    _beep(900, 200)
    _beep(1200, 400)
    _beep(750, 2000)   # extra-long final beep (~2 s) — execution complete


# ---------------------------------------------------------------------------
# WINDOWS SLEEP PREVENTION  —  keeps script running by preventing idle sleep
# ---------------------------------------------------------------------------
@contextmanager
def prevent_windows_sleep() -> Generator[None, None, None]:
    """
    Context manager to prevent Windows from entering sleep mode (system suspend)
    due to inactivity while the scraper is running. The display is still
    allowed to turn off normally.
    """
    if sys.platform != "win32":
        yield
        return
    import ctypes
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    try:
        logger.info("Setting Windows thread execution state to prevent sleep...")
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    except Exception as e:
        logger.warning("Could not set Windows execution state to prevent sleep: %s", e)
    try:
        yield
    finally:
        logger.info("Restoring Windows sleep behavior...")
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception as e:
            logger.error("Failed to restore Windows sleep state: %s", e)

from playwright.async_api import (
    Error as PlaywrightError,
    async_playwright,
    BrowserContext,
    Page,
    Response,
    Playwright,
)

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / f"linkedin_scraper_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)
logger.info("Log file: %s", LOG_FILE)

# ---------------------------------------------------------------------------
# FILE PATHS
# ---------------------------------------------------------------------------
SESSION_FILE    = str((Path(__file__).parent / "data" / "json" / "linkedin_state.json"))
DB_FILE         = str((Path(__file__).parent / "data" / "db" / "linkedin_data.db"))
Path(SESSION_FILE).parent.mkdir(parents=True, exist_ok=True)
Path(DB_FILE).parent.mkdir(parents=True, exist_ok=True)

# Firefox profile "scraper" (Profile Manager) — cookies copied from here each run.
# Override per run with: --firefox-profile "C:\...\Profiles\..."
FIREFOX_PROFILE_DIR    = (
    r"C:\Users\sandeep\AppData\Roaming\Mozilla\Firefox\Profiles"
    r"\YMCBoBDv.Profile 3"
)
# Throwaway profile Playwright owns entirely — recreated fresh every run
PLAYWRIGHT_PROFILE_DIR = r"C:\Users\sandeep\AppData\Local\linkedin_scraper_profile"
PLAYWRIGHT_CHROMIUM_PROFILE_DIR = (
    r"C:\Users\sandeep\AppData\Local\linkedin_scraper_chromium_profile"
)

# ---------------------------------------------------------------------------
# SCRAPING BEHAVIOUR  —  tune these to balance speed vs. block risk
# ---------------------------------------------------------------------------
DELAY_SHORT  = (2.5, 5.5)    # between UI actions within a page
DELAY_MEDIUM = (4.0, 8.0)   # between pages within one combination
DELAY_LONG   = (10.0, 15.0)  # after errors / retries

# Delay BETWEEN search combinations — critical for avoiding blocks.
# LinkedIn will rate-limit if you hammer many different filter combos
# in quick succession.  Keep this at 30-60 s minimum in production.
DELAY_BETWEEN_COMBOS = (30.0, 60.0)  # seconds — longer gap reduces bot detection

# After each company is fully scraped (profile + about + employees saved).
DELAY_BETWEEN_COMPANIES = (10.0, 20.0)

# Pause between each employee card while parsing the /people/ DOM (not used for API path).
DELAY_BETWEEN_EMPLOYEE_CARDS = (0.45, 1.35)

# How many search combinations to attempt per run.
# Each combination = one (country × industry × size) trio.
# Keep low (1-3) while testing; raise to 5-10 for production runs.
MAX_COMBOS_PER_RUN  = 30

# How many NEW companies to collect before stopping for this run.
MAX_COMPANIES_PER_RUN = 20     # ← raise for production (e.g. 50 or 100)

# How many LinkedIn result pages to fetch per combination (10 results/page).
# 10 pages = up to 100 companies per combination.
MAX_PAGES_PER_COMBO   = 10

# Max employees saved per company (API and DOM paths).
MAX_EMPLOYEES_PER_COMPANY = 10

TEST_MODE = False

# ---------------------------------------------------------------------------
# EMPLOYEE SEARCH FOCUS  —  reduce profile-history bias in /people/ results
# ---------------------------------------------------------------------------
EMPLOYEE_FOCUS_PRESETS: dict[str, list[str]] = {
    # Neutral mode intentionally sends no keyword bias to LinkedIn.
    "neutral": [],
    # HR / talent-oriented mode.
    "hr": [
        "human resources",
        "talent acquisition",
        "recruiter",
        "people operations",
        "hr business partner",
        "compensation",
        "benefits",
        "employee relations",
        "learning and development",
        "chief people officer",
        "head of hr",
    ],
    # Top leadership and decision-maker mode.
    "top-management": [
        "chief executive officer",
        "ceo",
        "coo",
        "cfo",
        "cto",
        "cio",
        "cmo",
        "president",
        "vice president",
        "director",
        "head of",
        "managing director",
        "general manager",
        "founder",
        "partner",
    ],
}

# ── LinkedIn geoUrn IDs for country-level location filtering ─────────────────
GEO_URN_MAP: dict[str, str] = {
    "Albania":                "102769717",
    "Andorra":                "100469284",
    "Austria":                "103883259",
    "Belarus":                "104390006",
    "Belgium":                "100565514",
    "Bosnia and Herzegovina": "102752635",
    "Bulgaria":               "105333783",
    "Croatia":                "104688944",
    "Cyprus":                 "104476105",
    "Czech Republic":         "104508036",
    "Denmark":                "104514075",
    "Estonia":                "102974008",
    "Finland":                "100456013",
    "France":                 "105015875",
    "Germany":                "101282230",
    "Greece":                 "104677530",
    "Hungary":                "100288700",
    "Iceland":                "105238872",
    "Ireland":                "104738515",
    "Italy":                  "103350119",
    "Kosovo":                 "105756016",
    "Latvia":                 "104341318",
    "Liechtenstein":          "100878084",
    "Lithuania":              "101464403",
    "Luxembourg":             "104042105",
    "Malta":                  "100807540",
    "Moldova":                "104081175",
    "Monaco":                 "104779124",
    "Montenegro":             "101955449",
    "Netherlands":            "102890719",
    "North Macedonia":        "101738252",
    "Norway":                 "103819153",
    "Poland":                 "105072130",
    "Portugal":               "100364837",
    "Romania":                "106670623",
    "Russia":                 "101728786",
    "San Marino":             "104135404",
    "Serbia":                 "101855366",
    "Slovakia":               "103119917",
    "Slovenia":               "106138358",
    "Spain":                  "105646813",
    "Sweden":                 "105117694",
    "Switzerland":            "106693272",
    "Ukraine":                "102264497",
    "United Kingdom":         "101165590",
    "Vatican City":           "104490509",
}

# ---------------------------------------------------------------------------
# CRITERIA FILES  —  edit these CSVs to control what gets scraped
# ---------------------------------------------------------------------------
# Place these files in the SAME folder as linkedin_scraper.py
#
#   criteria_1_search_type.csv   – always "Companies", rarely changed
#   criteria_2_countries.csv     – 47 European countries + LinkedIn geo IDs
#   criteria_3_industries.csv    – ~150 industries + LinkedIn vertical IDs
#   criteria_4_sizes.csv         – 7 company size bands + LinkedIn size codes
#
# To activate/deactivate any row: set active = 1 or active = 0 in the CSV.
# To verify a missing industry_id: search LinkedIn filtered to that industry,
# copy the industryCompanyVertical value from the URL, paste it in the CSV.
# ---------------------------------------------------------------------------

CRITERIA_DIR = Path(__file__).parent / "data" / "csv"
CRITERIA_DIR.mkdir(parents=True, exist_ok=True)

def _load_criteria_csv(filename: str) -> list[dict]:
    """Read a criteria CSV and return list of dicts. Skips header row."""
    path = CRITERIA_DIR / filename
    if not path.exists():
        logger.warning("Criteria file not found: %s  (using empty list)", path)
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))

def load_countries() -> list[dict]:
    rows = _load_criteria_csv("criteria_2_countries.csv")
    result = []
    for idx, r in enumerate(rows, 1):
        result.append({
            "name":       r["country_name"],
            "geo_id":     r["geo_id"],
            "active":     int(r.get("active", 1)),
            "sort_order": int(r["sort_order"]) if r.get("sort_order", "").strip().isdigit()
                          else idx,
        })
    return result

def load_industries() -> list[dict]:
    rows = _load_criteria_csv("criteria_3_industries.csv")
    return [{"name": r["industry_name"], "industry_id": r["industry_id"],
             "active": int(r.get("active", 1))} for r in rows]

def load_sizes() -> list[dict]:
    rows = _load_criteria_csv("criteria_4_sizes.csv")
    return [{"label": r["size_label"], "size_code": r["size_code"],
             "active": int(r.get("active", 1))} for r in rows]

# Load at import time so they are available to seed_criteria()
COUNTRIES  = load_countries()
INDUSTRIES = load_industries()
SIZES      = load_sizes()


# ===========================================================================
# HELPERS
# ===========================================================================

def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


async def sleep_rand(low: float, high: float, label: str = "") -> None:
    """
    Sleep for a random duration between low and high seconds.
    Broken into 1-second ticks so Windows timer throttling cannot
    stretch a short sleep into minutes when the window loses focus.
    """
    total = random.uniform(low, high)
    elapsed = 0.0
    tick = 1.0          # maximum chunk size (seconds)
    log_every = 30.0    # heartbeat interval
    last_heartbeat = 0.0
    while elapsed < total:
        chunk = min(tick, total - elapsed)
        await asyncio.sleep(chunk)
        elapsed += chunk
        last_heartbeat += chunk
        if last_heartbeat >= log_every:
            remaining = total - elapsed
            tag = f" [{label}]" if label else ""
            logger.debug("  ⏳ Waiting… %.0fs elapsed, %.0fs remaining%s", elapsed, remaining, tag)
            last_heartbeat = 0.0


def _transient_page_error(exc: BaseException) -> bool:
    """True when LinkedIn navigated/redirected mid-action (common after goto)."""
    msg = str(exc).lower()
    return (
        "execution context was destroyed" in msg
        or "target page, context or browser has been closed" in msg
        or "has been closed" in msg
        or "navigation" in msg
    )


async def wait_page_settled(page: Page, timeout_ms: int = 30_000) -> None:
    """Wait for redirects to finish before mouse/scroll (feed often navigates twice)."""
    try:
        await page.wait_for_load_state("load", timeout=timeout_ms)
    except PlaywrightError:
        pass
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=5_000)
    except PlaywrightError:
        pass
    await sleep_rand(0.35, 0.85)


async def maybe_mouse_drift(page: Page) -> None:
    """Small, slow pointer movement — breaks perfect scroll-only automation."""
    if random.random() > 0.42:
        return
    vp = page.viewport_size
    if not vp:
        return
    margin = 100
    x = random.randint(margin, max(margin + 1, vp["width"] - margin))
    y = random.randint(margin, max(margin + 1, vp["height"] - margin))
    try:
        await page.mouse.move(
            x + random.randint(-40, 40),
            y + random.randint(-25, 25),
            steps=random.randint(10, 26),
        )
    except PlaywrightError as exc:
        if _transient_page_error(exc):
            return
        raise


async def human_scroll(page: Page, steps: int = 5) -> None:
    """Slower, chunkier scrolls with occasional slight reverse (re-read) pauses."""
    for i in range(steps):
        if random.random() < 0.14:
            await maybe_mouse_drift(page)
        delta = random.randint(70, 210)
        if random.random() < 0.11:
            delta = -random.randint(35, 110)
        try:
            await page.mouse.wheel(0, delta)
        except PlaywrightError as exc:
            if _transient_page_error(exc):
                logger.debug("human_scroll: page navigated during scroll — stopping early")
                return
            raise
        await sleep_rand(0.55, 1.75)
        if random.random() < 0.2:
            await sleep_rand(0.35, 1.15)
        if i > 0 and random.random() < 0.08:
            await sleep_rand(0.25, 0.9)


def extract_domain(raw_url: str) -> str:
    if not raw_url:
        return ""
    url = raw_url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    try:
        netloc = urlparse(url).netloc
        return netloc.replace("www.", "", 1).lower().strip("/")
    except Exception:
        return raw_url


# ===========================================================================
# DATABASE  —  schema
# ===========================================================================

SCHEMA = """
-- ── CRITERIA REFERENCE TABLES ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS criteria_countries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    UNIQUE NOT NULL,
    geo_id      TEXT    NOT NULL,
    active      INTEGER DEFAULT 1,   -- 1 = include in search, 0 = skip
    sort_order  INTEGER DEFAULT 999, -- lower number = processed first
    notes       TEXT    DEFAULT ''
);

CREATE TABLE IF NOT EXISTS criteria_industries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    UNIQUE NOT NULL,
    industry_id TEXT    NOT NULL,    -- "TODO" = unverified, excluded from search
    active      INTEGER DEFAULT 1,
    notes       TEXT    DEFAULT ''
);

CREATE TABLE IF NOT EXISTS criteria_sizes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT    UNIQUE NOT NULL,
    size_code   TEXT    NOT NULL,
    active      INTEGER DEFAULT 1,
    notes       TEXT    DEFAULT ''
);

-- ── SEARCH PERMUTATION QUEUE ───────────────────────────────────────────────
-- One row per (country × industry × size) combination.
-- status: pending | in_progress | exhausted
-- last_page: 0-based LinkedIn results page, updated after every page fetch

CREATE TABLE IF NOT EXISTS search_combinations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    country_name    TEXT,
    geo_id          TEXT,
    industry_name   TEXT,
    industry_id     TEXT,
    size_label      TEXT,
    size_code       TEXT,
    status          TEXT    DEFAULT 'pending',
    last_page       INTEGER DEFAULT 0,
    total_found     INTEGER DEFAULT 0,
    created_at      TEXT,
    updated_at      TEXT,
    UNIQUE(geo_id, industry_id, size_code)
);

-- ── SCRAPED DATA ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS companies (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    linkedin_url        TEXT    UNIQUE NOT NULL,
    company_name        TEXT,
    country             TEXT,   -- ← country from the search combination that found it
    company_domain      TEXT,
    website_raw         TEXT,
    industry            TEXT,
    company_size        TEXT,
    headquarters        TEXT,
    company_type        TEXT,
    founded             TEXT,
    followers           TEXT,
    description         TEXT,
    scraped_timestamp   TEXT,
    updated_timestamp   TEXT
);

CREATE TABLE IF NOT EXISTS employees (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_url          TEXT    UNIQUE NOT NULL,
    employee_name        TEXT,
    job_title            TEXT,
    company_linkedin_url TEXT,
    company_name         TEXT,
    location             TEXT,
    connection_level     TEXT,
    mutual_connections   TEXT,
    scraped_timestamp    TEXT,
    updated_timestamp    TEXT
);

-- ── SCRAPER RUN HISTORY ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scraper_runs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at              TEXT    NOT NULL,
    finished_at             TEXT    NOT NULL,
    duration_seconds        REAL    NOT NULL,
    duration_display        TEXT    NOT NULL,
    test_mode               INTEGER NOT NULL DEFAULT 0,
    production_run          INTEGER NOT NULL DEFAULT 0,
    browser                 TEXT    DEFAULT '',
    companies_saved         INTEGER NOT NULL DEFAULT 0,
    employees_saved         INTEGER NOT NULL DEFAULT 0,
    countries_covered       TEXT    DEFAULT '',
    avg_seconds_per_company REAL,
    notes                   TEXT    DEFAULT ''
);

-- ── INDEXES ───────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_scraper_runs_started ON scraper_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_companies_url   ON companies(linkedin_url);
CREATE INDEX IF NOT EXISTS idx_employees_url   ON employees(profile_url);
CREATE INDEX IF NOT EXISTS idx_employees_co    ON employees(company_linkedin_url);
CREATE INDEX IF NOT EXISTS idx_combos_status   ON search_combinations(status);
-- NOTE: idx_countries_sort is created in _migrate_db() not here,
-- because sort_order column may not exist yet on older databases.
"""


def _migrate_db(conn: sqlite3.Connection) -> None:
    """
    Apply schema changes to existing databases. Safe to run on every startup.
    Each ALTER is guarded so it only runs once.
    """
    # ── criteria_countries: add sort_order ───────────────────────────────
    country_cols = {r[1] for r in conn.execute("PRAGMA table_info(criteria_countries)")}
    if "sort_order" not in country_cols:
        conn.execute("ALTER TABLE criteria_countries ADD COLUMN sort_order INTEGER DEFAULT 999")
        conn.commit()
        logger.info("Migration: added sort_order to criteria_countries.")

    try:
        conn.execute("DROP INDEX IF EXISTS idx_countries_sort")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_countries_sort ON criteria_countries(sort_order, active)")
        conn.commit()
    except Exception as exc:
        logger.debug("Index migration: %s", exc)

    # ── companies: add country column ────────────────────────────────────
    company_cols = {r[1] for r in conn.execute("PRAGMA table_info(companies)")}
    if "country" not in company_cols:
        conn.execute("ALTER TABLE companies ADD COLUMN country TEXT DEFAULT ''")
        conn.commit()
        logger.info("Migration: added country column to companies.")


def init_db(path: str = DB_FILE) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate_db(conn)   # apply any new columns to existing tables
    logger.info("Database ready: %s", path)
    return conn


@contextmanager
def db_cursor(conn: sqlite3.Connection) -> Generator[sqlite3.Cursor, None, None]:
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()


# ===========================================================================
# CRITERIA SEEDING
# ===========================================================================

def seed_criteria(conn: sqlite3.Connection) -> None:
    """
    Populate the three criteria reference tables from the Python lists above.
    INSERT OR IGNORE means running this again never overwrites manual edits
    made directly in the DB.
    """
    with db_cursor(conn) as cur:
        for idx, c in enumerate(COUNTRIES, 1):
            cur.execute(
                "INSERT OR IGNORE INTO criteria_countries "
                "(name, geo_id, active, sort_order) VALUES (?, ?, ?, ?)",
                (c["name"], c["geo_id"], c["active"], c.get("sort_order", idx)),
            )
            # Update sort_order on every run so CSV changes take effect
            cur.execute(
                "UPDATE criteria_countries SET sort_order=? WHERE name=?",
                (c.get("sort_order", idx), c["name"]),
            )
        for i in INDUSTRIES:
            cur.execute(
                "INSERT OR IGNORE INTO criteria_industries (name, industry_id, active) "
                "VALUES (?, ?, ?)",
                (i["name"], i["industry_id"], i["active"]),
            )
        for s in SIZES:
            cur.execute(
                "INSERT OR IGNORE INTO criteria_sizes (label, size_code, active) "
                "VALUES (?, ?, ?)",
                (s["label"], s["size_code"], s["active"]),
            )

    counts = {
        "countries":  conn.execute("SELECT COUNT(*) FROM criteria_countries").fetchone()[0],
        "industries": conn.execute("SELECT COUNT(*) FROM criteria_industries").fetchone()[0],
        "sizes":      conn.execute("SELECT COUNT(*) FROM criteria_sizes").fetchone()[0],
    }
    logger.info("Criteria tables: %s", counts)


def build_combinations(conn: sqlite3.Connection) -> int:
    """
    Generate the Cartesian product of all ACTIVE criteria rows and insert
    each combination into search_combinations.

    Rules:
    - Skips industries where industry_id = 'TODO' (unverified).
    - INSERT OR IGNORE so existing rows (with progress) are never reset.
    - Returns the number of NEW rows inserted.
    """
    countries  = conn.execute(
        "SELECT name, geo_id FROM criteria_countries "
        "WHERE active=1 ORDER BY sort_order ASC, name ASC"
    ).fetchall()
    industries = conn.execute(
        "SELECT name, industry_id FROM criteria_industries "
        "WHERE active=1 AND industry_id != 'TODO'"
    ).fetchall()
    sizes      = conn.execute(
        "SELECT label, size_code FROM criteria_sizes WHERE active=1"
    ).fetchall()

    ts = now_ts()
    inserted = 0
    with db_cursor(conn) as cur:
        for country_name, geo_id in countries:
            for industry_name, industry_id in industries:
                for size_label, size_code in sizes:
                    cur.execute("""
                        INSERT OR IGNORE INTO search_combinations
                            (country_name, geo_id, industry_name, industry_id,
                             size_label, size_code, status, last_page,
                             total_found, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, 0, ?, ?)
                    """, (country_name, geo_id, industry_name, industry_id,
                          size_label, size_code, ts, ts))
                    inserted += cur.rowcount
    # Note: combinations are ordered in get_next_combination by industry_id
    # to interleave industries rather than exhausting one industry's sizes first

    total = conn.execute("SELECT COUNT(*) FROM search_combinations").fetchone()[0]
    logger.info(
        "search_combinations: %d new rows added | %d total in queue",
        inserted, total,
    )
    return inserted


# ===========================================================================
# COMBINATION QUEUE MANAGEMENT
# ===========================================================================

def get_next_combination(conn: sqlite3.Connection) -> dict | None:
    """
    Return the next combination to process.
    in_progress rows are prioritised (resume interrupted run), then pending.
    Pending rows are ordered: country sort_order → industry_id → size_code
    so each run samples across many industries rather than exhausting
    all size bands of one industry before moving to the next.
    """
    row = conn.execute("""
        SELECT sc.id, sc.country_name, sc.geo_id, sc.industry_name, sc.industry_id,
               sc.size_label, sc.size_code, sc.last_page, sc.total_found
        FROM   search_combinations sc
        LEFT JOIN criteria_countries cc ON sc.country_name = cc.name
        WHERE  sc.status IN ('pending', 'in_progress')
        ORDER  BY
            CASE sc.status WHEN 'in_progress' THEN 0 ELSE 1 END,
            COALESCE(cc.sort_order, 999) ASC,
            sc.size_code ASC,
            sc.industry_id ASC
        LIMIT  1
    """).fetchone()

    if not row:
        return None

    return {
        "id":            row[0],
        "country_name":  row[1],
        "geo_id":        row[2],
        "industry_name": row[3],
        "industry_id":   row[4],
        "size_label":    row[5],
        "size_code":     row[6],
        "last_page":     row[7],
        "total_found":   row[8],
    }


def update_combination(conn: sqlite3.Connection, combo_id: int,
                        status: str, last_page: int, total_found: int) -> None:
    with db_cursor(conn) as cur:
        cur.execute("""
            UPDATE search_combinations
            SET    status=?, last_page=?, total_found=?, updated_at=?
            WHERE  id=?
        """, (status, last_page, total_found, now_ts(), combo_id))


def verify_search_combinations_table(conn: sqlite3.Connection) -> bool:
    """
    Confirm the combination queue table exists and log how many rows are
  available for get_next_combination() (pending + in_progress).
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='search_combinations'"
    ).fetchone()
    if not row:
        logger.error(
            "Table search_combinations is missing — run init_db() / check SCHEMA."
        )
        return False

    counts = dict(
        conn.execute(
            "SELECT status, COUNT(*) FROM search_combinations GROUP BY status"
        ).fetchall()
    )
    actionable = counts.get("pending", 0) + counts.get("in_progress", 0)
    logger.info(
        "Combination queue (search_combinations): %d actionable "
        "(pending=%d, in_progress=%d) | exhausted=%d | total=%d",
        actionable,
        counts.get("pending", 0),
        counts.get("in_progress", 0),
        counts.get("exhausted", 0),
        sum(counts.values()),
    )
    if actionable == 0:
        logger.warning(
            "No pending or in_progress combinations — discovery will not run. "
            "Activate criteria in DB/CSV and call build_combinations(), or reset "
            "exhausted rows to pending if you want to re-scan."
        )
    return True


def recover_stale_combinations(conn: sqlite3.Connection) -> int:
    """
    Mark in_progress rows that already finished all pages as exhausted so
    get_next_combination() can advance to the next queue entry.
    """
    with db_cursor(conn) as cur:
        cur.execute(
            """
            UPDATE search_combinations
            SET    status='exhausted', updated_at=?
            WHERE  status='in_progress' AND last_page >= ?
            """,
            (now_ts(), MAX_PAGES_PER_COMBO),
        )
        n = cur.rowcount
    if n:
        logger.info(
            "Recovered %d stale in_progress combination(s) → exhausted.", n
        )
    return n


def finalize_combination_status(
    *,
    exhausted: bool,
    last_page: int,
    added_this_combo: int,
    stopped_for_company_cap: bool,
) -> str:
    """
    Decide how to mark the current row in search_combinations.

    in_progress — only when this run stopped mid-combo (company cap); resume later.
    exhausted   — combo finished; get_next_combination() will pick the next row.
    """
    if stopped_for_company_cap:
        return "in_progress"
    if exhausted or last_page >= MAX_PAGES_PER_COMBO:
        return "exhausted"
    if added_this_combo == 0:
        return "exhausted"
    return "exhausted"


# ===========================================================================
# COMPANY / EMPLOYEE UPSERT
# ===========================================================================

def upsert_company(conn: sqlite3.Connection, rec: dict) -> None:
    sql = """
    INSERT INTO companies
        (linkedin_url, company_name, country, company_domain, website_raw,
         industry, company_size, headquarters, company_type, founded,
         followers, description, scraped_timestamp, updated_timestamp)
    VALUES
        (:linkedin_url, :company_name, :country, :company_domain, :website_raw,
         :industry, :company_size, :headquarters, :company_type, :founded,
         :followers, :description, :scraped_timestamp, :updated_timestamp)
    ON CONFLICT(linkedin_url) DO UPDATE SET
        company_name      = excluded.company_name,
        country           = excluded.country,
        company_domain    = excluded.company_domain,
        website_raw       = excluded.website_raw,
        industry          = excluded.industry,
        company_size      = excluded.company_size,
        headquarters      = excluded.headquarters,
        company_type      = excluded.company_type,
        founded           = excluded.founded,
        followers         = excluded.followers,
        description       = excluded.description,
        updated_timestamp = excluded.updated_timestamp
    """
    with db_cursor(conn) as cur:
        cur.execute(sql, rec)


def upsert_employee(conn: sqlite3.Connection, rec: dict) -> None:
    sql = """
    INSERT INTO employees
        (profile_url, employee_name, job_title, company_linkedin_url,
         company_name, location, connection_level, mutual_connections,
         scraped_timestamp, updated_timestamp)
    VALUES
        (:profile_url, :employee_name, :job_title, :company_linkedin_url,
         :company_name, :location, :connection_level, :mutual_connections,
         :scraped_timestamp, :updated_timestamp)
    ON CONFLICT(profile_url) DO UPDATE SET
        employee_name        = excluded.employee_name,
        job_title            = excluded.job_title,
        company_linkedin_url = excluded.company_linkedin_url,
        company_name         = excluded.company_name,
        location             = excluded.location,
        connection_level     = excluded.connection_level,
        mutual_connections   = excluded.mutual_connections,
        updated_timestamp    = excluded.updated_timestamp
    """
    with db_cursor(conn) as cur:
        cur.execute(sql, rec)


def get_scraped_urls(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT linkedin_url FROM companies").fetchall()
    return {r[0] for r in rows}


def get_companies_with_zero_employees(
    conn: sqlite3.Connection, limit: int
) -> list[tuple[str, str]]:
    """
    Companies already in DB but with no employee rows — for backfill when the
    account could not see /people/ on the first pass (e.g. fresh LinkedIn profile).
    Returns list of (linkedin_url, country_name).
    """
    if limit <= 0:
        return []
    cur = conn.execute(
        """
        SELECT c.linkedin_url, COALESCE(c.country, '')
        FROM companies c
        WHERE c.linkedin_url IS NOT NULL AND trim(c.linkedin_url) != ''
          AND (
            SELECT COUNT(*)
            FROM employees e
            WHERE e.company_name = c.company_name
               OR (
                    trim(COALESCE(c.linkedin_url, '')) != ''
                    AND e.company_linkedin_url = c.linkedin_url
                  )
          ) = 0
        ORDER BY c.updated_timestamp ASC, c.company_name
        LIMIT ?
        """,
        (limit,),
    )
    return [(r[0], r[1]) for r in cur.fetchall()]


# ===========================================================================
# CSV EXPORT  —  all 6 tables
# ===========================================================================

def export_all_csv(conn: sqlite3.Connection) -> None:
    """Export every table to its own CSV file."""
    conn.row_factory = sqlite3.Row

    exports = [
        ("criteria_countries",
         ["id","name","geo_id","active","notes"],
         "criteria_countries.csv"),
        ("criteria_industries",
         ["id","name","industry_id","active","notes"],
         "criteria_industries.csv"),
        ("criteria_sizes",
         ["id","label","size_code","active","notes"],
         "criteria_sizes.csv"),
        ("search_combinations",
         ["id","country_name","geo_id","industry_name","industry_id",
          "size_label","size_code","status","last_page","total_found",
          "created_at","updated_at"],
         "search_combinations.csv"),
        ("companies",
         ["id","linkedin_url","company_name","country","company_domain",
          "website_raw","industry","company_size","headquarters",
          "company_type","founded","followers","description",
          "scraped_timestamp","updated_timestamp"],
         "companies.csv"),
        ("employees",
         ["id","profile_url","employee_name","job_title",
          "company_linkedin_url","company_name","location",
          "connection_level","mutual_connections",
          "scraped_timestamp","updated_timestamp"],
         "employees.csv"),
    ]

    for table, fields, filename in exports:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        path = CRITERIA_DIR / filename
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows([dict(r) for r in rows])
        logger.info("CSV: %-35s  %d rows", path, len(rows))

    conn.row_factory = None


# ===========================================================================
# VOYAGER API INTERCEPTION
# ===========================================================================

_api_company_buffer:  list[dict] = []
_api_employee_buffer: list[dict] = []


def _parse_voyager_response(body: dict) -> None:
    elements = (body.get("elements")
                or body.get("data", {}).get("elements") or [])
    for el in elements:
        if "company" in el or el.get("type") in ("COMPANY", "company"):
            rec = _node_to_company(el.get("company") or el)
            if rec:
                _api_company_buffer.append(rec)
        if "profile" in el or el.get("type") in ("PROFILE", "profile"):
            rec = _node_to_employee(el.get("profile") or el)
            if rec:
                _api_employee_buffer.append(rec)
    if "universalName" in body or "companyPageUrl" in body:
        rec = _node_to_company(body)
        if rec:
            _api_company_buffer.append(rec)


def _node_to_company(node: dict) -> dict | None:
    name = (node.get("name") or node.get("localizedName")
            or node.get("companyName", ""))
    if not name:
        return None
    website_raw = node.get("websiteUrl") or node.get("companyPageUrl") or ""
    size_range  = node.get("staffCountRange") or {}
    size_str    = (f"{size_range.get('start','')}-{size_range.get('end','')}"
                   if size_range else str(node.get("staffCount", "")))
    hq = node.get("headquartersLocation") or node.get("headquarters") or ""
    if isinstance(hq, dict):
        hq = ", ".join(filter(None, [hq.get("city",""), hq.get("country","")]))
    industries = node.get("industries") or []
    industry   = industries[0] if industries else node.get("industryName", "")
    if isinstance(industry, dict):
        industry = industry.get("localizedName", "")
    linkedin_url = (
        node.get("url")
        or (f"https://www.linkedin.com/company/{node['universalName']}"
            if "universalName" in node else "")
    )
    founded_raw = node.get("foundedOn") or ""
    founded = (str(founded_raw.get("year", ""))
               if isinstance(founded_raw, dict) else str(founded_raw))
    ts = now_ts()
    return {
        "linkedin_url":      linkedin_url,
        "company_name":      str(name),
        "country":           "",          # filled by caller from combination
        "company_domain":    extract_domain(website_raw),
        "website_raw":       website_raw,
        "industry":          str(industry),
        "company_size":      size_str,
        "headquarters":      str(hq),
        "company_type":      "",
        "founded":           founded,
        "followers":         str(
            node.get("followingInfo", {}).get("followerCount", "")
            or node.get("followerCount", "")),
        "description":       (node.get("description") or "")[:500],
        "scraped_timestamp": ts,
        "updated_timestamp": ts,
    }


def _node_to_employee(node: dict) -> dict | None:
    first = node.get("firstName") or node.get("localizedFirstName") or ""
    last  = node.get("lastName")  or node.get("localizedLastName")  or ""
    name  = f"{first} {last}".strip() or node.get("publicIdentifier", "")
    if not name:
        return None
    pub_id      = node.get("publicIdentifier") or ""
    profile_url = (node.get("profileUrl")
                   or (f"https://www.linkedin.com/in/{pub_id}" if pub_id else ""))
    if not profile_url:
        return None
    positions    = (node.get("positions") or {}).get("elements") or []
    title = company_name = ""
    if positions:
        title        = positions[0].get("title", "")
        company_name = (positions[0].get("company") or {}).get("name", "")
    degree_map = {1: "1st", 2: "2nd", 3: "3rd"}
    dist_val   = (node.get("distance", {}) or {}).get("value", 0)
    ts = now_ts()
    return {
        "profile_url":           profile_url,
        "employee_name":         name,
        "job_title":             title,
        "company_linkedin_url":  "",
        "company_name":          company_name,
        "location":              node.get("locationName", ""),
        "connection_level":      degree_map.get(dist_val, ""),
        "mutual_connections":    "",
        "scraped_timestamp":     ts,
        "updated_timestamp":     ts,
    }


async def intercept_api_responses(page: Page) -> None:
    async def _handler(response: Response) -> None:
        if "voyager/api" not in response.url:
            return
        try:
            _parse_voyager_response(await response.json())
        except Exception:
            pass
    page.on("response", _handler)


# ===========================================================================
# BROWSER / SESSION
# ===========================================================================

# Avoid D3D11 / GPU-process hangs on Windows (common with Playwright Firefox).
_FIREFOX_GPU_SAFE_PREFS: dict[str, object] = {
    "layers.acceleration.disabled": True,
    "layers.gpu-process.enabled": False,
    "gfx.webrender.all": False,
    "gfx.webrender.enabled": False,
    "gfx.webrender.software": True,
    "gfx.webrender.software.draw": True,
    "media.hardware-video-decoding.enabled": False,
    "media.ffmpeg.vaapi.enabled": False,
}


def _firefox_launch_prefs() -> dict[str, object]:
    return {
        "general.useragent.override": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) "
            "Gecko/20100101 Firefox/146.0"
        ),
        "browser.startup.homepage_override.mstone": "ignore",
        "startup.homepage_welcome_url": "",
        "startup.homepage_welcome_url.additional": "",
        "privacy.resistFingerprinting": False,
        "dom.webdriver.enabled": False,
        "dom.min_background_timeout_value": 4,
        "dom.timeout.enable_budget_timer_throttling": False,
        "dom.min_background_timeout_value_without_budget_throttling": 4,
        "dom.timer.throttling.enabled": False,
        "widget.windows.window_occlusion_tracking.enabled": False,
        "browser.tabs.unloadOnLowMemory": False,
        "browser.window.left": 0,
        "browser.window.top": 0,
        "browser.window.width": 1280,
        "browser.window.height": 900,
        "browser.sessionstore.resume_from_crash": False,
        "browser.startup.page": 0,
        "browser.startup.homepage": "about:blank",
        **_FIREFOX_GPU_SAFE_PREFS,
    }


def _prepare_cookie_only_profile(firefox_profile_dir: str | None = None) -> str:
    """
    Copy ONLY cookies.sqlite from the real Firefox profile into a fresh
    throwaway directory. Avoids the 'older Firefox version' warning because
    the throwaway profile has no version stamp.
    """
    src  = Path(firefox_profile_dir or FIREFOX_PROFILE_DIR)
    dest = Path(PLAYWRIGHT_PROFILE_DIR)
    src_cookies = src / "cookies.sqlite"
    profile_label = str(src)

    if not src.exists():
        raise FileNotFoundError(
            f"Firefox profile not found:\n  {profile_label}"
        )
    if not src_cookies.exists():
        raise FileNotFoundError(
            f"cookies.sqlite missing from {profile_label}\n"
            "Log into LinkedIn in that Firefox profile first."
        )
    dest_cookie = dest / "cookies.sqlite"
    if (
        dest_cookie.exists()
        and src_cookies.exists()
        and src_cookies.stat().st_mtime <= dest_cookie.stat().st_mtime
    ):
        logger.info("Reusing Playwright profile (cookies unchanged): %s", dest)
        return str(dest)

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    shutil.copy2(src_cookies, dest_cookie)
    logger.info("Cookies copied from %s → %s", src, dest)
    return str(dest)


def _linkedin_cookies_from_firefox_sqlite(cookies_path: Path) -> list[dict]:
    """Read linkedin.com cookies from Firefox cookies.sqlite for Chromium import."""
    if not cookies_path.exists():
        return []
    copy_path = cookies_path.with_name(".cookies_import_copy.sqlite")
    try:
        shutil.copy2(cookies_path, copy_path)
        db_path = copy_path
    except OSError:
        db_path = cookies_path
    same_site_map = {0: "None", 1: "Lax", 2: "Strict"}
    out: list[dict] = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cols = {
                r[1]
                for r in conn.execute("PRAGMA table_info(moz_cookies)").fetchall()
            }
            has_same_site = "sameSite" in cols
            query = (
                "SELECT name, value, host, path, expiry, isSecure, isHttpOnly"
                + (", sameSite" if has_same_site else "")
                + " FROM moz_cookies WHERE host LIKE '%linkedin%'"
            )
            for row in conn.execute(query).fetchall():
                name, value, host, path, expiry, is_secure, is_http_only = row[:7]
                same_site = row[7] if has_same_site and len(row) > 7 else 1
                cookie: dict = {
                    "name": name,
                    "value": value,
                    "domain": host,
                    "path": path or "/",
                    "httpOnly": bool(is_http_only),
                    "secure": bool(is_secure),
                    "sameSite": same_site_map.get(int(same_site), "Lax"),
                }
                exp = int(expiry or 0)
                cookie["expires"] = float(exp) if exp > 0 else -1
                out.append(cookie)
        finally:
            conn.close()
    finally:
        if copy_path.exists() and copy_path != cookies_path:
            copy_path.unlink(missing_ok=True)
    return out


def _session_file_path() -> Path:
    """Prefer data/json/linkedin_state.json; fall back to legacy file in project root."""
    path = Path(SESSION_FILE)
    legacy = Path(__file__).parent / "linkedin_state.json"
    if not path.exists() and legacy.exists():
        return legacy
    return path


def _sanitize_playwright_cookies(
    cookies: list[dict],
    *,
    linkedin_only: bool = False,
) -> list[dict]:
    """
    Playwright accepts expires=-1 (session) or a positive Unix time in seconds.
    Storage export and Firefox sometimes use 0, floats, or milliseconds.
    """
    allowed_same_site = {"Strict", "Lax", "None"}
    out: list[dict] = []
    for raw in cookies:
        domain = (raw.get("domain") or "").lower()
        if linkedin_only and domain and "linkedin" not in domain:
            continue
        name = raw.get("name")
        if not name:
            continue
        value = raw.get("value")
        if value is None:
            continue
        if not domain and not raw.get("url"):
            continue

        cookie: dict = {
            "name": name,
            "value": value,
            "path": raw.get("path") or "/",
        }
        if raw.get("domain"):
            cookie["domain"] = raw["domain"]
        if raw.get("url"):
            cookie["url"] = raw["url"]
        if raw.get("httpOnly"):
            cookie["httpOnly"] = True
        if raw.get("secure"):
            cookie["secure"] = True
        ss = raw.get("sameSite")
        cookie["sameSite"] = ss if ss in allowed_same_site else "Lax"

        exp = raw.get("expires")
        if exp is None or exp == "":
            cookie["expires"] = -1
        elif isinstance(exp, (int, float)):
            if exp == -1:
                cookie["expires"] = -1
            elif exp > 0:
                cookie["expires"] = float(exp / 1000 if exp > 1e12 else exp)
            else:
                cookie["expires"] = -1
        else:
            cookie["expires"] = -1
        out.append(cookie)
    return out


async def _seed_linkedin_cookies(
    context: BrowserContext,
    firefox_profile_dir: str | None,
    *,
    cookies_applied: bool = False,
) -> None:
    if not cookies_applied and _session_state_fresh():
        cookies_applied = await _apply_storage_state_cookies(context)
    if cookies_applied:
        return
    src = Path(firefox_profile_dir or FIREFOX_PROFILE_DIR) / "cookies.sqlite"
    cookies = _sanitize_playwright_cookies(
        _linkedin_cookies_from_firefox_sqlite(src),
        linkedin_only=True,
    )
    if cookies:
        try:
            await context.add_cookies(cookies)
            logger.info("Imported %d LinkedIn cookie(s) from Firefox profile.", len(cookies))
        except PlaywrightError as e:
            logger.warning("Could not import Firefox cookies: %s", e)


def _session_state_fresh(max_age_hours: float = 168.0) -> bool:
    path = _session_file_path()
    if not path.exists():
        return False
    age_h = (time.time() - path.stat().st_mtime) / 3600
    return age_h <= max_age_hours


async def _apply_storage_state_cookies(context: BrowserContext) -> bool:
    path = _session_file_path()
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("cookies") or []
        cookies = _sanitize_playwright_cookies(raw, linkedin_only=True)
        if not cookies:
            return False
        await context.add_cookies(cookies)
        logger.info(
            "Applied %d LinkedIn cookie(s) from saved session (%s).",
            len(cookies),
            path.name,
        )
        return True
    except PlaywrightError as e:
        logger.warning("Saved session cookies rejected by browser (%s): %s", path, e)
        return False
    except Exception as e:
        logger.warning("Could not load cookies from %s: %s", path, e)
        return False


async def _open_linkedin_feed(page: Page, *, browser_label: str) -> bool:
    """Navigate to the feed and return True if the tab reached linkedin.com."""
    for attempt in (1, 2):
        try:
            await page.goto(
                "https://www.linkedin.com/feed/",
                wait_until="commit" if attempt == 1 else "domcontentloaded",
                timeout=60_000,
            )
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=25_000)
            except PlaywrightError:
                pass
        except PlaywrightError as e:
            logger.warning(
                "%s: feed navigation attempt %d failed: %s",
                browser_label,
                attempt,
                e,
            )
            continue
        if "linkedin.com" in page.url.lower():
            return True
        logger.warning(
            "%s: tab did not reach LinkedIn after attempt %d (url=%r).",
            browser_label,
            attempt,
            page.url,
        )
    return False


async def _verify_linkedin_logged_in(context: BrowserContext) -> bool:
    """Lightweight auth check — avoids loading the heavy /feed/ page."""
    try:
        resp = await context.request.get(
            "https://www.linkedin.com/voyager/api/me",
            timeout=20_000,
        )
        if resp.status != 200:
            return False
        snippet = (await resp.text())[:800].lower()
        return not any(x in snippet for x in ("signup", "authwall", "guest"))
    except Exception as e:
        logger.debug("Voyager session check failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# JS KEEPALIVE  —  injected into every page to defeat browser throttling
# ---------------------------------------------------------------------------
# Firefox (and Chrome) throttle JS timers to ≥1000 ms when the page is
# considered "hidden" (Page Visibility API: document.hidden == true).
# This happens whenever the window moves behind another app — even though
# our firefox_user_prefs already set dom.timer.throttling.enabled=False.
# The prefs are not always honoured in newer Firefox builds.
#
# The script below:
#   1. Overrides document.hidden / visibilityState so the page always
#      reports "visible" to any JS code (including LinkedIn's own code).
#   2. Fires a 400ms setInterval that does trivial work — this keeps the
#      JS timer scheduler "awake" so our asyncio.sleep ticks fire on time.
# ---------------------------------------------------------------------------
_JS_KEEPALIVE = """
(function () {
  // ── 1. Spoof Page Visibility API ─────────────────────────────────────
  try {
    Object.defineProperty(document, 'hidden',           { get: () => false });
    Object.defineProperty(document, 'visibilityState',  { get: () => 'visible' });
    Object.defineProperty(document, 'webkitHidden',     { get: () => false });
    Object.defineProperty(document, 'webkitVisibilityState', { get: () => 'visible' });
    // Suppress any existing visibilitychange listeners firing "hidden"
    const _ae = document.addEventListener.bind(document);
    document.addEventListener = function (type, fn, opts) {
      if (type === 'visibilitychange') return;  // swallow
      _ae(type, fn, opts);
    };
  } catch (e) {}

  // ── 2. Sub-second heartbeat to keep timer scheduler alive ────────────
  let _tick = 0;
  setInterval(function () { _tick = (_tick + 1) & 0x7fffffff; }, 400);
})();
"""


async def _launch_firefox_context(
    playwright: Playwright,
    firefox_profile_dir: str | None,
    *,
    fast_warmup: bool,
    max_attempts: int = 2,
) -> BrowserContext:
    os.environ.setdefault("MOZ_DISABLE_GPU", "1")
    os.environ.setdefault("MOZ_WEBRENDER", "0")
    os.environ.setdefault("MOZ_SOFTWARE_RENDERING", "1")

    profile_dir = _prepare_cookie_only_profile(firefox_profile_dir)
    logger.info(
        "Launching Playwright Firefox%s…",
        " [quick warmup]" if fast_warmup else "",
    )
    launch_start = time.perf_counter()
    context: BrowserContext | None = None
    last_err: PlaywrightError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            context = await playwright.firefox.launch_persistent_context(
                user_data_dir=profile_dir,
                headless=False,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
                firefox_user_prefs=_firefox_launch_prefs(),
                ignore_default_args=["-wait-for-browser"],
                timeout=90_000,
            )
            break
        except PlaywrightError as e:
            last_err = e
            logger.error("Firefox launch failed (attempt %d/2): %s", attempt, e)
            if attempt >= max_attempts:
                raise
            logger.info("Wiping Playwright profile and retrying Firefox once…")
            shutil.rmtree(profile_dir, ignore_errors=True)
            profile_dir = _prepare_cookie_only_profile(firefox_profile_dir)
    if context is None:
        raise last_err or RuntimeError("Firefox launch failed with no context")
    logger.info(
        "Firefox window up in %.1fs (starts on a blank page; LinkedIn loads next).",
        time.perf_counter() - launch_start,
    )
    return context


async def _launch_chromium_context(playwright: Playwright) -> BrowserContext:
    dest = Path(PLAYWRIGHT_CHROMIUM_PROFILE_DIR)
    dest.mkdir(parents=True, exist_ok=True)
    logger.info("Launching Chromium (Firefox GPU/D3D11 unavailable on this PC)…")
    launch_start = time.perf_counter()
    context = await playwright.chromium.launch_persistent_context(
        str(dest),
        headless=False,
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        timeout=60_000,
    )
    logger.info("Chromium window up in %.1fs.", time.perf_counter() - launch_start)
    return context


async def _warm_up_linkedin_session(
    context: BrowserContext,
    *,
    force_login: bool,
    fast_warmup: bool,
    firefox_profile_dir: str | None,
    browser_label: str,
) -> BrowserContext:
    await context.add_init_script(_JS_KEEPALIVE)
    cookies_applied = False
    if _session_state_fresh():
        cookies_applied = await _apply_storage_state_cookies(context)
    if browser_label == "chromium":
        await _seed_linkedin_cookies(
            context, firefox_profile_dir, cookies_applied=cookies_applied
        )

    # Reuse the default tab (about:blank) — avoids an extra empty tab sitting on top.
    if context.pages:
        page = context.pages[0]
        for extra in context.pages[1:]:
            await extra.close()
    else:
        page = await context.new_page()
    await page.bring_to_front()

    if force_login:
        logger.info("Force login requested. Logging out of current session...")
        try:
            await page.goto(
                "https://www.linkedin.com/m/logout",
                wait_until="load",
                timeout=30_000,
            )
            await sleep_rand(4.0, 6.0, label="logout-wait")
        except Exception as e:
            logger.warning("Error during logout navigation: %s", e)
        
        # Navigate directly to the login page to prompt sign-in
        await page.goto(
            "https://www.linkedin.com/login",
            wait_until="load",
            timeout=30_000,
        )
    elif fast_warmup:
        logger.info("Checking LinkedIn session (API)…")
        if await _verify_linkedin_logged_in(context):
            logger.info("LinkedIn session active (API) — opening feed in browser…")
        else:
            logger.info("API check inconclusive — opening feed to verify session…")
        if not await _open_linkedin_feed(page, browser_label=browser_label):
            logger.error(
                "%s could not load LinkedIn in the browser window. "
                "On this PC Playwright Firefox often fails D3D11/GPU; "
                "use: python linkedin_scraper.py --browser chromium",
                browser_label.capitalize(),
            )
        await sleep_rand(1.0, 2.0, label="session-check")
    else:
        logger.info("Opening LinkedIn feed to verify session…")
        if not await _open_linkedin_feed(page, browser_label=browser_label):
            logger.error(
                "%s could not load LinkedIn. Try --browser chromium.",
                browser_label.capitalize(),
            )

    if fast_warmup:
        pass  # already handled above (API path or short feed fallback)
    else:
        await wait_page_settled(page)
        await sleep_rand(*DELAY_SHORT)
        await maybe_mouse_drift(page)
        await human_scroll(page, steps=random.randint(2, 4))
        await wait_page_settled(page)

    if "login" in page.url or "authwall" in page.url or "challenge" in page.url or force_login:
        wait_secs = 120 if fast_warmup else 300
        logger.warning(
            "Cookies expired, Captcha hit, or Force Login requested — "
            "please log in manually in the browser window. Pausing %ds…",
            wait_secs,
        )
        await sleep_rand(wait_secs, wait_secs, label="manual-login-wait")
        await context.storage_state(path=SESSION_FILE)
    else:
        logger.info("LinkedIn session active — no login needed.")

    # Keep the LinkedIn tab open (scraping opens its own pages as needed).
    return context


async def load_or_create_session(
    playwright: Playwright,
    force_login: bool = False,
    firefox_profile_dir: str | None = None,
    *,
    fast_warmup: bool = False,
    browser: str = "auto",
) -> BrowserContext:
    browser = (browser or "auto").lower()
    context: BrowserContext | None = None
    browser_label = "firefox"

    if browser in ("auto", "firefox"):
        try:
            context = await _launch_firefox_context(
                playwright,
                firefox_profile_dir,
                fast_warmup=fast_warmup,
                max_attempts=1 if browser == "auto" else 2,
            )
        except PlaywrightError as e:
            if browser == "firefox":
                raise
            logger.warning(
                "Firefox could not start (GPU/D3D11). Falling back to Chromium. %s",
                e,
            )

    if context is None:
        if browser == "firefox":
            raise RuntimeError("Firefox launch failed")
        context = await _launch_chromium_context(playwright)
        browser_label = "chromium"

    return await _warm_up_linkedin_session(
        context,
        force_login=force_login,
        fast_warmup=fast_warmup,
        firefox_profile_dir=firefox_profile_dir,
        browser_label=browser_label,
    )


# ===========================================================================
# SEARCH  —  URL builder + UI-based country/filter selection
# ===========================================================================

def build_base_search_url(combo: dict, page_num: int = 0) -> str:
    """
    Build a LinkedIn company search URL with country (geoUrn), industry,
    and size filters encoded directly in the URL.
    geoUrn with a country-level ID reliably filters to country level.
    """
    size_param     = f'["{combo["size_code"]}"]'
    industry_param = f'["{combo["industry_id"]}"]'
    # geo_id comes from criteria_2_countries.csv via the DB — authoritative source
    geo_urn        = combo.get("geo_id", "") or GEO_URN_MAP.get(combo["country_name"], "")

    parts = [
        "origin=FACETED_SEARCH",
        f"companySize={quote(size_param, safe='[]\"')}",
        f"industryCompanyVertical={quote(industry_param, safe='[]\"')}",
    ]
    if geo_urn:
        geo_param = f'["{geo_urn}"]'
        parts.append(f"companyHqGeo={quote(geo_param, safe='')}") 
    if page_num > 0:
        parts.append(f"start={page_num * 10}")

    return "https://www.linkedin.com/search/results/companies/?" + "&".join(parts)


# ===========================================================================
# BROWSER HELPERS
# ===========================================================================


_api_company_buffer:  list[dict] = []
_api_employee_buffer: list[dict] = []


async def intercept_api_responses(page) -> None:
    """Intercept LinkedIn Voyager API responses to extract bonus data."""
    async def handle_response(response):
        try:
            url = response.url
            if "voyager/api" not in url:
                return
            if response.status != 200:
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            body = await response.json()
            # Company search results
            if "search/blended" in url or "search/cluster" in url:
                elements = (body.get("data", {})
                               .get("elements", []))
                for el in elements:
                    for item in el.get("elements", [el]):
                        try:
                            url_ = item.get("navigationUrl", "")
                            if "/company/" in url_:
                                clean = re.sub(r"\?.*$", "", url_).rstrip("/")
                                if clean.startswith("/"):
                                    clean = "https://www.linkedin.com" + clean
                                _api_company_buffer.append({"linkedin_url": clean})
                        except Exception:
                            pass
            # Employee/people results — company /people/ page
            if "/organization/updatesV2" in url or "memberRelationshipDetails" in url                     or "/identity/profiles" in url or "/voyager/api/search" in url                     or "peopleSearch" in url or "/company/" in url and "employees" in url:
                _parse_voyager_response(body)
        except Exception:
            pass
    page.on("response", handle_response)



# ===========================================================================
# COMPANY DISCOVERY
# ===========================================================================

def _normalize_company_url(href: str) -> str | None:
    if "/company/" not in href:
        return None
    clean = re.sub(r"\?.*$", "", href).rstrip("/")
    if clean.startswith("/"):
        clean = "https://www.linkedin.com" + clean
    if re.fullmatch(r"https://www\.linkedin\.com/company/[^/]+", clean):
        return clean
    return None


async def _collect_urls_from_page(page) -> set:
    """Scrape all top-level /company/ links from the current results page."""
    urls: set = set()
    try:
        await wait_page_settled(page)
        await sleep_rand(*DELAY_SHORT)
        await maybe_mouse_drift(page)
        # Lazy-loaded cards: scroll the results list in passes before collecting.
        prev_count = -1
        for _ in range(8):
            await human_scroll(page, steps=random.randint(3, 5))
            await sleep_rand(0.7, 1.4)
            for link in await page.query_selector_all("a[href*='/company/']"):
                norm = _normalize_company_url(await link.get_attribute("href") or "")
                if norm:
                    urls.add(norm)
            if len(urls) == prev_count and len(urls) > 0:
                break
            prev_count = len(urls)
    except Exception as exc:
        logger.warning("URL collection error: %s", exc)
    return urls


def get_combination_db_stats(conn: sqlite3.Connection, country: str, size_label: str) -> tuple[int, int]:
    """Return best-effort (companies, employees) count in DB for this combination."""
    try:
        size_prefix = size_label.split(" ")[0].replace(",", "")
        cur = conn.execute(
            """
            SELECT c.linkedin_url 
            FROM companies c
            WHERE c.country = ? 
              AND (
                replace(replace(c.company_size, ',', ''), ' ', '') LIKE '%' || ? || '%'
                OR ? LIKE '%' || replace(replace(c.company_size, ',', ''), ' ', '') || '%'
              )
            """,
            (country, size_prefix, size_prefix)
        )
        matching_urls = [r[0] for r in cur.fetchall()]
        if not matching_urls:
            return 0, 0
            
        co_cnt = len(matching_urls)
        
        # Now count employees for these companies
        placeholders = ",".join("?" for _ in matching_urls)
        emp_cur = conn.execute(
            f"SELECT COUNT(*) FROM employees WHERE company_linkedin_url IN ({placeholders})",
            matching_urls
        )
        emp_cnt = emp_cur.fetchone()[0]
        return co_cnt, emp_cnt
    except Exception:
        try:
            cur = conn.execute("SELECT COUNT(*) FROM companies WHERE country = ?", (country,))
            co_cnt = cur.fetchone()[0]
            emp_cur = conn.execute(
                "SELECT COUNT(*) FROM employees WHERE company_linkedin_url IN (SELECT linkedin_url FROM companies WHERE country = ?)",
                (country,)
            )
            emp_cnt = emp_cur.fetchone()[0]
            return co_cnt, emp_cnt
        except Exception:
            return 0, 0


async def discover_companies(
    context,
    conn: sqlite3.Connection,
) -> list:
    """
    Work through search_combinations (in_progress first, then pending).
    Country filter is applied via geoUrn in the URL — no UI interaction needed.
    Returns list of (linkedin_url, country_name) tuples.
    """
    already_scraped = get_scraped_urls(conn)
    logger.info("Companies already in DB: %d (will be skipped)", len(already_scraped))

    results:      list = []
    seen:         set  = set(already_scraped)
    combos_tried: int  = 0

    page = await context.new_page()
    await intercept_api_responses(page)

    while len(results) < MAX_COMPANIES_PER_RUN and combos_tried < MAX_COMBOS_PER_RUN:

        combo = get_next_combination(conn)
        if combo is None:
            logger.info("All search combinations exhausted.")
            break

        combos_tried += 1
        geo_urn = combo.get("geo_id", "") or GEO_URN_MAP.get(combo["country_name"], "")
        if not geo_urn:
            logger.warning("  No companyHqGeo ID for '%s' — skipping.", combo["country_name"])
            update_combination(conn, combo["id"], "exhausted", 0, combo["total_found"])
            continue

        combo_pct = (combos_tried / MAX_COMBOS_PER_RUN) * 100
        company_pct = (len(results) / MAX_COMPANIES_PER_RUN) * 100
        logger.info("We are starting a new search combination of filters.")
        logger.info(
            "Combination [%d] (%d/%d - %.1f%%) | Companies found: %d/%d (%.1f%%): %s | %s | %s  (resume page %d)",
            combo["id"], combos_tried, MAX_COMBOS_PER_RUN, combo_pct,
            len(results), MAX_COMPANIES_PER_RUN, company_pct,
            combo["country_name"], combo["industry_name"],
            combo["size_label"], combo["last_page"],
        )
        update_combination(conn, combo["id"], "in_progress",
                           combo["last_page"], combo["total_found"])

        combo_total = combo["total_found"]
        exhausted   = False
        last_pg     = combo["last_page"]
        added_this_combo = 0
        stopped_for_company_cap = False

        # Navigate to page 0 first
        base_url = build_base_search_url(combo, 0)
        logger.info("  Navigating to: %s", base_url)
        try:
            await page.bring_to_front()   # keep browser foreground → prevent OS timer throttle
            await page.goto(base_url, wait_until="domcontentloaded", timeout=35_000)
            await sleep_rand(*DELAY_MEDIUM, label="post-nav page 0")
        except Exception as exc:
            logger.warning("  Failed to load base URL: %s", exc)
            update_combination(conn, combo["id"], "pending",
                               combo["last_page"], combo_total)
            continue

        # Verify geoUrn survived any redirect
        landed_url = page.url
        if "companyHqGeo" in landed_url:
            logger.info("  ✓ companyHqGeo confirmed in URL.")
        else:
            logger.warning("  ⚠ companyHqGeo missing from URL. Landed: %s", landed_url)

        # Paginate
        _bring_front_every = 3   # call bring_to_front() every N pages
        for pg in range(combo["last_page"], MAX_PAGES_PER_COMBO):
            if len(results) >= MAX_COMPANIES_PER_RUN:
                stopped_for_company_cap = True
                break

            # Keep window in foreground every few pages so Windows can't
            # silently background it between navigations.
            if pg % _bring_front_every == 0:
                try:
                    await page.bring_to_front()
                except Exception:
                    pass

            if pg > 0:
                try:
                    next_url = build_base_search_url(combo, pg)
                    await page.bring_to_front()   # keep browser foreground → prevent OS timer throttle
                    await page.goto(next_url, wait_until="domcontentloaded",
                                    timeout=35_000)
                    await sleep_rand(*DELAY_MEDIUM, label=f"post-nav page {pg}")
                except Exception as exc:
                    logger.warning("  Page %d nav error: %s", pg, exc)
                    await sleep_rand(*DELAY_LONG, label="error-recovery")
                    break

            found = await _collect_urls_from_page(page)

            # Voyager API bonus
            for rec in list(_api_company_buffer):
                if rec.get("linkedin_url"):
                    found.add(rec["linkedin_url"])
            _api_company_buffer.clear()

            if not found:
                logger.info("  Page %d: 0 results — combination exhausted.", pg)
                exhausted = True
                last_pg   = pg
                break

            added = 0
            for u in found:
                if u not in seen and len(results) < MAX_COMPANIES_PER_RUN:
                    seen.add(u)
                    results.append((u, combo["country_name"]))
                    added += 1
                    added_this_combo += 1

            combo_total += len(found)
            last_pg      = pg + 1
            logger.info(
                "  Page %d: %d on page | %d new | %d queued total",
                pg, len(found), added, len(results),
            )
            update_combination(conn, combo["id"], "in_progress",
                               last_pg, combo_total)

            # Stop when this page adds nothing: same company on every page, or all
            # results already in DB — avoids burning ~25s × remaining pages.
            if added == 0:
                logger.info(
                    "  Page %d: 0 new — stopping pagination for this combination.",
                    pg,
                )
                exhausted = True
                break

            await sleep_rand(*DELAY_MEDIUM, label=f"inter-page pg{pg}")

        # Mark combination complete so the next loop picks another row from the queue
        final_status = finalize_combination_status(
            exhausted=exhausted,
            last_page=last_pg,
            added_this_combo=added_this_combo,
            stopped_for_company_cap=stopped_for_company_cap,
        )
        update_combination(conn, combo["id"], final_status, last_pg, combo_total)
        co_db, emp_db = get_combination_db_stats(conn, combo["country_name"], combo["size_label"])
        logger.info("................................................................................")
        logger.info(
            "Combination %s | %s | %s : %d Companies %d employees",
            combo["country_name"], combo["industry_name"], combo["size_label"], co_db, emp_db
        )
        logger.info("................................................................................")

        # Advance to next combination in search_combinations after a finished combo
        will_continue = (
            combos_tried < MAX_COMBOS_PER_RUN
            and len(results) < MAX_COMPANIES_PER_RUN
            and final_status == "exhausted"
        )

        # Delay between combinations (only when moving to the next combo)
        if will_continue:
            if combos_tried % 5 == 0 and combos_tried > 0:
                rest = random.uniform(180.0, 300.0)
                logger.info("  Extended rest %.0f s after %d combinations…", rest, combos_tried)
                await sleep_rand(rest, rest, label="extended-combo-rest")
            else:
                delay = random.uniform(*DELAY_BETWEEN_COMBOS)
                logger.info("  Waiting %.0f s before next combination (anti-block)…", delay)
                await sleep_rand(delay, delay, label="between-combos")

    await page.close()
    logger.info(
        "Discovery done: %d new companies queued after %d combination(s) tried.",
        len(results),
        combos_tried,
    )
    return results[:MAX_COMPANIES_PER_RUN]


# ===========================================================================
# ABOUT SECTION  —  website / domain
# ===========================================================================

async def scrape_about_section(context, company_url: str) -> dict:
    result: dict = {
        "website_raw": "", "company_domain": "", "industry": "",
        "company_size": "", "headquarters": "", "company_type": "",
        "founded": "", "followers": "", "description": "",
    }

    about_url = (re.sub(r"/(about/?|people/?|life/?|jobs/?)?$",
                        "", company_url.rstrip("/")) + "/about/")
    page = await context.new_page()
    try:
        await page.goto(about_url, wait_until="domcontentloaded", timeout=30_000)
        await sleep_rand(*DELAY_SHORT)
        await maybe_mouse_drift(page)
        await human_scroll(page, steps=3)
    except Exception as exc:
        logger.debug("Could not load /about/: %s", exc)
        await page.close()
        return result

    # Website link
    for sel in [
        "[data-test-id='about-us__website'] a",
        "a[data-tracking-control-name='about_website']",
        "a[data-test-id='about-us__website-link']",
        "li.org-page-details__definition a[href*='http']",
    ]:
        try:
            el = await page.wait_for_selector(sel, timeout=4_000)
            if el:
                href = (await el.get_attribute("href") or "").strip()
                href = re.sub(
                    r"https?://www\.linkedin\.com/redir/redirect\?url=([^&]+).*",
                    lambda m: m.group(1), href,
                )
                if href and "linkedin.com" not in href:
                    result["website_raw"] = href
                    logger.info("  Website: %s", href)
                    break
        except Exception:
            pass

    # Overview fields
    for key, field in [
        ("industry", "industry"), ("company-size", "company_size"),
        ("headquarters", "headquarters"), ("type", "company_type"),
        ("founded", "founded"),
    ]:
        try:
            el = await page.query_selector(
                f"[data-test-id='about-us__{key}'] dd, "
                f"[data-test-id='about-us__{key}'] span"
            )
            if el:
                val = (await el.inner_text()).strip()
                if val:
                    result[field] = val
        except Exception:
            pass

    # Legacy dt/dd fallback
    if not result["website_raw"]:
        try:
            for dt in await page.query_selector_all("dt"):
                label = (await dt.inner_text()).strip().lower()
                dd    = await dt.evaluate_handle("el => el.nextElementSibling")
                val   = (await page.evaluate("el => el ? el.innerText : ''", dd)).strip()
                if not val:
                    continue
                if "website" in label and not result["website_raw"]:
                    result["website_raw"] = val
                elif "industry" in label and not result["industry"]:
                    result["industry"] = val
                elif ("size" in label or "employee" in label) and not result["company_size"]:
                    result["company_size"] = val
                elif "headquarter" in label and not result["headquarters"]:
                    result["headquarters"] = val
                elif "type" in label and not result["company_type"]:
                    result["company_type"] = val
                elif "founded" in label and not result["founded"]:
                    result["founded"] = val
        except Exception as exc:
            logger.debug("Legacy dt/dd: %s", exc)

    if result["website_raw"]:
        result["company_domain"] = extract_domain(result["website_raw"])
        logger.info("  Domain: %s", result["company_domain"])
    else:
        logger.warning("  ⚠ No website found.")

    await page.close()
    return result


# ===========================================================================
# COMPANY SCRAPING
# ===========================================================================

async def scrape_company_data(context, company_url: str, country_name: str) -> dict:
    logger.info("Scraping: %s", company_url)
    _api_company_buffer.clear()

    page = await context.new_page()
    await intercept_api_responses(page)

    ts = now_ts()
    company: dict = {
        "linkedin_url":      company_url,
        "company_name":      "",
        "country":           country_name,
        "company_domain":    "",
        "website_raw":       "",
        "industry":          "",
        "company_size":      "",
        "headquarters":      "",
        "company_type":      "",
        "founded":           "",
        "followers":         "",
        "description":       "",
        "scraped_timestamp": ts,
        "updated_timestamp": ts,
    }

    for attempt in range(3):
        try:
            await page.goto(company_url, wait_until="domcontentloaded", timeout=40_000)
            await sleep_rand(*DELAY_MEDIUM)
            await maybe_mouse_drift(page)
            await human_scroll(page, steps=4)
            break
        except Exception as exc:
            logger.warning("Attempt %d: %s", attempt + 1, exc)
            await sleep_rand(*DELAY_LONG)
    else:
        logger.error("Giving up on: %s", company_url)
        await page.close()
        return company

    if _api_company_buffer:
        for k, v in _api_company_buffer[0].items():
            if v and k != "country":
                company[k] = v

    if not company["company_name"]:
        try:
            el = await page.query_selector(
                "h1.org-top-card-summary__title, h1[data-test-id='name']"
            )
            if el:
                company["company_name"] = (await el.inner_text()).strip()
        except Exception:
            pass

    await page.close()

    about = await scrape_about_section(context, company_url)
    for k, v in about.items():
        if v:
            company[k] = v

    company["linkedin_url"]      = company_url
    company["country"]           = country_name
    company["updated_timestamp"] = now_ts()
    return company


# ===========================================================================
# EMPLOYEE DISCOVERY
# ===========================================================================

# ---------------------------------------------------------------------------
# EMPLOYEE FIELD PARSER
# ---------------------------------------------------------------------------
_DEGREE_RE = re.compile(r'^[\uf09d\U0001f539·\s\ufffd]*(1st|2nd|3rd)', re.I)
_CONN_RE   = re.compile(r'degree connection', re.I)
_FOLLOW_RE = re.compile(r'^[\d.,]+\s*[KkMm]?\s*followers', re.I)

def _parse_employee_fields(raw_name: str, raw_title: str) -> tuple[str, str]:
    """
    LinkedIn DOM cards dump the full card text into the title element,
    and store the connection badge (·2nd, ·3rd) as the name.
    This function extracts the real name and job title from the blob.
    """
    is_badge = bool(_DEGREE_RE.match(raw_name.strip()))
    blob_lines = [l.strip() for l in raw_title.split('\n') if l.strip()]

    if is_badge:
        name = ""
        title = ""
        after_degree = False
        for line in blob_lines:
            if not name:
                if not _DEGREE_RE.match(line):
                    name = line
            elif _CONN_RE.search(line) or _DEGREE_RE.match(line):
                after_degree = True
            elif after_degree:
                if not _FOLLOW_RE.match(line):
                    title = line
                    break
        return name, title
    else:
        name = raw_name.strip()
        title = ""
        after_degree = False
        for line in blob_lines:
            if _CONN_RE.search(line) or _DEGREE_RE.match(line):
                after_degree = True
            elif after_degree:
                if not _FOLLOW_RE.match(line):
                    title = line
                    break
        if not title:
            title = blob_lines[0] if blob_lines else ""
        return name, title


async def _scrape_employee_dom(page, company_url: str, company_name: str) -> list:
    employees: list = []
    ts = now_ts()
    try:
        await human_scroll(page, steps=5)
        for card in (await page.query_selector_all(
            "li.org-people-profile-card__profile-card-spacing, "
            "div[data-member-id], li.reusable-search__result-container"
        ))[:MAX_EMPLOYEES_PER_COMPANY]:
            try:
                if len(employees) >= MAX_EMPLOYEES_PER_COMPANY:
                    break
                # Try named class first; avoid aria-hidden='true' which holds
                # LinkedIn connection degree indicators (· 2nd, · 3rd), not names
                name_el = await card.query_selector(
                    "span.org-people-profile-card__profile-title, "
                    "span.entity-result__title-text, "
                    "a.app-aware-link span[aria-hidden='false'], "
                    "span.visually-hidden"
                )
                # Fallback: first span that looks like a real name (has a space, no ·)
                if not name_el:
                    for span in await card.query_selector_all("span"):
                        txt = (await span.inner_text()).strip()
                        if txt and " " in txt and "·" not in txt and len(txt) < 60:
                            name = txt
                            break
                    else:
                        continue
                else:
                    name = (await name_el.inner_text()).strip()
                # Skip connection indicators stored as names
                if not name or "·" in name or name in ("2nd", "3rd", "1st"):
                    continue
                title_el = await card.query_selector(
                    "div.org-people-profile-card__profile-info, "
                    "div.entity-result__primary-subtitle"
                )
                title = (await title_el.inner_text()).strip() if title_el else ""
                # Parse real name and title out of the raw scraped text
                name, title = _parse_employee_fields(name, title)
                if not name:
                    continue
                link_el = await card.query_selector("a[href*='/in/']")
                profile_url = ""
                if link_el:
                    href = await link_el.get_attribute("href") or ""
                    profile_url = re.sub(r"\?.*$", "", href).rstrip("/")
                    if profile_url.startswith("/"):
                        profile_url = "https://www.linkedin.com" + profile_url
                if not profile_url:
                    continue
                employees.append({
                    "profile_url":          profile_url,
                    "employee_name":        name,
                    "job_title":            title,
                    "company_linkedin_url": company_url,
                    "company_name":         company_name,
                    "location":             "",
                    "connection_level":     "",
                    "mutual_connections":   "",
                    "scraped_timestamp":    ts,
                    "updated_timestamp":    ts,
                })
                await sleep_rand(*DELAY_BETWEEN_EMPLOYEE_CARDS)
            except Exception:
                pass
    except Exception as exc:
        logger.debug("DOM employee: %s", exc)
    return employees


def _parse_csv_terms(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def _employee_focus_terms(mode: str, custom_keywords: list[str] | None = None) -> list[str]:
    terms = list(EMPLOYEE_FOCUS_PRESETS.get(mode, []))
    if custom_keywords:
        terms.extend(custom_keywords)
    # preserve order and remove duplicates
    deduped: list[str] = []
    seen: set[str] = set()
    for t in terms:
        key = t.strip().lower()
        if key and key not in seen:
            seen.add(key)
            deduped.append(t.strip())
    return deduped


def _build_people_url(
    company_url: str,
    *,
    mode: str = "neutral",
    custom_keywords: list[str] | None = None,
) -> str:
    base = company_url.rstrip("/") + "/people/"
    terms = _employee_focus_terms(mode, custom_keywords)
    if not terms:
        return base
    # LinkedIn "keywords" on /people is less personalized than default ranking.
    keyword_query = " OR ".join(terms[:8])
    return f"{base}?keywords={quote(keyword_query)}"


def _employee_matches_focus(emp: dict, terms: list[str]) -> bool:
    if not terms:
        return True
    hay = f"{emp.get('employee_name', '')} {emp.get('job_title', '')}".lower()
    return any(t.lower() in hay for t in terms)


def _apply_employee_focus(
    employees: list[dict],
    *,
    mode: str = "neutral",
    custom_keywords: list[str] | None = None,
    strict: bool = False,
) -> list[dict]:
    terms = _employee_focus_terms(mode, custom_keywords)
    if not terms:
        return employees
    matched = [e for e in employees if _employee_matches_focus(e, terms)]
    if strict:
        return matched
    if not matched:
        return employees
    # non-strict: prioritize matching profiles while keeping fallback diversity
    matched_urls = {e.get("profile_url", "") for e in matched}
    remainder = [e for e in employees if e.get("profile_url", "") not in matched_urls]
    return matched + remainder


async def discover_employees(
    context,
    company: dict,
    *,
    focus_mode: str = "neutral",
    custom_keywords: list[str] | None = None,
    strict_focus: bool = False,
) -> list:
    company_name = company.get("company_name", "Unknown")
    company_url  = company.get("linkedin_url", "")
    logger.info("We are now starting to scrape employees for this company.")
    logger.info("  Employees for '%s'…", company_name)
    _api_employee_buffer.clear()

    page = await context.new_page()
    await intercept_api_responses(page)

    people_url = _build_people_url(
        company_url,
        mode=focus_mode,
        custom_keywords=custom_keywords,
    )
    logger.info("  Employee focus: %s", focus_mode)
    if custom_keywords:
        logger.info("  Custom employee keywords: %s", ", ".join(custom_keywords))
    if strict_focus:
        logger.info("  Strict focus filter: ON")

    for attempt in range(3):
        try:
            await page.goto(people_url,
                            wait_until="domcontentloaded", timeout=40_000)
            await sleep_rand(*DELAY_MEDIUM)
            await maybe_mouse_drift(page)
            await human_scroll(page, steps=6)
            break
        except Exception as exc:
            logger.warning("  Attempt %d (people): %s", attempt + 1, exc)
            await sleep_rand(*DELAY_LONG)
    else:
        await page.close()
        return []

    ts = now_ts()
    if _api_employee_buffer:
        employees = []
        for rec in _api_employee_buffer[:MAX_EMPLOYEES_PER_COMPANY]:
            rec["company_linkedin_url"] = company_url
            rec["company_name"]         = company_name
            rec.setdefault("updated_timestamp", ts)
            employees.append(rec)
            await sleep_rand(0.2, 0.65)
        logger.info("  %d employees via API.", len(employees))
    else:
        employees = await _scrape_employee_dom(page, company_url, company_name)
        logger.info("  %d employees via DOM.", len(employees))

    employees = _apply_employee_focus(
        employees,
        mode=focus_mode,
        custom_keywords=custom_keywords,
        strict=strict_focus,
    )
    if focus_mode != "neutral" or custom_keywords:
        logger.info("  %d employees after focus filter.", len(employees))

    await page.close()
    return employees[:MAX_EMPLOYEES_PER_COMPANY]


def save_company(conn: sqlite3.Connection, company: dict) -> None:
    upsert_company(conn, company)
    logger.info("  ✓ %-45s | country: %-15s | domain: %s",
                company.get("company_name", "?"),
                company.get("country", "?"),
                company.get("company_domain") or "— not found —")


def save_employees(conn: sqlite3.Connection, employees: list[dict]) -> None:
    for emp in employees:
        upsert_employee(conn, emp)
        print()
        logger.info("    👤 Employee: %s | Title: %s", emp.get("employee_name", "?"), emp.get("job_title", "?"))
    if employees:
        print()
        logger.info("  ✓ %d employees saved.", len(employees))


# ===========================================================================
# MAIN
# ===========================================================================

def save_scraper_run(
    *,
    started_at: datetime,
    finished_at: datetime,
    elapsed_seconds: float,
    test_mode: bool,
    production_run: bool,
    browser: str,
    companies_saved: int,
    employees_saved: int,
    countries_covered: set[str],
    avg_seconds_per_company: float,
    notes: str = "",
) -> int | None:
    """Persist one scraper execution to scraper_runs."""
    duration_display = _format_run_duration(elapsed_seconds)
    countries_str = ", ".join(sorted(countries_covered)) if countries_covered else ""
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            INSERT INTO scraper_runs (
                started_at, finished_at, duration_seconds, duration_display,
                test_mode, production_run, browser,
                companies_saved, employees_saved, countries_covered,
                avg_seconds_per_company, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_at.strftime("%Y-%m-%d %H:%M:%S"),
                finished_at.strftime("%Y-%m-%d %H:%M:%S"),
                round(elapsed_seconds, 2),
                duration_display,
                int(test_mode),
                int(production_run),
                browser or "",
                companies_saved,
                employees_saved,
                countries_str,
                round(avg_seconds_per_company, 2) if avg_seconds_per_company else None,
                notes,
            ),
        )
        conn.commit()
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        logger.info(
            "Run logged to scraper_runs (id=%s, duration=%s).",
            run_id,
            duration_display,
        )
        return int(run_id)
    except Exception as e:
        logger.warning("Could not save scraper run to database: %s", e)
        return None
    finally:
        if conn is not None:
            conn.close()


def _format_run_duration(seconds: float) -> str:
    """Human-readable duration for run summary logs."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"


# Human-readable labels for v_validation_pipeline_summary columns (key = view column name).
_PIPELINE_SUMMARY_LABELS: dict[str, str] = {
    "total_employee_rows": "All employee rows in database",
    "scrapeable_employees": (
        "Outreach-ready (domain + name) — includes already-contacted employees"
    ),
    "rows_in_employee_email_state": "Employees tracked in email-validation state table",
    "never_in_validation_cycle": "Outreach-ready employees never started in validation cycle",
    "resolved_valid_count": "Employees with at least one confirmed valid email address",
    "still_eligible_for_validation": "Still eligible for another validation / format attempt",
    "eligible_firstname_lastname": "Next format to try: firstname.lastname@domain",
    "eligible_firstname": "Next format to try: firstname@domain",
    "eligible_firstinitial_lastname": "Next format to try: f.lastname@domain (first initial)",
    "eligible_firstname_lastinitial": "Next format to try: firstname.l@domain (last initial)",
    "cascade_exhausted_no_valid": "All format patterns tried — no valid address found",
    "allowlisted_addresses": "Allowlisted addresses (trusted / skip re-validation)",
    "pool_sendable_addresses": "Addresses in send pool (ready for campaign)",
    "email_attempts_total": "Total email_attempts rows (every send + bounce record)",
    "email_attempts_sent": "Attempts with status sent (SMTP accepted)",
    "email_attempts_bounced": "Attempts with status bounced / delivery failed",
}


def _pipeline_summary_label(column_name: str) -> str:
    return _PIPELINE_SUMMARY_LABELS.get(column_name, column_name.replace("_", " "))


def _db_has_view(conn: sqlite3.Connection, view_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name=?",
        (view_name,),
    ).fetchone()
    return row is not None


def _normalize_domain_sql(expr: str) -> str:
    """SQL expression: strip scheme/www from a domain column."""
    return f"""
        lower(
            trim(
                replace(
                    replace(
                        replace(
                            CASE
                                WHEN instr({expr}, '/') > 0
                                THEN substr({expr}, 1, instr({expr}, '/') - 1)
                                ELSE {expr}
                            END,
                            'https://', ''
                        ),
                        'http://', ''
                    ),
                    'www.', ''
                )
            )
        )
    """


def _employee_email_outreach_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """
    Per-employee outreach counts (same scrapeable pool as validation_pipeline_views).

    - reachable_not_sent_successfully: outreach-ready, formats NOT all exhausted,
      and no email_attempts row with status 'sent' for that employee
    - never_emailed_once: subset with zero attempts
    - all_attempts_failed: subset with attempts but zero sent (bounces only, etc.)
    """
    empty = {
        "reachable_not_sent_successfully": 0,
        "never_emailed_once": 0,
        "all_attempts_failed": 0,
    }
    try:
        conn.execute("SELECT 1 FROM email_attempts LIMIT 1")
    except sqlite3.OperationalError:
        return empty

    dom_ea = _normalize_domain_sql("ea.company_domain")

    if _db_has_view(conn, "v_scrapeable_employees"):
        dom_s = "lower(trim(s.company_domain))"
        base_from = """
            FROM v_scrapeable_employees s
            LEFT JOIN v_employee_validation_status v
                ON v.employee_key = s.employee_key
        """
        exhausted_expr = "COALESCE(v.cascade_exhausted_no_valid, 0)"
    else:
        dom_s = _normalize_domain_sql("c.company_domain")
        base_from = """
            FROM employees e
            JOIN companies c ON (
                e.company_name = c.company_name
                OR (
                    COALESCE(trim(e.company_linkedin_url), '') != ''
                    AND e.company_linkedin_url = c.linkedin_url
                )
            )
            LEFT JOIN employee_email_state st
                ON st.employee_key = lower(trim(c.company_name)) || '|' || lower(trim(e.employee_name))
        """
        exhausted_expr = """
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
            END
        """

    if _db_has_view(conn, "v_scrapeable_employees"):
        base_select = f"""
            SELECT DISTINCT
                s.employee_id AS employee_id,
                trim(s.employee_name) AS employee_name,
                {dom_s} AS company_domain,
                {exhausted_expr} AS cascade_exhausted_no_valid
            {base_from}
        """
    else:
        base_select = f"""
            SELECT DISTINCT
                e.id AS employee_id,
                trim(e.employee_name) AS employee_name,
                {dom_s} AS company_domain,
                {exhausted_expr} AS cascade_exhausted_no_valid
            {base_from}
            WHERE c.company_domain IS NOT NULL
              AND trim(c.company_domain) != ''
              AND e.employee_name IS NOT NULL
              AND length(trim(e.employee_name)) > 3
              AND instr(trim(e.employee_name), ' ') > 0
              AND e.employee_name NOT LIKE '%·%'
              AND e.employee_name NOT LIKE '% 2nd%'
              AND e.employee_name NOT LIKE '% 3rd%'
        """

    row = conn.execute(
        f"""
        WITH base AS (
            {base_select}
        ),
        per_employee AS (
            SELECT
                b.employee_id,
                b.cascade_exhausted_no_valid,
                COUNT(ea.id) AS attempt_count,
                SUM(
                    CASE WHEN lower(COALESCE(ea.status, '')) = 'sent' THEN 1 ELSE 0 END
                ) AS sent_count
            FROM base b
            LEFT JOIN email_attempts ea ON (
                {dom_ea} = b.company_domain
                AND trim(ea.employee_name) = b.employee_name
            )
            GROUP BY b.employee_id, b.cascade_exhausted_no_valid
        )
        SELECT
            SUM(
                CASE
                    WHEN cascade_exhausted_no_valid = 0 AND sent_count = 0 THEN 1
                    ELSE 0
                END
            ),
            SUM(
                CASE
                    WHEN cascade_exhausted_no_valid = 0 AND attempt_count = 0 THEN 1
                    ELSE 0
                END
            ),
            SUM(
                CASE
                    WHEN cascade_exhausted_no_valid = 0
                     AND attempt_count > 0
                     AND sent_count = 0 THEN 1
                    ELSE 0
                END
            )
        FROM per_employee
        """
    ).fetchone()
    if not row:
        return empty
    return {
        "reachable_not_sent_successfully": int(row[0] or 0),
        "never_emailed_once": int(row[1] or 0),
        "all_attempts_failed": int(row[2] or 0),
    }


def print_db_summary_to_logger() -> None:
    try:
        import os
        if not os.path.exists(DB_FILE):
            return
        conn = sqlite3.connect(DB_FILE)
        row = conn.execute("SELECT * FROM v_validation_pipeline_summary").fetchone()
        if not row:
            conn.close()
            return
        names = [d[0] for d in conn.execute("SELECT * FROM v_validation_pipeline_summary").description]
        outreach = _employee_email_outreach_stats(conn)
        logger.info("")
        logger.info("=" * 72)
        logger.info("  EMAIL / VALIDATION PIPELINE SUMMARY")
        logger.info("=" * 72)
        logger.info(
            "  %-55s: %s",
            ">>> STILL REACHABLE: no successful send, formats not all exhausted",
            f"{outreach['reachable_not_sent_successfully']:,}",
        )
        logger.info(
            "       (never emailed + bounce-only; excludes all formats exhausted)"
        )
        logger.info("")
        logger.info("  (detail from view v_validation_pipeline_summary)")
        logger.info("")
        for name, val in zip(names, row):
            label = _pipeline_summary_label(name)
            if isinstance(val, int):
                logger.info("  %-55s: %s", label, f"{val:,}")
            else:
                logger.info("  %-55s: %s", label, val)

        logger.info("")
        logger.info("  --- Per-employee outreach breakdown ---")
        logger.info(
            "  %-55s: %s",
            "Never emailed — not even one attempt",
            f"{outreach['never_emailed_once']:,}",
        )
        logger.info(
            "  %-55s: %s",
            "Emailed before but every attempt failed (0 sent, has bounce/attempt rows)",
            f"{outreach['all_attempts_failed']:,}",
        )
        logger.info("=" * 72)
        conn.close()
    except Exception as e:
        logger.warning("Could not print database validation summary: %s", e)


def _count_emails_in_db(conn: sqlite3.Connection) -> int:
    """
    Return the total number of rows in email_attempts.
    Returns 0 if the table does not yet exist (first run before any campaign).
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM email_attempts"
        ).fetchone()
        return row[0] if row else 0
    except sqlite3.OperationalError:
        # Table hasn't been created yet by send_linkedin_campaigns.py
        return 0


def parse_cli_args() -> argparse.Namespace:
    """--run enables production caps; default (no args) is test mode."""
    argv = []
    for a in sys.argv[1:]:
        a_lower = a.lower()
        if a_lower in ("--run", "--production", "--full"):
            argv.append("--run")
        elif a_lower in ("--login", "--switch"):
            argv.append("--login")
        else:
            argv.append(a)

    parser = argparse.ArgumentParser(
        description="LinkedIn company/employee scraper (Playwright + SQLite).",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help=(
            "Production run: normal company/employee limits and search queue. "
            "Also revisits companies already in DB that have zero employees. "
            "Default without this flag: test mode (1 company, 1 employee)."
        ),
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Force logout of the current session and show the login page to switch accounts.",
    )
    parser.add_argument(
        "--firefox-profile",
        metavar="PATH",
        default=None,
        help=(
            "Firefox profile folder to copy cookies from (must contain cookies.sqlite). "
            "Default: FIREFOX_PROFILE_DIR in linkedin_scraper.py. "
            "Create a dedicated profile in Firefox Profile Manager for a separate LinkedIn account."
        ),
    )
    parser.add_argument(
        "--browser",
        choices=("auto", "firefox", "chromium"),
        default="chromium",
        help=(
            "Browser window: chromium (default, reliable on Windows) or firefox. "
            "auto tries Firefox once then Chromium. Cookies always come from your "
            "Firefox 'scraper' profile / linkedin_state.json."
        ),
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Print email/validation pipeline summary from DB and exit (no browser).",
    )
    parser.add_argument(
        "--employee-focus",
        choices=("neutral", "hr", "top-management"),
        default="neutral",
        help=(
            "Employee targeting mode on company /people pages. "
            "neutral=unbiased broad scrape, hr=people/talent roles, "
            "top-management=leadership/decision-makers."
        ),
    )
    parser.add_argument(
        "--employee-keywords",
        default="",
        help=(
            "Optional extra employee keywords (comma-separated). "
            "Example: --employee-keywords \"CHRO, Talent Director, HRBP\""
        ),
    )
    parser.add_argument(
        "--strict-employee-focus",
        action="store_true",
        help=(
            "Keep only employees matching focus keywords. "
            "Default keeps keyword matches first, then fallback profiles."
        ),
    )
    parser.add_argument(
        "--no-beep",
        action="store_true",
        help="Disable all beeps (success/error/company-change/done).",
    )
    return parser.parse_args(argv)


def apply_test_mode_limits() -> None:
    """Shrink run caps for a fast end-to-end smoke test."""
    global MAX_COMBOS_PER_RUN, MAX_COMPANIES_PER_RUN, MAX_PAGES_PER_COMBO
    global MAX_EMPLOYEES_PER_COMPANY, TEST_MODE
    TEST_MODE = True
    MAX_COMBOS_PER_RUN = 1
    MAX_COMPANIES_PER_RUN = 1
    MAX_PAGES_PER_COMBO = 1
    MAX_EMPLOYEES_PER_COMPANY = 1
    logger.info(
        "TEST MODE: max %d combo, %d company, %d employee, %d search page per combo.",
        MAX_COMBOS_PER_RUN,
        MAX_COMPANIES_PER_RUN,
        MAX_EMPLOYEES_PER_COMPANY,
        MAX_PAGES_PER_COMBO,
    )


async def mouse_mover_loop() -> None:
    """
    Simulates a small relative mouse movement (1 pixel back and forth) every 30 seconds
    to prevent Windows from entering standby/sleep due to user inactivity.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        logger.info("Setting Windows thread execution state to prevent sleep + mouse movement simulation...")
        while True:
            await asyncio.sleep(30)
            try:
                pt = POINT()
                if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
                    # Move cursor 1 pixel to the right, then back
                    ctypes.windll.user32.SetCursorPos(pt.x + 1, pt.y)
                    ctypes.windll.user32.SetCursorPos(pt.x, pt.y)
            except Exception:
                pass
    except asyncio.CancelledError:
        logger.info("Stopped background mouse movement task.")
    except Exception as e:
        logger.warning("Background mouse mover encountered error: %s", e)


async def main(cli_args: argparse.Namespace | None = None) -> None:
    cli_args = cli_args or parse_cli_args()
    logger.info("=== LinkedIn Scraper v4.2 ===")
    if TEST_MODE:
        logger.info("  (test mode — default limits; use --run for production)")

    wall_start = datetime.now()
    perf_start = time.perf_counter()
    ts_fmt = "%Y-%m-%d %H:%M:%S"
    
    companies_saved = 0
    employees_saved = 0
    countries_covered: set[str] = set()
    company_processing_times: list[float] = []
    emails_start = 0   # snapshot before run; delta = emails added this session
    run_notes = ""

    # Start background mouse mover task to reset idle timer
    mouse_task = asyncio.create_task(mouse_mover_loop())
    conn: sqlite3.Connection | None = None

    try:
        conn = init_db(DB_FILE)
        seed_criteria(conn)        # populate / update the 3 reference tables
        emails_start = _count_emails_in_db(conn)

        # ── Reset pending combinations so sort_order changes take effect ──────
        # Pending rows are cheap to rebuild; in_progress and exhausted are kept
        # so no real progress is ever lost.
        pending = conn.execute(
            "SELECT COUNT(*) FROM search_combinations WHERE status='pending'"
        ).fetchone()[0]
        conn.execute("DELETE FROM search_combinations WHERE status='pending'")
        conn.commit()
        if pending:
            logger.info(
                "Cleared %d pending combinations — rebuilding with current sort order.",
                pending,
            )

        build_combinations(conn)   # re-insert pending rows in correct sort order
        recover_stale_combinations(conn)
        if not verify_search_combinations_table(conn):
            run_notes = "aborted: search_combinations table invalid"
            return

        logger.info(
            "Browser engine: %s (session cookies from Firefox profile / %s)",
            cli_args.browser,
            SESSION_FILE,
        )
        if cli_args.firefox_profile:
            logger.info("Using Firefox cookie profile: %s", cli_args.firefox_profile)
        async with async_playwright() as pw:
            context = await load_or_create_session(
                pw,
                force_login=cli_args.login,
                firefox_profile_dir=cli_args.firefox_profile,
                fast_warmup=TEST_MODE,
                browser=cli_args.browser,
            )
            try:
                # Returns list of (url, country_name)
                discoveries = await discover_companies(context, conn)

                if cli_args.run:
                    seen_urls = {u for u, _ in discoveries}
                    room = max(0, MAX_COMPANIES_PER_RUN - len(discoveries))
                    backfill = get_companies_with_zero_employees(conn, room)
                    added = 0
                    for url, country in backfill:
                        if url not in seen_urls:
                            discoveries.append((url, country))
                            seen_urls.add(url)
                            added += 1
                    if added:
                        logger.info(
                            "Backfill: queued %d companies already in DB with 0 employees "
                            "(fresh account /people/ retry).",
                            added,
                        )

                if not discoveries:
                    verify_search_combinations_table(conn)
                    run_notes = "no new companies queued"
                    logger.warning(
                        "No new companies found this run.\n"
                        "The script tried up to %d combination(s) from "
                        "search_combinations; all URLs may already be in the DB, "
                        "or every combo is exhausted. Activate more criteria in "
                        "criteria_* tables and run build_combinations(), or reset "
                        "exhausted rows to pending to re-scan.",
                        MAX_COMBOS_PER_RUN,
                    )
                    return

                custom_employee_keywords = _parse_csv_terms(cli_args.employee_keywords)

                for idx, (url, country_name) in enumerate(discoveries, 1):
                    print()
                    pct = (idx / len(discoveries)) * 100
                    if idx > 1:
                        logger.info("We are now moving from the previous company to the next one.")
                        beep_company_change()
                    logger.info("─── %d / %d (%.1f%%) ───", idx, len(discoveries), pct)
                    company_start_time = time.perf_counter()
                    try:
                        company   = await scrape_company_data(context, url, country_name)
                        save_company(conn, company)
                        print()
                        employees = []
                        if company.get("website_raw"):
                            employees = await discover_employees(
                                context,
                                company,
                                focus_mode=cli_args.employee_focus,
                                custom_keywords=custom_employee_keywords,
                                strict_focus=cli_args.strict_employee_focus,
                            )
                            save_employees(conn, employees)
                        else:
                            logger.info("  Skipping employee scraping - no website found.")
                        beep_ok()   # 🔔 short beep — company + employees saved

                        companies_saved += 1
                        employees_saved += len(employees)
                        if country_name:
                            countries_covered.add(country_name)

                        company_duration = time.perf_counter() - company_start_time
                        company_processing_times.append(company_duration)

                        if TEST_MODE:
                            logger.info("TEST MODE: stopping after 1 company.")
                            break

                        await sleep_rand(*DELAY_BETWEEN_COMPANIES, label="between-companies")
                        if random.random() < 0.22:
                            await sleep_rand(2.0, 10.0, label="company-jitter")
                    except Exception as exc:
                        logger.error("Error on %s: %s", url, exc, exc_info=True)
                        beep_error()   # 🚨 low beep — company failed
                        await sleep_rand(*DELAY_LONG, label="company-error-recovery")
                        if TEST_MODE:
                            break

            finally:
                await context.storage_state(path=SESSION_FILE)
                await context.close()

        export_all_csv(conn)
        conn.close()
        logger.info("=== Done ===")

    finally:
        mouse_task.cancel()
        try:
            await mouse_task
        except asyncio.CancelledError:
            pass
        wall_end = datetime.now()
        elapsed_s = time.perf_counter() - perf_start
        duration_display = _format_run_duration(elapsed_s)
        avg_time_per_company = (sum(company_processing_times) / len(company_processing_times)) if company_processing_times else 0.0

        run_id = save_scraper_run(
            started_at=wall_start,
            finished_at=wall_end,
            elapsed_seconds=elapsed_s,
            test_mode=TEST_MODE,
            production_run=bool(cli_args.run),
            browser=cli_args.browser,
            companies_saved=companies_saved,
            employees_saved=employees_saved,
            countries_covered=countries_covered,
            avg_seconds_per_company=avg_time_per_company,
            notes=run_notes,
        )

        print()
        print("=" * 60)
        print(f"  TOTAL EXECUTION TIME : {duration_display} ({elapsed_s:.1f} s)")
        if run_id is not None:
            print(f"  Saved to database    : scraper_runs id={run_id}")
        print("=" * 60)
        print()

        logger.info("=== RUN SUMMARY ===")
        logger.info("  Start time      : %s", wall_start.strftime(ts_fmt))
        logger.info("  End time        : %s", wall_end.strftime(ts_fmt))
        logger.info(
            "  Total duration  : %s (%.1f seconds)",
            duration_display,
            elapsed_s,
        )
        if run_id is not None:
            logger.info("  Run id (DB)     : %d  (table: scraper_runs)", run_id)
        logger.info("  Companies saved : %d", companies_saved)
        logger.info("  Employees saved : %d", employees_saved)
        logger.info(
            "  Time / company  : %s (%.1f seconds)",
            _format_run_duration(avg_time_per_company),
            avg_time_per_company,
        )
        logger.info(
            "  Countries cvrd  : %d (%s)",
            len(countries_covered),
            ", ".join(sorted(countries_covered)) if countries_covered else "None"
        )
        # email_attempts is written by send_linkedin_campaigns.py, not this
        # scraper — so we report the total in DB plus any delta from this session.
        try:
            emails_total = _count_emails_in_db(conn)
            emails_added = emails_total - emails_start
            logger.info(
                "  Emails added    : %d  (total in DB: %d)",
                emails_added, emails_total,
            )
        except Exception:
            pass  # conn already closed — skip gracefully

        print_db_summary_to_logger()
        beep_done()   # 🔔🔔🔔 rising triple beep — run fully complete


if __name__ == "__main__":
    _cli = parse_cli_args()
    BEEPS_ENABLED = not _cli.no_beep
    _custom_kw = _parse_csv_terms(_cli.employee_keywords)
    logger.info(
        "Employee focus mode: %s%s%s",
        _cli.employee_focus,
        " + custom keywords" if _custom_kw else "",
        " (strict)" if _cli.strict_employee_focus else "",
    )
    if _custom_kw:
        logger.info("Employee focus keywords: %s", ", ".join(_custom_kw))
    if _cli.summary_only:
        print_db_summary_to_logger()
        sys.exit(0)
    if not _cli.run:
        apply_test_mode_limits()
    else:
        logger.info(
            "PRODUCTION MODE (--run): max %d companies, %d employees/company, %d combos.",
            MAX_COMPANIES_PER_RUN,
            MAX_EMPLOYEES_PER_COMPANY,
            MAX_COMBOS_PER_RUN,
        )
    with prevent_windows_sleep():
        try:
            asyncio.run(main(_cli))
        except PlaywrightError as e:
            logger.error(
                "Browser could not start. Close stuck firefox.exe / chrome.exe, "
                "or run with: --browser chromium\n%s",
                e,
            )
            sys.exit(1)
