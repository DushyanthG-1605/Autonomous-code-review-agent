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
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)")}
    if "execution_id" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN execution_id TEXT")
    if "status" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN status TEXT")
    if "metadata" not in columns:
        conn.execute("ALTER TABLE events ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_execution_id "
        "ON events (execution_id) WHERE execution_id IS NOT NULL"
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("Database initialized at", DB_PATH)