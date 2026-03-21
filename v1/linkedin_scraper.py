"""
linkedin_scraper.py
====================
Production-ready LinkedIn scraper using Playwright (Firefox) with:
  - Session reuse via storage_state
  - Voyager API interception (preferred over DOM scraping)
  - Company discovery via search + job listings
  - Employee discovery per company
  - CSV + JSON output
  - Anti-blocking: randomised delays, human-like scrolling

Usage
-----
  pip install playwright
  playwright install firefox

  # First run – opens browser for manual login, then saves session
  python linkedin_scraper.py

  # Subsequent runs – reuses saved linkedin_state.json
  python linkedin_scraper.py
"""

import asyncio
import csv
import json
import logging
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlencode, quote_plus

from playwright.async_api import (
    async_playwright,
    Browser,
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
# CONFIGURATION  (edit search criteria here)
# ---------------------------------------------------------------------------
SEARCH_CONFIG: dict[str, Any] = {
    "country": "Austria",
    "industry": "Software Development",
    "company_size": "11-200",
    "keywords": ["saas", "cloud", "ai", "software"],
    "job_titles_for_discovery": [
        "software engineer",
        "python developer",
        "data engineer",
    ],
    "search_strategy": ["linkedin_company_search", "linkedin_job_listings"],
    "max_pages": 10,
    # ---- testing cap ----
    "max_companies": 5,   # set to None to remove limit
}

# Firefox profile path from the user's machine (mapped into the container)
# When running locally on Windows this path must be reachable by the machine
# executing the script.  Adjust if necessary.
FIREFOX_PROFILE_PATH = (
    r"C:\Users\sandeep\AppData\Roaming\Mozilla\Firefox\Profiles"
    r"\binejpxk.default-release"
)

SESSION_FILE = "linkedin_state.json"
COMPANIES_CSV = "companies.csv"
EMPLOYEES_CSV = "employees.csv"
COMPANIES_JSON = "companies.json"
EMPLOYEES_JSON = "employees.json"

# Delay ranges (seconds) – keep human-like
DELAY_SHORT = (1.0, 2.5)
DELAY_MEDIUM = (2.5, 5.0)
DELAY_LONG = (5.0, 10.0)

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def rand_delay(low: float, high: float) -> float:
    return random.uniform(low, high)


async def sleep_rand(low: float, high: float) -> None:
    await asyncio.sleep(rand_delay(low, high))


async def human_scroll(page: Page, steps: int = 5) -> None:
    """Scroll down gradually to trigger lazy-loading."""
    for _ in range(steps):
        delta = random.randint(200, 600)
        await page.mouse.wheel(0, delta)
        await asyncio.sleep(rand_delay(0.3, 0.9))


def extract_domain(url: str) -> str:
    """Return bare domain from a URL string, e.g. 'company.com'."""
    if not url:
        return ""
    try:
        parsed = urlparse(url if url.startswith("http") else f"https://{url}")
        domain = parsed.netloc or parsed.path
        return domain.lstrip("www.").split("/")[0].strip()
    except Exception:
        return url


def now_ts() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


# ---------------------------------------------------------------------------
# BROWSER INITIALISATION
# ---------------------------------------------------------------------------

async def initialize_browser(playwright: Playwright) -> tuple[Browser, BrowserContext]:
    """
    Launch Firefox in headful mode.
    The Firefox user-data-dir is specified so LinkedIn sees a consistent
    browser fingerprint; Playwright session state is layered on top.
    """
    logger.info("Launching Firefox browser…")
    browser = await playwright.firefox.launch(
        headless=False,          # headful – easier for manual login + less detection
        firefox_user_prefs={
            "general.useragent.override": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) "
                "Gecko/20100101 Firefox/124.0"
            ),
        },
    )
    logger.info("Firefox launched.")
    return browser


async def load_or_create_session(browser: Browser) -> BrowserContext:
    """
    Return a BrowserContext with an authenticated LinkedIn session.

    Priority:
      1. Load existing linkedin_state.json (storage_state).
      2. If not found, open a page for manual login and save afterwards.
    """
    if Path(SESSION_FILE).exists():
        logger.info("Found existing session file '%s' – reusing it.", SESSION_FILE)
        context = await browser.new_context(
            storage_state=SESSION_FILE,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        return context

    # No saved session – let the user log in manually
    logger.info(
        "No session file found. Opening LinkedIn for manual login.\n"
        "Please log in, then press ENTER in this terminal to continue."
    )
    context = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        locale="en-US",
    )
    page = await context.new_page()
    await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

    # Wait for the user to complete login
    input("\n>>> Log in to LinkedIn in the browser window, then press ENTER here <<<\n")

    # Persist the session so future runs skip this step
    await context.storage_state(path=SESSION_FILE)
    logger.info("Session saved to '%s'.", SESSION_FILE)
    await page.close()
    return context


