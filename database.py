import sqlite3
import json
from datetime import datetime

DB_NAME = "scholarships.db"

def get_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    # Master Scholarship Table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS scholarships (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE,
        name TEXT NOT NULL,
        provider TEXT NOT NULL,
        source_type TEXT NOT NULL, -- Government, University, Corporate CSR, Foundation/Trust
        official_source_url TEXT NOT NULL,
        application_url TEXT,
        amount_benefit TEXT,
        education_level TEXT,
        income_criteria TEXT,
        gender_criteria TEXT,
        category_criteria TEXT,
        domicile_criteria TEXT,
        opening_date TEXT,
        closing_date TEXT,
        selection_process TEXT,
        documents_required TEXT,
        status TEXT NOT NULL, -- ACTIVE, EXPIRING_SOON, EXPIRED, REVIEW_REQUIRED, NO_LONGER_VERIFIABLE
        confidence_score REAL NOT NULL,
        confidence_breakdown TEXT,
        last_verified TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    # Evidence Table (Field-level provenance)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS field_evidence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scholarship_id INTEGER,
        field_name TEXT NOT NULL,
        extracted_value TEXT,
        source_snippet TEXT NOT NULL,
        verified_at TEXT NOT NULL,
        FOREIGN KEY (scholarship_id) REFERENCES scholarships (id) ON DELETE CASCADE
    )
    """)

    # Change Detection History
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS change_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scholarship_id INTEGER,
        field_name TEXT NOT NULL,
        old_value TEXT,
        new_value TEXT,
        change_date TEXT NOT NULL,
        evidence TEXT,
        FOREIGN KEY (scholarship_id) REFERENCES scholarships (id) ON DELETE CASCADE
    )
    """)

    # Crawl Run History
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS crawl_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_timestamp TEXT NOT NULL,
        discovered_count INTEGER,
        updated_count INTEGER,
        unchanged_count INTEGER,
        stale_count INTEGER
    )
    """)

    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print("Database schema successfully initialized.")