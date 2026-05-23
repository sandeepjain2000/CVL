import sqlite3
import pandas as pd

def analyze_email_formats(db_path):
    conn = sqlite3.connect(db_path)
    
    query = """
    SELECT 
        email_format, 
        status, 
        COUNT(*) as count
    FROM 
        email_attempts
    GROUP BY 
        email_format, status
    ORDER BY 
        email_format, status;
    """
    
    df = pd.read_sql_query(query, conn)
    conn.close()
    
    if df.empty:
        print("No data found in email_attempts table.")
        return

    # Pivot the data to have statuses as columns
    pivot_df = df.pivot(index='email_format', columns='status', values='count').fillna(0)
    
    # Calculate totals and success/failure rates
    pivot_df['total'] = pivot_df.sum(axis=1)
    
    # Assuming 'sent' is success, or everything not 'bounced' is success
    # Need to check what statuses exist first
    
    print(pivot_df)

if __name__ == "__main__":
    analyze_email_formats('linkedin_data.db')