# ---------------------------------------------------------------------------
# API INTERCEPTION
# ---------------------------------------------------------------------------

# Shared buffers populated by the response handler
_api_company_buffer: list[dict] = []
_api_employee_buffer: list[dict] = []


def _parse_voyager_response(url: str, body: dict) -> None:
    """
    Parse a voyager API JSON response and push recognised records into
    the shared buffers.  This runs synchronously inside the async handler.
    """
    # ---- Company / organisation results ----
    elements = (
        body.get("elements")
        or body.get("data", {}).get("elements")
        or []
    )

    for el in elements:
        # Company search result shape
        if "company" in el or el.get("type") in ("COMPANY", "company"):
            company_node = el.get("company") or el
            record = _extract_company_from_api_node(company_node)
            if record:
                _api_company_buffer.append(record)

        # People / employee search result shape
        if "profile" in el or el.get("type") in ("PROFILE", "profile"):
            profile_node = el.get("profile") or el
            record = _extract_employee_from_api_node(profile_node)
            if record:
                _api_employee_buffer.append(record)

    # Top-level company detail page response
    if "companyPageUrl" in body or "universalName" in body:
        record = _extract_company_from_api_node(body)
        if record:
            _api_company_buffer.append(record)


def _extract_company_from_api_node(node: dict) -> dict | None:
    """Map a raw API node to a normalised company dict."""
    name = (
        node.get("name")
        or node.get("localizedName")
        or node.get("companyName")
        or ""
    )
    if not name:
        return None

    website = node.get("companyPageUrl") or node.get("websiteUrl") or ""
    size_range = node.get("staffCountRange") or {}
    size_str = (
        f"{size_range.get('start', '')}-{size_range.get('end', '')}"
        if size_range
        else node.get("staffCount", "")
    )
    hq = node.get("headquartersLocation") or node.get("headquarters") or ""
    if isinstance(hq, dict):
        parts = [hq.get("city", ""), hq.get("country", "")]
        hq = ", ".join(p for p in parts if p)

    industries = node.get("industries") or []
    industry = industries[0] if industries else node.get("industryName", "")
    if isinstance(industry, dict):
        industry = industry.get("localizedName", "")

    linkedin_url = (
        node.get("url")
        or node.get("companyPageUrl")
        or (
            f"https://www.linkedin.com/company/{node['universalName']}"
            if "universalName" in node
            else ""
        )
    )

    return {
        "company_name": name,
        "linkedin_url": linkedin_url,
        "company_domain": extract_domain(website),
        "industry": industry,
        "company_size": str(size_str),
        "headquarters": str(hq),
        "followers": str(node.get("followingInfo", {}).get("followerCount", "")
                         or node.get("followerCount", "")),
        "description": (node.get("description") or "")[:500],
        "scraped_timestamp": now_ts(),
    }


def _extract_employee_from_api_node(node: dict) -> dict | None:
    """Map a raw API node to a normalised employee dict."""
    first = node.get("firstName") or node.get("localizedFirstName") or ""
    last = node.get("lastName") or node.get("localizedLastName") or ""
    name = f"{first} {last}".strip() or node.get("publicIdentifier", "")
    if not name:
        return None

    public_id = node.get("publicIdentifier") or ""
    profile_url = (
        node.get("profileUrl")
        or (f"https://www.linkedin.com/in/{public_id}" if public_id else "")
    )
    positions = node.get("positions", {}).get("elements") or []
    title = ""
    company_name = ""
    if positions:
        pos = positions[0]
        title = pos.get("title", "")
        company_name = (pos.get("company") or {}).get("name", "")

    degree_map = {1: "1st", 2: "2nd", 3: "3rd"}
    distance = node.get("distance", {}).get("value", 0) if isinstance(node.get("distance"), dict) else 0

    return {
        "employee_name": name,
        "job_title": title,
        "profile_url": profile_url,
        "company_name": company_name,
        "location": node.get("locationName", ""),
        "connection_level": degree_map.get(distance, ""),
        "mutual_connections": str(
            node.get("memberRelationship", {}).get("memberRelationship", "") or ""
        ),
        "scraped_timestamp": now_ts(),
    }


