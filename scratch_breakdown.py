import sqlite3

conn = sqlite3.connect(r'c:\Users\sandeep\Downloads\Claudes\CVL\data\db\linkedin_data.db')
cur = conn.cursor()

# Breakdown of all 480 companies
print("=== BREAKDOWN OF ALL 480 COMPANIES ===")

# Emailed vs Not Emailed
cur.execute("""
    SELECT 
        CASE 
            WHEN c.company_domain IN (SELECT DISTINCT company_domain FROM email_attempts WHERE company_domain IS NOT NULL) THEN 'Already Emailed'
            ELSE 'Not Emailed'
        END as status,
        COUNT(*)
    FROM companies c
    GROUP BY status
""")
for row in cur.fetchall():
    print(f"{row[0]}: {row[1]}")

print("\n=== DEEP DIVE INTO THE 48 UNEMAILED COMPANIES ===")
# Of the 48 unemailed companies, check domains and employees
cur.execute("""
    WITH Unemailed AS (
        SELECT c.*
        FROM companies c
        WHERE (c.company_domain IS NULL OR c.company_domain = ''
               OR c.company_domain NOT IN (
                   SELECT DISTINCT company_domain FROM email_attempts
                   WHERE company_domain IS NOT NULL AND company_domain != ''
               ))
    )
    SELECT 
        CASE WHEN company_domain IS NULL OR company_domain = '' THEN 'No Domain' ELSE 'Has Domain' END as domain_status,
        CASE WHEN (SELECT COUNT(*) FROM employees e WHERE e.company_linkedin_url = u.linkedin_url) = 0 THEN 'No Employees' ELSE 'Has Employees' END as emp_status,
        COUNT(*)
    FROM Unemailed u
    GROUP BY domain_status, emp_status
""")

for row in cur.fetchall():
    print(f"- {row[0]} AND {row[1]}: {row[2]} companies")

conn.close()
