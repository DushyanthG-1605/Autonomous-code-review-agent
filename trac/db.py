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
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            github_login TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL,
            created_at TEXT NOT NULL,
            revoked_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS activity_events (
            event_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            type TEXT NOT NULL,
            ts TEXT NOT NULL,
            received_at TEXT NOT NULL,
            session_id TEXT,
            user_id INTEGER NOT NULL,
            repo TEXT,
            branch TEXT,
            source TEXT,
            tool TEXT,
            data JSON NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE INDEX IF NOT EXISTS idx_activity_events_user_ts
            ON activity_events (user_id, ts);
        CREATE INDEX IF NOT EXISTS idx_activity_events_repo_ts
            ON activity_events (repo, ts);
    """)
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("Database initialized at", DB_PATH)