async def intercept_api_responses(page: Page) -> None:
    """
    Attach a network response listener to the given page that watches for
    LinkedIn voyager API calls and parses them into the shared buffers.
    """
    async def _handler(response: Response) -> None:
        url = response.url
        if "voyager/api" not in url:
            return
        try:
            body = await response.json()
            _parse_voyager_response(url, body)
            logger.debug("Intercepted voyager API: %s", url)
        except Exception as exc:
            logger.debug("Could not parse voyager response from %s: %s", url, exc)

    page.on("response", _handler)
    logger.info("Voyager API interception active on page.")


# ---------------------------------------------------------------------------
# COMPANY DISCOVERY
# ---------------------------------------------------------------------------

# LinkedIn company-size filter codes
_SIZE_FILTERS = {
    "1-10": "B",
    "11-50": "C",
    "51-200": "D",
    "201-500": "E",
    "501-1000": "F",
    "1001-5000": "G",
    "5001-10000": "H",
    "10001+": "I",
}

# Approximate industry URN IDs (LinkedIn changes these; kept as reasonable defaults)
_INDUSTRY_FILTERS = {
    "Software Development": "4",
    "Information Technology": "96",
    "Internet": "6",
}


def _build_company_search_url(keyword: str, page_num: int = 0) -> str:
    """Build a LinkedIn company search URL for a given keyword."""
    params = {
        "keywords": keyword,
        "origin": "SWITCH_SEARCH_VERTICAL",
    }
    base = "https://www.linkedin.com/search/results/companies/?" + urlencode(params)
    if page_num > 0:
        base += f"&start={page_num * 10}"
    return base


def _build_job_search_url(title: str, country: str, page_num: int = 0) -> str:
    """Build a LinkedIn job search URL for employee-based company discovery."""
    params = {
        "keywords": title,
        "location": country,
        "f_TPR": "r2592000",   # past month
    }
    base = "https://www.linkedin.com/jobs/search/?" + urlencode(params)
    if page_num > 0:
        base += f"&start={page_num * 25}"
    return base


async def _collect_company_urls_from_page(page: Page) -> set[str]:
    """DOM fallback: scrape company card links from a search results page."""
    urls: set[str] = set()
    try:
        await human_scroll(page, steps=4)
        # Company search result cards
        links = await page.query_selector_all(
            "a[href*='/company/']"
        )
        for link in links:
            href = await link.get_attribute("href")
            if href and "/company/" in href:
                # Normalise to canonical company URL
                clean = re.sub(r"\?.*$", "", href).rstrip("/")
                if clean.startswith("/"):
                    clean = "https://www.linkedin.com" + clean
                urls.add(clean)
    except Exception as exc:
        logger.warning("DOM company URL collection failed: %s", exc)
    return urls


async def _collect_company_urls_from_jobs(page: Page) -> set[str]:
    """Extract company URLs from job listing cards."""
    urls: set[str] = set()
    try:
        await human_scroll(page, steps=6)
        links = await page.query_selector_all(
            "a.job-card-container__company-name, "
            "a[href*='/company/']"
        )
        for link in links:
            href = await link.get_attribute("href")
            if href and "/company/" in href:
                clean = re.sub(r"\?.*$", "", href).rstrip("/")
                if clean.startswith("/"):
                    clean = "https://www.linkedin.com" + clean
                urls.add(clean)
    except Exception as exc:
        logger.warning("Job-based company URL collection failed: %s", exc)
    return urls


