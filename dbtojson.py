import sqlite3
import json

def jobs_table_to_json(db_path, json_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Get all rows from jobs table
    cursor.execute("SELECT * FROM jobs")
    columns = [description[0] for description in cursor.description]
    rows = cursor.fetchall()

    # Convert to list of dictionaries
    data = []
    for row in rows:
        row_dict = dict(zip(columns, row))
        data.append(row_dict)

    conn.close()

    # Write to JSON
    with open(json_path, 'w') as f:
        json.dump(data, f, indent=2, default=str)

    print(f"Exported {len(data)} rows from jobs table to {json_path}")
    return data

# Usage
jobs_table_to_json('rtjobs.db', 'jobs.json')
print("hrllo")
