import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from trac import db
from trac.main import _rate_limit_hits, _rate_limit_lock, app
from trac.models import now_iso


@pytest.fixture(autouse=True)
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    with _rate_limit_lock:
        _rate_limit_hits.clear()
    with TestClient(app) as client:
        yield client


@pytest.fixture
def device():
    token = "test-device-token"
    conn = db.get_connection()
    user_cursor = conn.execute(
        "INSERT INTO users (github_login, display_name, created_at) VALUES (?, ?, ?)",
        ("test-user", "Test User", now_iso()),
    )
    cursor = conn.execute(
        """
        INSERT INTO devices (user_id, token_hash, label, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            user_cursor.lastrowid,
            hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "test device",
            now_iso(),
        ),
    )
    conn.commit()
    device_id = cursor.lastrowid
    conn.close()
    return token, device_id


def activity_event(event_id="01JGFJJZ00A1B2C3D4E5F6G7H8", **overrides):
    event = {
        "event_id": event_id,
        "schema_version": 1,
        "type": "task.completed",
        "ts": "2025-01-02T03:04:05Z",
        "repo": "org/project",
        "branch": "main",
        "data": {"summary": "Ship the release"},
    }
    event.update(overrides)
    return event


def post_activity(api, token, events):
    return api.post(
        "/v1/events",
        headers={"Authorization": f"Bearer {token}"},
        json={"events": events},
    )


def test_missing_bad_and_revoked_tokens_return_401(api, device):
    token, device_id = device
    assert api.post("/v1/events", json={"events": []}).status_code == 401
    assert post_activity(api, "not-valid", []).status_code == 401

    conn = db.get_connection()
    conn.execute(
        "UPDATE devices SET revoked_at = ? WHERE id = ?",
        (now_iso(), device_id),
    )
    conn.commit()
    conn.close()

    assert post_activity(api, token, []).status_code == 401


def test_duplicate_event_id_is_ignored(api, device):
    token, _ = device
    event = activity_event()

    first = post_activity(api, token, [event])
    second = post_activity(api, token, [event])

    assert first.json() == {"accepted": 1, "duplicates": 0, "rejected": []}
    assert second.json() == {"accepted": 0, "duplicates": 1, "rejected": []}


def test_forbidden_field_rejects_event(api, device):
    token, _ = device
    event = activity_event(data={"summary": "safe", "prompt": "must not be stored"})

    response = post_activity(api, token, [event])

    assert response.json()["rejected"] == [
        {"event_id": event["event_id"], "reason": "forbidden_field"}
    ]


def test_unknown_data_fields_are_dropped(api, device):
    token, _ = device
    event = activity_event(data={"summary": "Ship", "custom": "ignored"})

    response = post_activity(api, token, [event])

    assert response.json()["accepted"] == 1
    conn = db.get_connection()
    stored = conn.execute(
        "SELECT data FROM activity_events WHERE event_id = ?",
        (event["event_id"],),
    ).fetchone()
    conn.close()
    assert json.loads(stored["data"]) == {"summary": "Ship"}


def test_batch_over_100_events_returns_413(api, device):
    token, _ = device
    events = [
        activity_event(event_id=f"01JGFJJZ00A1B2C3D4E5F6G7{index:02X}")
        for index in range(101)
    ]

    assert post_activity(api, token, events).status_code == 413


def test_body_over_256_kb_returns_413(api, device):
    token, _ = device

    response = api.post(
        "/v1/events",
        headers={"Authorization": f"Bearer {token}"},
        content=b"x" * (256 * 1024 + 1),
    )

    assert response.status_code == 413


def test_rate_limit_returns_retry_after(api, device):
    token, _ = device
    for _ in range(120):
        response = post_activity(api, token, [])
        assert response.status_code == 202

    limited = post_activity(api, token, [])

    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) > 0


def test_manage_cli_creates_and_revokes_a_hashed_device_token(capsys):
    from trac.manage import main

    assert main(["create-user", "cli-user"]) == 0
    token = capsys.readouterr().out.strip()
    assert token

    conn = db.get_connection()
    row = conn.execute(
        """
        SELECT devices.id, devices.token_hash
        FROM devices JOIN users ON users.id = devices.user_id
        WHERE users.github_login = ?
        """,
        ("cli-user",),
    ).fetchone()
    conn.close()
    assert row["token_hash"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert token not in row["token_hash"]

    assert main(["revoke-device", str(row["id"])]) == 0
    conn = db.get_connection()
    revoked_at = conn.execute(
        "SELECT revoked_at FROM devices WHERE id = ?", (row["id"],)
    ).fetchone()["revoked_at"]
    conn.close()
    assert revoked_at


def test_accepted_task_is_mirrored_to_dashboard_feed(api, device):
    token, _ = device
    event = activity_event(
        type="git.commit",
        actor="Spoofed actor",
        data={"files_changed": 3, "lines_added": 42, "lines_removed": 7},
    )

    response = post_activity(api, token, [event])
    feed = api.get("/api/events", params={"source": "trac-activity"})

    assert response.json()["accepted"] == 1
    assert feed.status_code == 200
    mirrored = feed.json()[0]
    assert mirrored["type"] == "activity.git.commit"
    assert mirrored["source"] == "trac-activity"
    assert mirrored["actor"] == "Test User"
    assert mirrored["repo"] == "org/project"
    assert mirrored["ref"] == "main"
    assert mirrored["summary"] == "Committed 3 files (+42 -7)"


def test_failed_test_run_is_mirrored_with_status_and_safe_metadata(api, device):
    token, _ = device
    event = activity_event(
        type="command.run",
        data={"kind": "test", "exit_code": 1, "duration_s": 12},
    )

    response = post_activity(api, token, [event])
    feed = api.get("/api/events", params={"source": "trac-activity"})

    assert response.json()["accepted"] == 1
    assert feed.status_code == 200
    mirrored = feed.json()[0]
    assert mirrored["type"] == "activity.command.run"
    assert mirrored["status"] == "failed"
    assert mirrored["summary"] == "Test run failed (exit code 1)"
    assert mirrored["metadata"] == {
        "kind": "test",
        "exit_code": 1,
        "duration_s": 12,
    }


def test_existing_api_events_endpoint_still_works(api):
    response = api.post(
        "/api/events",
        json={
            "type": "agent.review_completed",
            "source": "test",
            "summary": "Review finished",
            "metadata": {"count": 1},
        },
    )

    assert response.status_code == 200
    assert response.json()["type"] == "agent.review_completed"
    assert response.json()["metadata"] == {"count": 1}


def test_stream_route_precedes_parameterized_event_route():
    paths = [
        route.path
        for route in app.routes
        if route.path in {"/api/events/stream", "/api/events/{event_id}"}
    ]

    assert paths == ["/api/events/stream", "/api/events/{event_id}"]