async def discover_companies(context: BrowserContext) -> set[str]:
    """
    Discover company profile URLs using configured search strategies.

    Returns a set of LinkedIn company URLs.
    """
    logger.info("Starting company discovery…")
    company_urls: set[str] = set()
    max_companies = SEARCH_CONFIG.get("max_companies") or 999
    cfg = SEARCH_CONFIG

    page = await context.new_page()
    await intercept_api_responses(page)

    # ------------------------------------------------------------------ #
    # Strategy 1: LinkedIn company search
    # ------------------------------------------------------------------ #
    if "linkedin_company_search" in cfg["search_strategy"]:
        for keyword in cfg["keywords"]:
            if len(company_urls) >= max_companies:
                break
            logger.info("Company search – keyword: '%s'", keyword)
            for pg in range(cfg["max_pages"]):
                if len(company_urls) >= max_companies:
                    break
                url = _build_company_search_url(keyword, pg)
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    await sleep_rand(*DELAY_MEDIUM)
                    new_urls = await _collect_company_urls_from_page(page)
                    company_urls.update(new_urls)
                    logger.info(
                        "  Page %d – found %d new URLs (total: %d)",
                        pg + 1, len(new_urls), len(company_urls),
                    )
                    # Also pick up anything voyager handed us
                    for rec in list(_api_company_buffer):
                        if rec.get("linkedin_url"):
                            company_urls.add(rec["linkedin_url"])
                    if not new_urls:
                        logger.info("  No more results for '%s', stopping pagination.", keyword)
                        break
                    await sleep_rand(*DELAY_MEDIUM)
                except Exception as exc:
                    logger.warning("Company search page %d failed: %s", pg + 1, exc)
                    await sleep_rand(*DELAY_LONG)

    # ------------------------------------------------------------------ #
    # Strategy 2: Job listings
    # ------------------------------------------------------------------ #
    if "linkedin_job_listings" in cfg["search_strategy"]:
        for title in cfg["job_titles_for_discovery"]:
            if len(company_urls) >= max_companies:
                break
            logger.info("Job search – title: '%s'", title)
            for pg in range(min(cfg["max_pages"], 3)):
                if len(company_urls) >= max_companies:
                    break
                url = _build_job_search_url(title, cfg["country"], pg)
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    await sleep_rand(*DELAY_MEDIUM)
                    new_urls = await _collect_company_urls_from_jobs(page)
                    company_urls.update(new_urls)
                    logger.info(
                        "  Job page %d – found %d URLs (total: %d)",
                        pg + 1, len(new_urls), len(company_urls),
                    )
                    if not new_urls:
                        break
                    await sleep_rand(*DELAY_MEDIUM)
                except Exception as exc:
                    logger.warning("Job search page %d failed: %s", pg + 1, exc)
                    await sleep_rand(*DELAY_LONG)

    await page.close()

    # Cap at configured maximum
    result = set(list(company_urls)[:max_companies])
    logger.info("Company discovery complete – %d unique URLs collected.", len(result))
    return result


# ---------------------------------------------------------------------------
# COMPANY DATA EXTRACTION
# ---------------------------------------------------------------------------

async def _scrape_company_dom(page: Page) -> dict:
    """
    DOM-based fallback to extract company information when the voyager API
    did not supply the data we need.
    """
    data: dict[str, str] = {}
    try:
        # Company name
        name_el = await page.query_selector("h1.org-top-card-summary__title, h1[data-test-id='name']")
        if name_el:
            data["company_name"] = (await name_el.inner_text()).strip()

        # Industry / size / location appear in the overview section
        overview_items = await page.query_selector_all(
            "div.org-top-card-summary-info-list__info-item, "
            "div[data-test-id='about-us__size'], "
            "dt.org-about-company-module__company-size-definition-text"
        )
        for item in overview_items:
            txt = (await item.inner_text()).strip()
            if re.search(r"\d+[,\d]*\s*(employees|members)", txt, re.I):
                data.setdefault("company_size", txt)
            elif re.search(r"(inc\.|ltd\.|gmbh|software|consulting|services)", txt, re.I):
                data.setdefault("industry", txt)
            else:
                data.setdefault("headquarters", txt)

        # Followers
        followers_el = await page.query_selector(
            "span.org-top-card-summary-info-list__info-item--followers, "
            "[data-test-followers-count]"
        )
        if followers_el:
            data["followers"] = (await followers_el.inner_text()).strip()

        # Website
        website_el = await page.query_selector(
            "a[data-tracking-control-name='about_website'], "
            "a[href*='http'][class*='website']"
        )
        if website_el:
            href = await website_el.get_attribute("href") or ""
            data["company_domain"] = extract_domain(href)

        # Description
        desc_el = await page.query_selector(
            "p.org-about-us-organization-description__text, "
            "section[data-test-id='about-us'] p"
        )
        if desc_el:
            data["description"] = (await desc_el.inner_text()).strip()[:500]

    except Exception as exc:
        logger.debug("DOM company scrape error: %s", exc)

    return data


