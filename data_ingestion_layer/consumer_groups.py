import json
import sqlite3  # Using sqlite3 as an example; swap with psycopg2/SQLAlchemy for PostgreSQL
from datetime import datetime

# Consumer groups dataset mapped by revenue tiers (G1: High, G2: Medium, G3: Least Revenue)
consumer_groups_data = [
    # High Revenue & Critical Infrastructure (G1)
    ("G1", "FAC-KN-HOSP-01", "revenue-based (High Revenue / Critical)"),
    ("G1", "FAC-KN-PUMP-02", "revenue-based (High Revenue / Critical)"),
    ("G1", "CONS-KN-COM-0215", "revenue-based (High Revenue)"),
    ("G1", "CONS-KN-IND-0089", "revenue-based (High Revenue)"),
    # Medium Revenue (G2)
    ("G2", "CONS-KN-COM-0216", "revenue-based (Medium Revenue)"),
    ("G2", "CONS-KN-COM-0217", "revenue-based (Medium Revenue)"),
    ("G2", "CONS-KN-COM-0218", "revenue-based (Medium Revenue)"),
    # Least Revenue (G3)
    ("G3", "CONS-KN-RES-1042", "revenue-based (Least Revenue)"),
    ("G3", "CONS-KN-RES-1043", "revenue-based (Least Revenue)"),
    ("G3", "CONS-KN-RES-1044", "revenue-based (Least Revenue)"),
    ("G3", "CONS-KN-AGR-0501", "revenue-based (Least Revenue)"),
    ("G3", "CONS-KN-AGR-0502", "revenue-based (Least Revenue)"),
]


def seed_consumer_groups_db():
    # Connect to database (replace with your PostgreSQL connection string if using psycopg2)
    conn = sqlite3.connect("grid_master.db")
    cursor = conn.cursor()

    # Create table if it doesn't exist
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS consumer_groups (
            group_code VARCHAR(10) NOT NULL,
            consumer_id VARCHAR(50) PRIMARY KEY,
            allocation_basis VARCHAR(100) DEFAULT 'revenue-based'
        )
    """
    )

    # Execute parameterized batch insert
    insert_query = """
        INSERT INTO consumer_groups (group_code, consumer_id, allocation_basis)
        VALUES (?, ?, ?)
        ON CONFLICT(consumer_id) DO UPDATE SET
            group_code = EXCLUDED.group_code,
            allocation_basis = EXCLUDED.allocation_basis;
    """

    cursor.executemany(insert_query, consumer_groups_data)
    conn.commit()
    conn.close()
    print(
        f" Successfully inserted/updated {len(consumer_groups_data)} records into 'consumer_groups'."
    )


def export_consumer_groups_json():
    formatted_json = [
        {
            "group_code": group_code,
            "consumer_id": consumer_id,
            "allocation_basis": allocation_basis,
        }
        for group_code, consumer_id, allocation_basis in consumer_groups_data
    ]

    with open("consumer_groups.json", "w") as f:
        json.dump(formatted_json, f, indent=4)

    print(" Exported consumer_groups.json.")


if __name__ == "__main__":
    export_consumer_groups_json()
    seed_consumer_groups_db()