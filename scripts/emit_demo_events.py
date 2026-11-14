"""Create two demo users/API keys and emit a realistic multi-agent timeline."""
import hashlib, json, secrets, sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1]))
from trac.db import get_connection, init_db

if os.environ.get("TRAC_ALLOW_DEMO") != "1":
    raise SystemExit("Refusing to run demo emitter; set TRAC_ALLOW_DEMO=1")
init_db()
conn = get_connection()
try:
    users = []
    for login in ("claude-demo", "cursor-demo"):
        conn.execute("INSERT OR IGNORE INTO users (github_login, display_name, created_at) VALUES (?, ?, datetime('now'))", (login, login.title()))
        users.append(conn.execute("SELECT id FROM users WHERE github_login = ?", (login,)).fetchone()[0])
    keys = []
    for user_id in users:
        token = "trc_" + secrets.token_urlsafe(24)
        conn.execute("INSERT INTO api_keys (user_id, key_hash, name, created_at) VALUES (?, ?, ?, datetime('now'))", (user_id, hashlib.sha256(token.encode()).hexdigest(), "demo agent"))
        keys.append(token)
    conn.commit()
    base = datetime.now(timezone.utc) - timedelta(hours=4)
    events = [("task.started", "ENG-42", "Started API key migration"), ("git.commit", "ENG-42", "Added durable event indexes"), ("test.completed", "ENG-42", "All ingestion tests pass"), ("task.completed", "ENG-42", "Shipped personal agent event flow")]
    for i, (kind, task, summary) in enumerate(events):
        print(json.dumps({"agent": ("claude-code" if i % 2 == 0 else "cursor"), "event": {"type": kind, "task": task, "repo": "acme/platform", "summary": summary, "tool": ("claude-code" if i % 2 == 0 else "cursor"), "occurredAt": (base + timedelta(hours=i)).isoformat(), "payload": {"sequence": i + 1}}}))
finally:
    conn.close()