async def scrape_company_data(
    context: BrowserContext,
    company_url: str,
) -> dict:
    """
    Navigate to a LinkedIn company page and extract all configured fields.
    Prefers voyager API data; falls back to DOM scraping.
    """
    logger.info("Scraping company: %s", company_url)
    _api_company_buffer.clear()

    page = await context.new_page()
    await intercept_api_responses(page)

    company_data: dict = {
        "company_name": "",
        "linkedin_url": company_url,
        "company_domain": "",
        "industry": "",
        "company_size": "",
        "headquarters": "",
        "followers": "",
        "description": "",
        "scraped_timestamp": now_ts(),
    }

    for attempt in range(3):
        try:
            await page.goto(company_url, wait_until="domcontentloaded", timeout=40_000)
            await sleep_rand(*DELAY_MEDIUM)
            await human_scroll(page, steps=5)
            await sleep_rand(*DELAY_SHORT)
            break
        except Exception as exc:
            logger.warning("Attempt %d – failed to load %s: %s", attempt + 1, company_url, exc)
            await sleep_rand(*DELAY_LONG)
    else:
        logger.error("Could not load company page after 3 attempts: %s", company_url)
        await page.close()
        return company_data

    # ---- Prefer API data ----
    if _api_company_buffer:
        api_rec = _api_company_buffer[0]
        company_data.update({k: v for k, v in api_rec.items() if v})
        logger.info("  Used voyager API data for '%s'.", company_data.get("company_name"))
    else:
        # ---- DOM fallback ----
        dom_rec = await _scrape_company_dom(page)
        company_data.update({k: v for k, v in dom_rec.items() if v})
        logger.info("  Used DOM scrape for '%s'.", company_data.get("company_name"))

    company_data["linkedin_url"] = company_url  # ensure original URL is kept
    await page.close()
    return company_data


# ---------------------------------------------------------------------------
# EMPLOYEE DISCOVERY
# ---------------------------------------------------------------------------

async def _scrape_employee_dom(page: Page, company_name: str) -> list[dict]:
    """DOM fallback to pull employee cards from the People tab."""
    employees: list[dict] = []
    try:
        await human_scroll(page, steps=5)
        cards = await page.query_selector_all(
            "li.org-people-profile-card__profile-card-spacing, "
            "div[data-member-id], "
            "li.reusable-search__result-container"
        )
        for card in cards[:10]:   # cap at 10 per company
            try:
                name_el = await card.query_selector(
                    "span.org-people-profile-card__profile-title, "
                    "span[aria-hidden='true'], "
                    "span.actor-name"
                )
                name = (await name_el.inner_text()).strip() if name_el else ""

                title_el = await card.query_selector(
                    "div.org-people-profile-card__profile-info, "
                    "div.entity-result__primary-subtitle"
                )
                title = (await title_el.inner_text()).strip() if title_el else ""

                link_el = await card.query_selector("a[href*='/in/']")
                profile_url = ""
                if link_el:
                    href = await link_el.get_attribute("href") or ""
                    profile_url = re.sub(r"\?.*$", "", href).rstrip("/")
                    if profile_url.startswith("/"):
                        profile_url = "https://www.linkedin.com" + profile_url

                if name:
                    employees.append({
                        "employee_name": name,
                        "job_title": title,
                        "profile_url": profile_url,
                        "company_name": company_name,
                        "location": "",
                        "connection_level": "",
                        "mutual_connections": "",
                        "scraped_timestamp": now_ts(),
                    })
            except Exception:
                pass
    except Exception as exc:
        logger.debug("DOM employee scrape error: %s", exc)
    return employees


async def discover_employees(
    context: BrowserContext,
    company_data: dict,
) -> list[dict]:
    """
    Navigate to the People section of a company page and collect employees.
    Prefers voyager API employee data; falls back to DOM scraping.
    """
    company_name = company_data.get("company_name", "Unknown")
    company_url = company_data.get("linkedin_url", "")
    logger.info("Discovering employees for '%s'…", company_name)

    _api_employee_buffer.clear()

    # Construct the /people URL
    people_url = company_url.rstrip("/") + "/people/"

    page = await context.new_page()
    await intercept_api_responses(page)

    employees: list[dict] = []

    for attempt in range(3):
        try:
            await page.goto(people_url, wait_until="domcontentloaded", timeout=40_000)
            await sleep_rand(*DELAY_MEDIUM)
            await human_scroll(page, steps=6)
            await sleep_rand(*DELAY_SHORT)
            break
        except Exception as exc:
            logger.warning(
                "Attempt %d – could not load people page for '%s': %s",
                attempt + 1, company_name, exc,
            )
            await sleep_rand(*DELAY_LONG)
    else:
        logger.error("Could not load people page for '%s'.", company_name)
        await page.close()
        return employees

    # ---- Prefer API data ----
    if _api_employee_buffer:
        for rec in _api_employee_buffer:
            rec["company_name"] = company_name   # ensure linkage
        employees = _api_employee_buffer[:10]
        logger.info(
            "  Collected %d employees via API for '%s'.", len(employees), company_name
        )
    else:
        # ---- DOM fallback ----
        employees = await _scrape_employee_dom(page, company_name)
        logger.info(
            "  Collected %d employees via DOM for '%s'.", len(employees), company_name
        )

    await page.close()
    return employees


