import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8')

conn = sqlite3.connect(r'c:\Users\sandeep\Downloads\Claudes\CVL\data\db\linkedin_data.db')
cur = conn.cursor()

# Get the last email dispatch time
cur.execute("SELECT MAX(sent_timestamp) FROM email_attempts")
last_email_time = cur.fetchone()[0]

print(f"Last email dispatched at: {last_email_time}")

# Count companies scraped after the last email dispatch
cur.execute("""
    SELECT COUNT(*) 
    FROM companies 
    WHERE scraped_timestamp > ?
""", (last_email_time,))
companies_after = cur.fetchone()[0]

print(f"Companies with scraped_timestamp > last email dispatch: {companies_after}")

# Let's see the most recent scrape sessions by grouping scraped_timestamp by hour/day
cur.execute("""
    SELECT substr(scraped_timestamp, 1, 13) as scrape_hour, COUNT(*)
    FROM companies
    GROUP BY scrape_hour
    ORDER BY scrape_hour DESC
    LIMIT 10
""")
recent_scrapes = cur.fetchall()

print("\nRecent scrape sessions (by hour):")
for row in recent_scrapes:
    print(f"  {row[0]}: {row[1]} companies")

# Let's check how many total companies were added/updated on different dates
cur.execute("""
    SELECT substr(scraped_timestamp, 1, 10) as scrape_date, COUNT(*)
    FROM companies
    GROUP BY scrape_date
    ORDER BY scrape_date DESC
""")
scrapes_by_date = cur.fetchall()

print("\nCompanies scraped by date:")
for row in scrapes_by_date:
    print(f"  {row[0]}: {row[1]} companies")

# Check if there are companies with a scraped_timestamp BEFORE the email dispatch, 
# but no emails sent to them?
cur.execute("""
    SELECT COUNT(*) 
    FROM companies c
    WHERE c.scraped_timestamp <= ? 
      AND (c.company_domain IS NULL OR c.company_domain = ''
           OR c.company_domain NOT IN (
               SELECT DISTINCT company_domain FROM email_attempts
               WHERE company_domain IS NOT NULL AND company_domain != ''
           ))
""", (last_email_time,))
unemailed_before = cur.fetchone()[0]

print(f"\nCompanies scraped BEFORE last email dispatch but NOT emailed: {unemailed_before}")

conn.close()
