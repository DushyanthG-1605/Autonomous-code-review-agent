import hashlib, json, uuid
import pytest
from fastapi.testclient import TestClient
from trac import db
from trac.main import app
from trac.models import now_iso
from tests.fake_secrets import github_token, stripe_live_key

@pytest.fixture
def personal_api(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "personal.db")
    with TestClient(app) as client:
        conn = db.get_connection()
        cur = conn.execute("INSERT INTO users (github_login, display_name, created_at) VALUES ('owner', 'Owner', ?)", (now_iso(),))
        user_id = cur.lastrowid
        key = "trc_test_personal_key"
        conn.execute("INSERT INTO api_keys (user_id, key_hash, name, created_at) VALUES (?, ?, 'test', ?)", (user_id, hashlib.sha256(key.encode()).hexdigest(), now_iso()))
        conn.commit(); conn.close()
        yield client, key, user_id

def event(**overrides):
    value = {"type": "task.started", "task": "ENG-42", "repo": "acme/app", "summary": "started", "payload": {}}
    value.update(overrides); return value

@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer bad"}])
def test_missing_or_bad_key_401(personal_api, headers):
    assert personal_api[0].post("/v1/events", json=event(), headers=headers).status_code == 401

def test_revoked_key_401(personal_api):
    client, key, _ = personal_api
    conn = db.get_connection(); conn.execute("UPDATE api_keys SET revoked_at = ?", (now_iso(),)); conn.commit(); conn.close()
    assert client.post("/v1/events", json=event(), headers={"Authorization": f"Bearer {key}"}).status_code == 401

def test_owner_hint_dedupe_and_redaction(personal_api):
    client, key, user_id = personal_api; h = {"Authorization": f"Bearer {key}"}
    body = event(externalId="same", engineer="body-person", summary=f"{stripe_live_key()} {github_token()}", reason=f"reason {stripe_live_key()}")
    assert client.post("/v1/events", json=body, headers=h).json()["inserted"] == 1
    assert client.post("/v1/events", json=body, headers=h).json()["deduplicated"] == 1
    row = db.get_connection().execute("SELECT * FROM events WHERE external_id='same'").fetchone()
    assert row["actor_user_id"] == user_id and row["actor_hint"] == "body-person"
    assert "FAKEKEY" not in row["summary"] and "FAKEKEY" not in row["reason"]

def test_forbidden_payload_and_limits(personal_api):
    client, key, _ = personal_api; h = {"Authorization": f"Bearer {key}"}
    assert client.post("/v1/events", json=event(payload={"prompt": "do not store"}), headers=h).json()["rejected"]
    assert client.post("/v1/events", json={"events": [event(externalId=str(i)) for i in range(100)]}, headers=h).status_code == 201
    assert client.post("/v1/events", json={"events": [event() for _ in range(101)]}, headers=h).status_code == 413
    assert client.post("/v1/events", content=json.dumps(event(summary="x" * (256 * 1024))), headers={**h, "Content-Type": "application/json"}).status_code == 413

def test_events_database_is_append_only(personal_api):
    client, key, _ = personal_api
    client.post("/v1/events", json=event(), headers={"Authorization": f"Bearer {key}"})
    conn = db.get_connection(); row = conn.execute("SELECT id FROM events").fetchone()
    with pytest.raises(Exception): conn.execute("UPDATE events SET summary='x' WHERE id=?", (row["id"],))
    with pytest.raises(Exception): conn.execute("DELETE FROM events WHERE id=?", (row["id"],))
    conn.close()

def test_filters_and_cursor(personal_api):
    client, key, _ = personal_api; h = {"Authorization": f"Bearer {key}"}
    for i in range(3):
        assert client.post("/v1/events", json=event(type="build.completed", repo="acme/filter", task="ENG-9", summary=f"needle {i}", externalId=f"filter-{i}"), headers=h).status_code == 201
    response = client.get("/v1/events?type=build.completed&repo=acme/filter&task=ENG-9&search=needle&limit=2", headers=h)
    assert len(response.json()) == 2
    cursor = response.json()[-1]["id"]
    next_page = client.get(f"/v1/events?cursor={cursor}&limit=2", headers=h).json()
    assert not ({row["id"] for row in response.json()} & {row["id"] for row in next_page})

def test_demo_requires_opt_in():
    import subprocess, sys
    result = subprocess.run([sys.executable, "scripts/emit_demo_events.py"], capture_output=True, text=True)
    assert result.returncode != 0 and "TRAC_ALLOW_DEMO=1" in result.stderr
