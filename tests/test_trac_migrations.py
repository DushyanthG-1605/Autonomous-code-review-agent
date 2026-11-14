import sqlite3

from trac import db


def test_additive_migration_preserves_and_scopes_existing_rows(tmp_path, monkeypatch):
    database_path = tmp_path / "legacy.db"
    monkeypatch.setattr(db, "DB_PATH", database_path)
    conn = sqlite3.connect(database_path)
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            github_login TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL,
            created_at TEXT NOT NULL,
            revoked_at TEXT
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            source TEXT NOT NULL,
            summary TEXT NOT NULL,
            repo TEXT,
            ref TEXT,
            actor TEXT,
            timestamp TEXT NOT NULL
        );
        CREATE TABLE activity_events (
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
            data JSON NOT NULL
        );
        INSERT INTO users (github_login, display_name, created_at)
        VALUES ('alice', 'Alice', '2025-01-01T00:00:00Z');
        INSERT INTO users (github_login, display_name, created_at)
        VALUES ('bob', 'Ambiguous', '2025-01-01T00:00:00Z');
        INSERT INTO users (github_login, display_name, created_at)
        VALUES ('carol', 'Ambiguous', '2025-01-01T00:00:00Z');
        INSERT INTO devices (user_id, token_hash, label, created_at)
        VALUES (1, 'existing-hash', 'legacy device', '2025-01-01T00:00:00Z');
        INSERT INTO events (type, source, summary, actor, timestamp)
        VALUES (
            'legacy.event', 'legacy', 'kept event', 'alice',
            '2025-01-01T00:00:00Z'
        );
        INSERT INTO events (type, source, summary, actor, timestamp)
        VALUES (
            'legacy.event', 'legacy', 'ambiguous event', 'Ambiguous',
            '2025-01-01T00:00:00Z'
        );
        INSERT INTO activity_events
            (event_id, schema_version, type, ts, received_at, user_id, data)
        VALUES (
            '01JGFJJZ00A1B2C3D4E5F6G7H8', 1, 'task.started',
            '2025-01-01T00:00:00Z', '2025-01-01T00:00:01Z', 1, '{}'
        );
        """
    )
    conn.commit()
    conn.close()

    db.init_db()
    conn = db.get_connection()
    user = conn.execute(
        """
        SELECT users.public_id, users.github_username, users.role
        FROM users WHERE github_login = 'alice'
        """
    ).fetchone()
    device = conn.execute(
        "SELECT project_id, scopes FROM devices WHERE id = 1"
    ).fetchone()
    legacy_event = conn.execute(
        "SELECT project_id, actor_user_id, summary FROM events WHERE id = 1"
    ).fetchone()
    ambiguous_event = conn.execute(
        "SELECT project_id, actor_user_id FROM events WHERE id = 2"
    ).fetchone()
    activity = conn.execute(
        """
        SELECT activity_events.project_id, activity_event_stream.sequence
        FROM activity_events
        JOIN activity_event_stream
          ON activity_event_stream.event_id = activity_events.event_id
        WHERE activity_events.event_id = '01JGFJJZ00A1B2C3D4E5F6G7H8'
        """
    ).fetchone()
    migration_count = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[
        0
    ]
    project = conn.execute(
        """
        SELECT project_key, project_head_id FROM projects
        WHERE id = ?
        """,
        (device["project_id"],),
    ).fetchone()
    task_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
    ).fetchone()
    conn.close()

    assert user["public_id"]
    assert user["github_username"] == "alice"
    assert user["role"] == "developer"
    assert device["project_id"] == legacy_event["project_id"] == activity["project_id"]
    assert "events:ingest" in device["scopes"]
    assert legacy_event["actor_user_id"] == 1
    assert legacy_event["summary"] == "kept event"
    assert ambiguous_event["project_id"] is None
    assert ambiguous_event["actor_user_id"] is None
    assert activity["sequence"] == 1
    assert project["project_key"] == "personal-1"
    assert project["project_head_id"] == 1
    assert task_table is not None
    assert migration_count == 3

    db.init_db()
    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM activity_event_stream").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 3
    conn.close()
