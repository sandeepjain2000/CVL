"""
linkedin_scraper.py  –  v4.0
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
  python linkedin_scraper.py
"""

import asyncio
import csv
import logging
import random
import re
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator
from urllib.parse import urlparse, quote

from playwright.async_api import (
    async_playwright,
    BrowserContext,
    Page,
    Response,
    Playwright,
)

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("linkedin_scraper.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FILE PATHS
# ---------------------------------------------------------------------------
SESSION_FILE    = "linkedin_state.json"
DB_FILE         = "linkedin_data.db"

# Your REAL Firefox profile — cookies are read from here (never written back)
FIREFOX_PROFILE_DIR    = (
    r"C:\Users\sandeep\AppData\Roaming\Mozilla\Firefox\Profiles"
    r"\binejpxk.default-release"
)
# Throwaway profile Playwright owns entirely — recreated fresh every run
PLAYWRIGHT_PROFILE_DIR = r"C:\Users\sandeep\AppData\Local\linkedin_scraper_profile"

# ---------------------------------------------------------------------------
# SCRAPING BEHAVIOUR  —  tune these to balance speed vs. block risk
# ---------------------------------------------------------------------------
DELAY_SHORT  = (2.0, 4.0)    # between UI actions within a page
DELAY_MEDIUM = (4.0, 8.0)    # between pages within one combination
DELAY_LONG   = (8.0, 15.0)   # after errors / retries

# Delay BETWEEN search combinations — critical for avoiding blocks.
# LinkedIn will rate-limit if you hammer many different filter combos
# in quick succession.  Keep this at 30-60 s minimum in production.
DELAY_BETWEEN_COMBOS = (60.0, 120.0)  # seconds — longer gap reduces bot detection

# How many search combinations to attempt per run.
# Each combination = one (country × industry × size) trio.
# Keep low (1-3) while testing; raise to 5-10 for production runs.
MAX_COMBOS_PER_RUN  = 20

# How many NEW companies to collect before stopping for this run.
MAX_COMPANIES_PER_RUN = 50     # ← raise for production (e.g. 50 or 200)

# How many LinkedIn result pages to fetch per combination (10 results/page).
# 10 pages = up to 100 companies per combination.
MAX_PAGES_PER_COMBO   = 10

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

CRITERIA_DIR = Path(__file__).parent   # same folder as this script

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


async def sleep_rand(low: float, high: float) -> None:
    await asyncio.sleep(random.uniform(low, high))


async def human_scroll(page: Page, steps: int = 5) -> None:
    for _ in range(steps):
        await page.mouse.wheel(0, random.randint(200, 600))
        await asyncio.sleep(random.uniform(0.3, 0.9))


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

-- ── INDEXES ───────────────────────────────────────────────────────────────
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

    for table, fields, path in exports:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
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

def _prepare_cookie_only_profile() -> str:
    """
    Copy ONLY cookies.sqlite from the real Firefox profile into a fresh
    throwaway directory. Avoids the 'older Firefox version' warning because
    the throwaway profile has no version stamp.
    """
    src  = Path(FIREFOX_PROFILE_DIR)
    dest = Path(PLAYWRIGHT_PROFILE_DIR)
    src_cookies = src / "cookies.sqlite"

    if not src.exists():
        raise FileNotFoundError(
            f"Firefox profile not found:\n  {FIREFOX_PROFILE_DIR}"
        )
    if not src_cookies.exists():
        raise FileNotFoundError(
            f"cookies.sqlite missing from {FIREFOX_PROFILE_DIR}\n"
            "Log into LinkedIn in your real Firefox first."
        )
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    shutil.copy2(src_cookies, dest / "cookies.sqlite")
    logger.info("Cookies copied → %s", dest)
    return str(dest)


async def load_or_create_session(playwright: Playwright) -> BrowserContext:
    profile_dir = _prepare_cookie_only_profile()
    context = await playwright.firefox.launch_persistent_context(
        user_data_dir=profile_dir,
        headless=False,
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        firefox_user_prefs={
            # Match the actual Firefox version being used (146)
            "general.useragent.override": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:146.0) "
                "Gecko/20100101 Firefox/146.0"
            ),
            "browser.startup.homepage_override.mstone": "ignore",
            "startup.homepage_welcome_url": "",
            "startup.homepage_welcome_url.additional": "",
            # Reduce automation fingerprinting
            "privacy.resistFingerprinting": False,
            "dom.webdriver.enabled": False,
        },
    )
    page = await context.new_page()
    await page.goto("https://www.linkedin.com/feed/",
                    wait_until="domcontentloaded", timeout=30_000)
    await sleep_rand(*DELAY_SHORT)

    if "login" in page.url or "authwall" in page.url:
        logger.warning("Cookies expired — please log in manually.")
        input("\n>>> Log in to LinkedIn, then press ENTER <<<\n")
        await context.storage_state(path=SESSION_FILE)
    else:
        logger.info("LinkedIn session active — no login needed.")

    await page.close()
    return context


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

