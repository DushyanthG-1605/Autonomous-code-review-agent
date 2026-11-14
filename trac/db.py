import os
import sqlite3
import uuid
from pathlib import Path

DB_PATH = Path(os.environ.get("TRAC_DB_PATH", Path(__file__).parent / "trac.db"))
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _apply_migrations(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Documented maintenance path: migrations may rewrite legacy rows before
        # restoring the database-level append-only guard.
        conn.execute("DROP TRIGGER IF EXISTS events_append_only_update")
        conn.execute("DROP TRIGGER IF EXISTS events_append_only_delete")
        applied = {
            row["version"]
            for row in conn.execute("SELECT version FROM schema_migrations")
        }
        if 1 not in applied:
            migration_path = MIGRATIONS_DIR / "001_project_scoped_events.sql"
            for statement in migration_path.read_text(encoding="utf-8").split(";"):
                if statement.strip():
                    try:
                        conn.execute(statement)
                    except sqlite3.OperationalError as exc:
                        if "duplicate column name" not in str(exc).lower():
                            raise

            users = conn.execute(
                "SELECT id, github_login, display_name FROM users"
            ).fetchall()
            for user in users:
                public_id = str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"trac:user:{user['id']}")
                )
                project_id = str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"trac:personal:{user['id']}")
                )
                conn.execute(
                    "UPDATE users SET public_id = COALESCE(public_id, ?) WHERE id = ?",
                    (public_id, user["id"]),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO projects (id, slug, name, created_at)
                    VALUES (?, ?, ?, datetime('now'))
                    """,
                    (
                        project_id,
                        f"personal-{user['id']}",
                        f"{user['display_name']}'s workspace",
                    ),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO project_memberships
                        (project_id, user_id, role, created_at)
                    VALUES (?, ?, 'owner', datetime('now'))
                    """,
                    (project_id, user["id"]),
                )
                conn.execute(
                    """
                    UPDATE devices SET project_id = ?
                    WHERE user_id = ? AND project_id IS NULL
                    """,
                    (project_id, user["id"]),
                )
                conn.execute(
                    """
                    UPDATE activity_events SET project_id = ?
                    WHERE user_id = ? AND project_id IS NULL
                    """,
                    (project_id, user["id"]),
                )

            conn.execute(
                """
                UPDATE events SET actor_user_id = (
                    SELECT MIN(users.id) FROM users
                    WHERE users.github_login = events.actor
                       OR users.display_name = events.actor
                )
                WHERE actor_user_id IS NULL AND actor IS NOT NULL
                  AND (
                      SELECT COUNT(*) FROM users
                      WHERE users.github_login = events.actor
                         OR users.display_name = events.actor
                  ) = 1
                """
            )
            conn.execute(
                """
                UPDATE events SET project_id = (
                    SELECT projects.id FROM projects
                    JOIN project_memberships
                      ON project_memberships.project_id = projects.id
                    WHERE project_memberships.user_id = events.actor_user_id
                    ORDER BY projects.slug LIMIT 1
                )
                WHERE project_id IS NULL AND actor_user_id IS NOT NULL
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO activity_event_stream
                    (event_id, project_id, created_at)
                SELECT event_id, project_id, received_at
                FROM activity_events WHERE project_id IS NOT NULL
                """
            )
            conn.execute(
                """
                INSERT INTO schema_migrations (version, applied_at)
                VALUES (1, datetime('now'))
                """
            )

        if 2 not in applied:
            migration_path = MIGRATIONS_DIR / "002_projects_tasks_identity.sql"
            for statement in migration_path.read_text(encoding="utf-8").split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(
                """
                INSERT INTO schema_migrations (version, applied_at)
                VALUES (2, datetime('now'))
                """
            )
            applied.add(2)
        if 3 not in applied:
            migration_path = MIGRATIONS_DIR / "003_device_pairing.sql"
            for statement in migration_path.read_text(encoding="utf-8").split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(
                """
                INSERT INTO schema_migrations (version, applied_at)
                VALUES (3, datetime('now'))
                """
            )
        if 4 not in applied:
            migration_path = MIGRATIONS_DIR / "004_github_ci_integration.sql"
            for statement in migration_path.read_text(encoding="utf-8").split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(
                """
                INSERT INTO schema_migrations (version, applied_at)
                VALUES (4, datetime('now'))
                """
            )
        if 5 not in applied:
            migration_path = MIGRATIONS_DIR / "005_dashboard_device_activity.sql"
            for statement in migration_path.read_text(encoding="utf-8").split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(
                """
                INSERT INTO schema_migrations (version, applied_at)
                VALUES (5, datetime('now'))
                """
            )
        conn.execute("""CREATE TRIGGER IF NOT EXISTS events_append_only_update
            BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events is append-only'); END""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS events_append_only_delete
            BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events is append-only'); END""")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db():
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
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
            conn.execute(
                "ALTER TABLE events ADD COLUMN metadata TEXT NOT NULL DEFAULT '{}'"
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_events_execution_id "
            "ON events (execution_id) WHERE execution_id IS NOT NULL"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                github_login TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                label TEXT NOT NULL,
                created_at TEXT NOT NULL,
                revoked_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                key_hash TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL DEFAULT 'API key',
                created_at TEXT NOT NULL,
                last_used_at TEXT,
                revoked_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
        """)
        for statement in (
            "ALTER TABLE events ADD COLUMN actor_user_id INTEGER",
            "ALTER TABLE events ADD COLUMN actor_hint TEXT",
            "ALTER TABLE events ADD COLUMN task_key TEXT",
            "ALTER TABLE events ADD COLUMN reason TEXT",
            "ALTER TABLE events ADD COLUMN confidence REAL",
            "ALTER TABLE events ADD COLUMN occurred_at TEXT",
            "ALTER TABLE events ADD COLUMN external_id TEXT",
            "ALTER TABLE events ADD COLUMN tool TEXT",
            "ALTER TABLE events ADD COLUMN payload TEXT",
            "ALTER TABLE events ADD COLUMN api_key_id INTEGER",
        ):
            try:
                conn.execute(statement)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_occurred_at ON events (occurred_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_actor_occurred ON events (actor_user_id, occurred_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_repo_occurred ON events (repo, occurred_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_task_occurred ON events (task_key, occurred_at)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_events_owner_external ON events (api_key_id, external_id) WHERE external_id IS NOT NULL")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS events_append_only_update
            BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events is append-only'); END""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS events_append_only_delete
            BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events is append-only'); END""")
        conn.execute("""
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
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_activity_events_user_ts
            ON activity_events (user_id, ts)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_activity_events_repo_ts
            ON activity_events (repo, ts)
        """)
        conn.commit()
        _apply_migrations(conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print("Database initialized at", DB_PATH)
