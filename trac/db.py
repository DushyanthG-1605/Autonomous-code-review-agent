import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "trac.db"


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            source TEXT NOT NULL,
            summary TEXT NOT NULL,
            repo TEXT,
            ref TEXT,
            actor TEXT,
            timestamp TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("Database initialized at", DB_PATH)