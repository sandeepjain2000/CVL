import sqlite3

def get_schema(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT name, sql FROM sqlite_master WHERE type='table';")
    tables = cursor.fetchall()
    for table in tables:
        print(f"Table: {table[0]}")
        print(f"Schema: {table[1]}\n")
    conn.close()

if __name__ == "__main__":
    get_schema('linkedin_data.db')