# ---------------------------------------------------------------------------
# DATA PERSISTENCE
# ---------------------------------------------------------------------------

_COMPANY_FIELDS = [
    "company_name", "linkedin_url", "company_domain", "industry",
    "company_size", "headquarters", "followers", "description",
    "scraped_timestamp",
]

_EMPLOYEE_FIELDS = [
    "employee_name", "job_title", "profile_url", "company_name",
    "location", "connection_level", "mutual_connections", "scraped_timestamp",
]


def _write_csv(path: str, rows: list[dict], fieldnames: list[str]) -> None:
    file_exists = Path(path).exists()
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def _write_json(path: str, rows: list[dict]) -> None:
    existing: list[dict] = []
    if Path(path).exists():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                existing = json.load(fh)
        except Exception:
            pass
    existing.extend(rows)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, ensure_ascii=False, indent=2)


def save_company_data(companies: list[dict]) -> None:
    """Persist company records to CSV and JSON."""
    if not companies:
        logger.warning("No company data to save.")
        return
    _write_csv(COMPANIES_CSV, companies, _COMPANY_FIELDS)
    _write_json(COMPANIES_JSON, companies)
    logger.info("Saved %d company records → %s / %s", len(companies), COMPANIES_CSV, COMPANIES_JSON)


def save_employee_data(employees: list[dict]) -> None:
    """Persist employee records to CSV and JSON."""
    if not employees:
        logger.warning("No employee data to save.")
        return
    _write_csv(EMPLOYEES_CSV, employees, _EMPLOYEE_FIELDS)
    _write_json(EMPLOYEES_JSON, employees)
    logger.info("Saved %d employee records → %s / %s", len(employees), EMPLOYEES_CSV, EMPLOYEES_JSON)


# ---------------------------------------------------------------------------
# MAIN ORCHESTRATION
# ---------------------------------------------------------------------------

async def main() -> None:
    logger.info("=== LinkedIn Scraper Starting ===")
    logger.info("Search config: %s", json.dumps(SEARCH_CONFIG, indent=2))

    all_companies: list[dict] = []
    all_employees: list[dict] = []

    async with async_playwright() as pw:
        browser = await initialize_browser(pw)
        context = await load_or_create_session(browser)

        try:
            # 1. Discover company URLs
            company_urls = await discover_companies(context)

            if not company_urls:
                logger.warning("No company URLs discovered – check search config or login state.")
                return

            # 2. Scrape each company + its employees
            for idx, url in enumerate(company_urls, 1):
                logger.info("─── Processing company %d / %d ───", idx, len(company_urls))
                try:
                    company = await scrape_company_data(context, url)
                    all_companies.append(company)

                    employees = await discover_employees(context, company)
                    all_employees.extend(employees)

                    # Incremental saves so progress isn't lost on error
                    save_company_data([company])
                    if employees:
                        save_employee_data(employees)

                    await sleep_rand(*DELAY_LONG)

                except Exception as exc:
                    logger.error("Error processing %s: %s", url, exc)
                    await sleep_rand(*DELAY_LONG)

        finally:
            # Always persist the (potentially refreshed) session
            await context.storage_state(path=SESSION_FILE)
            logger.info("Session state updated in '%s'.", SESSION_FILE)
            await context.close()
            await browser.close()

    logger.info("=== Scraping Complete ===")
    logger.info("Companies collected : %d", len(all_companies))
    logger.info("Employees collected : %d", len(all_employees))
    logger.info("Output files: %s, %s, %s, %s",
                COMPANIES_CSV, EMPLOYEES_CSV, COMPANIES_JSON, EMPLOYEES_JSON)


if __name__ == "__main__":
    asyncio.run(main())