async def _collect_urls_from_page(page) -> set:
    """Scrape all top-level /company/ links from the current results page."""
    urls: set = set()
    try:
        await human_scroll(page, steps=4)
        for link in await page.query_selector_all("a[href*='/company/']"):
            href = await link.get_attribute("href") or ""
            if "/company/" in href:
                clean = re.sub(r"\?.*$", "", href).rstrip("/")
                if clean.startswith("/"):
                    clean = "https://www.linkedin.com" + clean
                if re.fullmatch(r"https://www\.linkedin\.com/company/[^/]+", clean):
                    urls.add(clean)
    except Exception as exc:
        logger.warning("URL collection error: %s", exc)
    return urls


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

        logger.info(
            "Combination [%d] (%d/%d): %s | %s | %s  (resume page %d)",
            combo["id"], combos_tried, MAX_COMBOS_PER_RUN,
            combo["country_name"], combo["industry_name"],
            combo["size_label"], combo["last_page"],
        )
        update_combination(conn, combo["id"], "in_progress",
                           combo["last_page"], combo["total_found"])

        combo_total = combo["total_found"]
        exhausted   = False
        last_pg     = combo["last_page"]

        # Navigate to page 0 first
        base_url = build_base_search_url(combo, 0)
        logger.info("  Navigating to: %s", base_url)
        try:
            await page.goto(base_url, wait_until="domcontentloaded", timeout=35_000)
            await sleep_rand(*DELAY_MEDIUM)
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
        for pg in range(combo["last_page"], MAX_PAGES_PER_COMBO):
            if len(results) >= MAX_COMPANIES_PER_RUN:
                break

            if pg > 0:
                try:
                    next_url = build_base_search_url(combo, pg)
                    await page.goto(next_url, wait_until="domcontentloaded",
                                    timeout=35_000)
                    await sleep_rand(*DELAY_MEDIUM)
                except Exception as exc:
                    logger.warning("  Page %d nav error: %s", pg, exc)
                    await sleep_rand(*DELAY_LONG)
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

            combo_total += len(found)
            last_pg      = pg + 1
            logger.info(
                "  Page %d: %d on page | %d new | %d queued total",
                pg, len(found), added, len(results),
            )
            update_combination(conn, combo["id"], "in_progress",
                               last_pg, combo_total)
            await sleep_rand(*DELAY_MEDIUM)

        # Mark final status
        if exhausted or last_pg >= MAX_PAGES_PER_COMBO:
            final_status = "exhausted"
        else:
            final_status = "in_progress"
        update_combination(conn, combo["id"], final_status, last_pg, combo_total)
        logger.info("  Combination [%d] → %s", combo["id"], final_status)

        # Delay between combinations
        if combos_tried < MAX_COMBOS_PER_RUN and len(results) < MAX_COMPANIES_PER_RUN:
            if combos_tried % 5 == 0 and combos_tried > 0:
                rest = random.uniform(180.0, 300.0)
                logger.info("  Extended rest %.0f s after %d combinations…", rest, combos_tried)
                await asyncio.sleep(rest)
            else:
                delay = random.uniform(*DELAY_BETWEEN_COMBOS)
                logger.info("  Waiting %.0f s before next combination (anti-block)…", delay)
                await asyncio.sleep(delay)

    await page.close()
    logger.info("Discovery done: %d new companies queued.", len(results))
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
        ))[:10]:
            try:
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
            except Exception:
                pass
    except Exception as exc:
        logger.debug("DOM employee: %s", exc)
    return employees


async def discover_employees(context, company: dict) -> list:
    company_name = company.get("company_name", "Unknown")
    company_url  = company.get("linkedin_url", "")
    logger.info("  Employees for '%s'…", company_name)
    _api_employee_buffer.clear()

    page = await context.new_page()
    await intercept_api_responses(page)

    for attempt in range(3):
        try:
            await page.goto(company_url.rstrip("/") + "/people/",
                            wait_until="domcontentloaded", timeout=40_000)
            await sleep_rand(*DELAY_MEDIUM)
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
        for rec in _api_employee_buffer[:10]:
            rec["company_linkedin_url"] = company_url
            rec["company_name"]         = company_name
            rec.setdefault("updated_timestamp", ts)
            employees.append(rec)
        logger.info("  %d employees via API.", len(employees))
    else:
        employees = await _scrape_employee_dom(page, company_url, company_name)
        logger.info("  %d employees via DOM.", len(employees))

    await page.close()
    return employees


def save_company(conn: sqlite3.Connection, company: dict) -> None:
    upsert_company(conn, company)
    logger.info("  ✓ %-45s | country: %-15s | domain: %s",
                company.get("company_name", "?"),
                company.get("country", "?"),
                company.get("company_domain") or "— not found —")


def save_employees(conn: sqlite3.Connection, employees: list[dict]) -> None:
    for emp in employees:
        upsert_employee(conn, emp)
    if employees:
        logger.info("  ✓ %d employees saved.", len(employees))


# ===========================================================================
# MAIN
# ===========================================================================

async def main() -> None:
    logger.info("=== LinkedIn Scraper v4.1 ===")

    conn = init_db(DB_FILE)
    seed_criteria(conn)        # populate / update the 3 reference tables

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

    # Summary of queue state
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM search_combinations GROUP BY status"
    ).fetchall()
    logger.info("Queue: %s", " | ".join(f"{r[0]}: {r[1]}" for r in rows))

    async with async_playwright() as pw:
        context = await load_or_create_session(pw)
        try:
            # Returns list of (url, country_name)
            discoveries = await discover_companies(context, conn)

            if not discoveries:
                logger.warning(
                    "No new companies found.\n"
                    "All combinations may be exhausted, or try activating "
                    "more criteria in the DB reference tables."
                )
                return

            for idx, (url, country_name) in enumerate(discoveries, 1):
                logger.info("─── %d / %d ───", idx, len(discoveries))
                try:
                    company   = await scrape_company_data(context, url, country_name)
                    employees = await discover_employees(context, company)
                    save_company(conn, company)
                    save_employees(conn, employees)
                    await sleep_rand(*DELAY_LONG)
                except Exception as exc:
                    logger.error("Error on %s: %s", url, exc, exc_info=True)
                    await sleep_rand(*DELAY_LONG)

        finally:
            await context.storage_state(path=SESSION_FILE)
            await context.close()

    export_all_csv(conn)
    conn.close()
    logger.info("=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
