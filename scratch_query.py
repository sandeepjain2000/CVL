import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8')

conn = sqlite3.connect(r'c:\Users\sandeep\Downloads\Claudes\CVL\data\db\linkedin_data.db')
cur = conn.cursor()

# Q0: Totals for context
cur.execute("SELECT COUNT(*) FROM companies")
total_companies = cur.fetchone()[0]

cur.execute("SELECT COUNT(*) FROM employees")
total_employees = cur.fetchone()[0]

cur.execute("SELECT COUNT(DISTINCT company_domain) FROM email_attempts WHERE company_domain IS NOT NULL AND company_domain != ''")
domains_emailed = cur.fetchone()[0]

# Q1: Companies to which NO email has been sent (matched by domain)
cur.execute("""
    SELECT COUNT(*) FROM companies c
    WHERE (c.company_domain IS NULL OR c.company_domain = ''
           OR c.company_domain NOT IN (
               SELECT DISTINCT company_domain FROM email_attempts
               WHERE company_domain IS NOT NULL AND company_domain != ''
           ))
""")
no_email_companies = cur.fetchone()[0]

# Cross-check by company_name
cur.execute("""
    SELECT COUNT(*) FROM companies c
    WHERE c.company_name NOT IN (
        SELECT DISTINCT company_name FROM email_attempts
        WHERE company_name IS NOT NULL AND company_name != ''
    )
""")
no_email_by_name = cur.fetchone()[0]

# Breakdown
cur.execute("SELECT COUNT(*) FROM companies WHERE company_domain IS NULL OR company_domain = ''")
no_domain = cur.fetchone()[0]

cur.execute("""
    SELECT COUNT(*) FROM companies
    WHERE company_domain IS NOT NULL AND company_domain != ''
      AND company_domain NOT IN (
          SELECT DISTINCT company_domain FROM email_attempts
          WHERE company_domain IS NOT NULL AND company_domain != ''
      )
""")
has_domain_not_emailed = cur.fetchone()[0]

# Q2: Employees for companies with no email (by company_linkedin_url)
cur.execute("""
    SELECT COUNT(*) FROM employees e
    WHERE e.company_linkedin_url IN (
        SELECT c.linkedin_url FROM companies c
        WHERE (c.company_domain IS NULL OR c.company_domain = ''
               OR c.company_domain NOT IN (
                   SELECT DISTINCT company_domain FROM email_attempts
                   WHERE company_domain IS NOT NULL AND company_domain != ''
               ))
    )
""")
employees_no_email_domain = cur.fetchone()[0]

# Cross-check via company_name
cur.execute("""
    SELECT COUNT(*) FROM employees e
    WHERE e.company_name IN (
        SELECT c.company_name FROM companies c
        WHERE c.company_name NOT IN (
            SELECT DISTINCT company_name FROM email_attempts
            WHERE company_name IS NOT NULL AND company_name != ''
        )
    )
""")
employees_no_email_name = cur.fetchone()[0]

print("=" * 55)
print("   CONTEXT")
print("=" * 55)
print(f"  Total companies in DB           : {total_companies:>6,}")
print(f"  Total employees in DB           : {total_employees:>6,}")
print(f"  Distinct domains emailed        : {domains_emailed:>6,}")
print()
print("=" * 55)
print("   Q1 -- COMPANIES WITH NO EMAIL SENT")
print("=" * 55)
print(f"  Matched by domain               : {no_email_companies:>6,}")
print(f"    (No domain recorded)          : {no_domain:>6,}")
print(f"    (Domain not in email_attempts): {has_domain_not_emailed:>6,}")
print(f"  Cross-check by company_name     : {no_email_by_name:>6,}")
print()
print("=" * 55)
print("   Q2 -- EMPLOYEES FOR THOSE COMPANIES")
print("=" * 55)
print(f"  Via company_linkedin_url        : {employees_no_email_domain:>6,}")
print(f"  Cross-check via company_name    : {employees_no_email_name:>6,}")
print("=" * 55)

conn.close()
