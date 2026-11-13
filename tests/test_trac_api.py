import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from trac import db
from trac.main import _rate_limit_hits, _rate_limit_lock, app, stream_activity_events
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
        """
        INSERT INTO users (github_login, display_name, created_at, public_id)
        VALUES (?, ?, ?, ?)
        """,
        ("test-user", "Test User", now_iso(), str(uuid.uuid4())),
    )
    project_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
        (project_id, "test-project", "Test Project", now_iso()),
    )
    conn.execute(
        """
        INSERT INTO project_memberships (project_id, user_id, role, created_at)
        VALUES (?, ?, 'owner', ?)
        """,
        (project_id, user_cursor.lastrowid, now_iso()),
    )
    cursor = conn.execute(
        """
        INSERT INTO devices (user_id, token_hash, label, created_at, project_id, scopes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            user_cursor.lastrowid,
            hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "test device",
            now_iso(),
            project_id,
            '["events:ingest","events:read"]',
        ),
    )
    conn.commit()
    device_id = cursor.lastrowid
    conn.close()
    return token, device_id


@pytest.fixture
def second_device():
    token = "second-project-token"
    conn = db.get_connection()
    user_cursor = conn.execute(
        """
        INSERT INTO users (github_login, display_name, created_at, public_id)
        VALUES (?, ?, ?, ?)
        """,
        ("other-user", "Other User", now_iso(), str(uuid.uuid4())),
    )
    project_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO projects (id, slug, name, created_at) VALUES (?, ?, ?, ?)",
        (project_id, "other-project", "Other Project", now_iso()),
    )
    conn.execute(
        """
        INSERT INTO project_memberships (project_id, user_id, role, created_at)
        VALUES (?, ?, 'owner', ?)
        """,
        (project_id, user_cursor.lastrowid, now_iso()),
    )
    conn.execute(
        """
        INSERT INTO devices (user_id, token_hash, label, created_at, project_id, scopes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            user_cursor.lastrowid,
            hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "other device",
            now_iso(),
            project_id,
            '["events:ingest","events:read"]',
        ),
    )
    conn.commit()
    conn.close()
    return token


def add_project_member(project_id, github_login, role="member"):
    token = f"{github_login}-project-token"
    public_id = str(uuid.uuid4())
    conn = db.get_connection()
    user = conn.execute(
        """
        INSERT INTO users
            (github_login, github_username, display_name, created_at, public_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (github_login, github_login, github_login.title(), now_iso(), public_id),
    )
    user_id = user.lastrowid
    conn.execute(
        """
        INSERT INTO project_memberships (project_id, user_id, role, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (project_id, user_id, role, now_iso()),
    )
    conn.execute(
        """
        INSERT INTO devices
            (user_id, token_hash, label, created_at, project_id, scopes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "test member device",
            now_iso(),
            project_id,
            '["events:read","events:ingest"]'
            if role == "member"
            else '["events:read"]',
        ),
    )
    conn.commit()
    conn.close()
    return token, public_id


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


def canonical_event(**overrides):
    event = {
        "event_id": str(uuid.uuid4()),
        "schema_version": 2,
        "event_type": "session.started",
        "source": "skill",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "metadata": {},
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


def test_legacy_read_and_stream_require_authentication(api):
    assert api.get("/api/events").status_code == 401
    assert api.get("/v1/events").status_code == 401
    assert api.get("/v1/events/stream").status_code == 401


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

    assert response.json()["rejected"] == [
        {"event_id": event["event_id"], "reason": "invalid_event"}
    ]


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
        data={"files_changed": 3, "lines_added": 42, "lines_removed": 7},
    )
    headers = {"Authorization": f"Bearer {token}"}
    response = post_activity(api, token, [event])
    feed = api.get("/api/events", params={"source": "trac-activity"}, headers=headers)

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
    feed = api.get(
        "/api/events",
        params={"source": "trac-activity"},
        headers={"Authorization": f"Bearer {token}"},
    )

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


def test_existing_api_events_endpoint_still_works(api, device):
    token, _ = device
    response = api.post(
        "/api/events",
        headers={"Authorization": f"Bearer {token}"},
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
    assert (
        api.get("/v1/events", headers={"Authorization": f"Bearer {token}"}).json() == []
    )


def test_legacy_endpoint_rejects_sensitive_metadata(api, device):
    token, _ = device
    response = api.post(
        "/api/events",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "type": "agent.review_completed",
            "source": "code-review-agent",
            "summary": "Review finished",
            "metadata": {"api_key": "sk-live-FAKEKEY1234567890"},
        },
    )

    assert response.status_code == 422


def test_canonical_event_uses_credential_identity_and_project(api, device):
    token, device_id = device
    task_id = str(uuid.uuid4())
    conn = db.get_connection()
    credential = conn.execute(
        "SELECT user_id, project_id FROM devices WHERE id = ?", (device_id,)
    ).fetchone()
    conn.execute(
        """
        INSERT INTO tasks
            (id, project_id, title, created_at, updated_at)
        VALUES (?, ?, 'Identity test task', ?, ?)
        """,
        (task_id, credential["project_id"], now_iso(), now_iso()),
    )
    conn.commit()
    conn.close()
    event = canonical_event(
        project_id=str(uuid.uuid4()),
        actor_id=str(uuid.uuid4()),
        task_id=task_id,
        event_type="task.progress",
        metadata={"progress": 35},
    )
    response = api.post(
        "/v1/events",
        headers={"Authorization": f"Bearer {token}"},
        json=event,
    )

    assert response.status_code == 202
    assert response.json() == {"accepted": 1, "duplicates": 0, "rejected": []}
    stored = api.get(
        f"/v1/events/{event['event_id']}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert stored.status_code == 200
    assert stored.json()["project_id"] != event["project_id"]
    assert stored.json()["actor_id"] != event["actor_id"]
    assert stored.json()["task_id"] == task_id
    task = api.get(
        f"/v1/tasks/{task_id}",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert task["progress_percent"] == 35
    assert task["progress_is_estimate"] is True
    assert task["updates"][0]["event_id"] == event["event_id"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"event_type": "unknown.event"},
        {"event_type": "task.progress", "metadata": {"progress": 101}},
        {"event_type": "task.progress", "metadata": {"progress": "60"}},
        {"event_type": "task.started", "metadata": {"extra": "not allowed"}},
        {"event_type": "task.started", "metadata": {"prompt": "private instructions"}},
    ],
)
def test_invalid_v2_event_contract_is_rejected(api, device, overrides):
    token, _ = device
    event = canonical_event(**overrides)

    response = api.post(
        "/v1/events",
        headers={"Authorization": f"Bearer {token}"},
        json=event,
    )

    assert response.status_code == 422
    assert (
        api.get("/v1/events", headers={"Authorization": f"Bearer {token}"}).json() == []
    )


def test_duplicate_v2_event_id_is_idempotent(api, device):
    token, _ = device
    event = canonical_event()
    headers = {"Authorization": f"Bearer {token}"}

    first = api.post("/v1/events", headers=headers, json=event)
    second = api.post("/v1/events", headers=headers, json=event)

    assert first.json()["accepted"] == 1
    assert second.json()["duplicates"] == 1
    conn = db.get_connection()
    count = conn.execute(
        "SELECT COUNT(*) FROM activity_events WHERE event_id = ?", (event["event_id"],)
    ).fetchone()[0]
    stream_count = conn.execute(
        "SELECT COUNT(*) FROM activity_event_stream WHERE event_id = ?",
        (event["event_id"],),
    ).fetchone()[0]
    conn.close()
    assert count == stream_count == 1


def test_project_reads_and_duplicate_ids_are_isolated(api, device, second_device):
    token, _ = device
    event = canonical_event()
    post_activity_result = api.post(
        "/v1/events",
        headers={"Authorization": f"Bearer {token}"},
        json=event,
    )

    other_headers = {"Authorization": f"Bearer {second_device}"}
    assert post_activity_result.status_code == 202
    assert api.get("/v1/events", headers=other_headers).json() == []
    assert (
        api.get(f"/v1/events/{event['event_id']}", headers=other_headers).status_code
        == 404
    )

    collision = api.post("/v1/events", headers=other_headers, json=event)
    assert collision.status_code == 409


def test_viewer_credential_cannot_ingest(api, device):
    token, device_id = device
    conn = db.get_connection()
    conn.execute(
        "UPDATE devices SET scopes = ? WHERE id = ?",
        ('["events:read"]', device_id),
    )
    conn.commit()
    conn.close()

    response = api.post(
        "/v1/events",
        headers={"Authorization": f"Bearer {token}"},
        json=canonical_event(),
    )
    assert response.status_code == 403


def test_manage_project_members_receive_project_scoped_permissions(api):
    from trac.manage import add_project_member, create_project, create_user

    create_user("project-owner")
    create_user("project-viewer")
    owner_token = create_project("shared-project", "Shared Project", "project-owner")
    viewer_token = add_project_member("shared-project", "project-viewer", "viewer")

    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    viewer_headers = {"Authorization": f"Bearer {viewer_token}"}
    assert (
        api.post(
            "/v1/events", headers=owner_headers, json=canonical_event()
        ).status_code
        == 202
    )
    assert api.get("/v1/events", headers=viewer_headers).status_code == 200
    assert (
        api.post(
            "/v1/events", headers=viewer_headers, json=canonical_event()
        ).status_code
        == 403
    )


def test_legacy_execution_replay_does_not_duplicate_stream_events(api, device):
    token, _ = device
    headers = {"Authorization": f"Bearer {token}"}
    event = {
        "type": "agent.review_completed",
        "source": "code-review-agent",
        "summary": "Review finished",
        "execution_id": "execution-123",
        "metadata": {"issue_counts": {"major": 1}},
    }

    assert api.post("/api/events", headers=headers, json=event).status_code == 200
    assert api.post("/api/events", headers=headers, json=event).status_code == 200
    conn = db.get_connection()
    stream_count = conn.execute(
        """
        SELECT COUNT(*) FROM activity_event_stream
        JOIN activity_events
          ON activity_events.event_id = activity_event_stream.event_id
        WHERE activity_events.source = 'compat-api'
        """
    ).fetchone()[0]
    conn.close()
    assert stream_count == 1


def test_sse_delivers_persisted_event(api, device):
    token, _ = device
    headers = {"Authorization": f"Bearer {token}"}
    event = canonical_event()

    async def run():
        request = SimpleNamespace(
            is_disconnected=lambda: asyncio.sleep(0, result=False)
        )
        response = await stream_activity_events(
            request, authorization=headers["Authorization"], last_event_id=None
        )
        iterator = response.body_iterator
        assert await iterator.__anext__() == "retry: 3000\n\n"
        posted = await asyncio.to_thread(
            api.post, "/v1/events", headers=headers, json=event
        )
        assert posted.json()["accepted"] == 1
        frame = await asyncio.wait_for(iterator.__anext__(), timeout=3)
        await iterator.aclose()
        return frame

    frame = asyncio.run(run())
    assert f"id: {event['event_id']}\n" in frame
    payload = json.loads(
        next(line[6:] for line in frame.splitlines() if line.startswith("data: "))
    )
    assert payload["event_id"] == event["event_id"]
    assert payload["event_type"] == event["event_type"]


def test_initial_feed_cursor_catches_events_created_before_stream_connect(api, device):
    token, _ = device
    headers = {"Authorization": f"Bearer {token}"}
    feed = api.get("/api/events", headers=headers)
    assert feed.headers["X-TRAC-Event-Cursor"] == "0"
    event = canonical_event()
    assert api.post("/v1/events", headers=headers, json=event).json()["accepted"] == 1

    async def run():
        request = SimpleNamespace(
            is_disconnected=lambda: asyncio.sleep(0, result=False)
        )
        response = await stream_activity_events(
            request,
            authorization=headers["Authorization"],
            last_event_id="0",
        )
        iterator = response.body_iterator
        assert await iterator.__anext__() == "retry: 3000\n\n"
        frame = await asyncio.wait_for(iterator.__anext__(), timeout=3)
        await iterator.aclose()
        return frame

    frame = asyncio.run(run())
    assert f"id: {event['event_id']}\n" in frame


def test_sse_emits_heartbeat_when_idle(api, device, monkeypatch):
    token, _ = device
    monkeypatch.setattr("trac.main.STREAM_HEARTBEAT_SECONDS", 0)

    async def run():
        request = SimpleNamespace(
            is_disconnected=lambda: asyncio.sleep(0, result=False)
        )
        response = await stream_activity_events(
            request,
            authorization=f"Bearer {token}",
            last_event_id=None,
        )
        iterator = response.body_iterator
        assert await iterator.__anext__() == "retry: 3000\n\n"
        frame = await iterator.__anext__()
        await iterator.aclose()
        return frame

    assert asyncio.run(run()) == ": heartbeat\n\n"


def test_sse_stops_after_client_disconnect(api, device):
    token, _ = device

    async def run():
        request = SimpleNamespace(is_disconnected=lambda: asyncio.sleep(0, result=True))
        response = await stream_activity_events(
            request,
            authorization=f"Bearer {token}",
            last_event_id=None,
        )
        iterator = response.body_iterator
        assert await iterator.__anext__() == "retry: 3000\n\n"
        with pytest.raises(StopAsyncIteration):
            await iterator.__anext__()

    asyncio.run(run())


def test_sse_reconnect_replays_only_events_after_cursor(api, device):
    token, _ = device
    headers = {"Authorization": f"Bearer {token}"}
    first = canonical_event()
    second = canonical_event()
    assert api.post("/v1/events", headers=headers, json=first).json()["accepted"] == 1

    async def run():
        request = SimpleNamespace(
            is_disconnected=lambda: asyncio.sleep(0, result=False)
        )
        response = await stream_activity_events(
            request,
            authorization=headers["Authorization"],
            last_event_id=first["event_id"],
        )
        iterator = response.body_iterator
        assert await iterator.__anext__() == "retry: 3000\n\n"
        posted = await asyncio.to_thread(
            api.post, "/v1/events", headers=headers, json=second
        )
        assert posted.json()["accepted"] == 1
        frame = await asyncio.wait_for(iterator.__anext__(), timeout=3)
        await iterator.aclose()
        return frame

    frame = asyncio.run(run())
    assert f"id: {second['event_id']}\n" in frame
    payload = json.loads(
        next(line[6:] for line in frame.splitlines() if line.startswith("data: "))
    )
    assert payload["event_id"] == second["event_id"]


def test_legacy_api_events_are_sent_as_compatibility_frames(api, device):
    token, _ = device
    headers = {"Authorization": f"Bearer {token}"}
    request_payload = {
        "type": "agent.review_completed",
        "source": "code-review-agent",
        "summary": "Review finished",
        "metadata": {"issue_counts": {"major": 1}},
    }

    async def run():
        request = SimpleNamespace(
            is_disconnected=lambda: asyncio.sleep(0, result=False)
        )
        response = await stream_activity_events(
            request, authorization=headers["Authorization"], last_event_id=None
        )
        iterator = response.body_iterator
        assert await iterator.__anext__() == "retry: 3000\n\n"
        posted = await asyncio.to_thread(
            api.post, "/api/events", headers=headers, json=request_payload
        )
        assert posted.status_code == 200
        frame = await asyncio.wait_for(iterator.__anext__(), timeout=3)
        await iterator.aclose()
        return frame

    frame = asyncio.run(run())
    assert "event: compatibility\n" in frame
    payload = json.loads(
        next(line[6:] for line in frame.splitlines() if line.startswith("data: "))
    )
    assert payload["event_type"] == "agent.review_completed"
    assert payload["source"] == "github"


def test_stream_route_precedes_parameterized_event_route():
    paths = [
        route.path
        for route in app.routes
        if route.path in {"/api/events/stream", "/api/events/{event_id}"}
    ]

    assert paths == ["/api/events/stream", "/api/events/{event_id}"]


def test_project_creation_issues_project_scoped_hashed_credential(api, device):
    token, _ = device
    response = api.post(
        "/v1/projects",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "New Engineering Project",
            "description": "A project created through the API",
            "project_key": "new-engineering-project",
        },
    )

    assert response.status_code == 201
    project = response.json()
    assert project["project_key"] == "new-engineering-project"
    assert project["project_head_id"]
    assert project["device_token"]
    headers = {"Authorization": f"Bearer {project['device_token']}"}
    loaded = api.get(f"/v1/projects/{project['id']}", headers=headers)
    assert loaded.status_code == 200
    assert loaded.json()["project_head_id"] == project["project_head_id"]
    listed = api.get(
        "/v1/projects", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert project["id"] in {item["id"] for item in listed}

    conn = db.get_connection()
    stored_hash = conn.execute(
        "SELECT token_hash FROM devices WHERE project_id = ?",
        (project["id"],),
    ).fetchone()["token_hash"]
    conn.close()
    assert stored_hash == hashlib.sha256(project["device_token"].encode()).hexdigest()


def test_task_assignment_member_access_and_project_head_authorization(api, device):
    owner_token, device_id = device
    conn = db.get_connection()
    project_id = conn.execute(
        "SELECT project_id FROM devices WHERE id = ?", (device_id,)
    ).fetchone()["project_id"]
    conn.close()
    member_token, member_id = add_project_member(project_id, "assigned-member")
    outsider_token, _ = add_project_member(project_id, "other-member")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    member_headers = {"Authorization": f"Bearer {member_token}"}
    outsider_headers = {"Authorization": f"Bearer {outsider_token}"}

    created = api.post(
        f"/v1/projects/{project_id}/tasks",
        headers=owner_headers,
        json={"title": "Implement task lifecycle", "assignee_id": member_id},
    )
    assert created.status_code == 201
    task = created.json()
    assert task["status"] == "todo"
    assert task["progress_percent"] == 0
    assert task["progress_is_estimate"] is False
    assert task["assignee_id"] == member_id

    assert api.get(
        f"/v1/tasks/{task['id']}", headers=member_headers
    ).status_code == 200
    assert api.get("/v1/me/tasks", headers=member_headers).json()[0]["id"] == task["id"]
    assert api.get(
        f"/v1/projects/{project_id}/tasks", headers=member_headers
    ).json()[0]["id"] == task["id"]
    assert api.get(
        f"/v1/tasks/{task['id']}", headers=outsider_headers
    ).status_code == 404
    assert api.patch(
        f"/v1/tasks/{task['id']}",
        headers=outsider_headers,
        json={"status": "in_progress"},
    ).status_code == 404
    assert api.post(
        f"/v1/projects/{project_id}/tasks",
        headers=member_headers,
        json={"title": "Unauthorized task"},
    ).status_code == 403
    assert api.patch(
        f"/v1/tasks/{task['id']}",
        headers=member_headers,
        json={"title": "Member cannot rename"},
    ).status_code == 403

    updated = api.patch(
        f"/v1/tasks/{task['id']}",
        headers=member_headers,
        json={"status": "in_progress", "progress_percent": 45, "note": "Started"},
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "in_progress"
    assert updated.json()["progress_percent"] == 45
    assert updated.json()["progress_is_estimate"] is False
    details = api.get(f"/v1/tasks/{task['id']}", headers=member_headers).json()
    assert details["updates"][0]["note"] == "Started"
    assert details["updates"][0]["event_id"] is None
    overview = api.get(
        f"/v1/projects/{project_id}/overview", headers=owner_headers
    ).json()
    assert overview["total_tasks"] == 1
    assert overview["task_counts"]["in_progress"] == 1


def test_task_event_updates_state_history_and_estimate_idempotently(api, device):
    owner_token, device_id = device
    conn = db.get_connection()
    project_id = conn.execute(
        "SELECT project_id FROM devices WHERE id = ?", (device_id,)
    ).fetchone()["project_id"]
    conn.close()
    member_token, member_id = add_project_member(project_id, "task-agent")
    task = api.post(
        f"/v1/projects/{project_id}/tasks",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"title": "Run a task event", "assignee_id": member_id},
    ).json()
    member_headers = {"Authorization": f"Bearer {member_token}"}
    started = canonical_event(event_type="task.started", task_id=task["id"])
    started_response = api.post("/v1/events", headers=member_headers, json=started)
    assert started_response.json()["accepted"] == 1
    in_progress = api.get(
        f"/v1/tasks/{task['id']}", headers=member_headers
    ).json()
    assert in_progress["status"] == "in_progress"
    assert in_progress["updates"][0]["event_id"] == started["event_id"]

    event = canonical_event(
        event_type="task.progress",
        task_id=task["id"],
        actor_id=str(uuid.uuid4()),
        project_id=str(uuid.uuid4()),
        metadata={"progress": 63},
    )

    first = api.post("/v1/events", headers=member_headers, json=event)
    duplicate = api.post("/v1/events", headers=member_headers, json=event)
    assert first.json()["accepted"] == 1
    assert duplicate.json()["duplicates"] == 1
    completed = canonical_event(
        event_type="task.completed",
        task_id=task["id"],
        metadata={"outcome": "done"},
    )
    assert (
        api.post("/v1/events", headers=member_headers, json=completed).json()[
            "accepted"
        ]
        == 1
    )
    details = api.get(f"/v1/tasks/{task['id']}", headers=member_headers).json()
    assert details["status"] == "done"
    assert details["progress_percent"] == 100
    assert details["progress_is_estimate"] is False
    assert len(details["updates"]) == 3
    assert {update["event_id"] for update in details["updates"]} == {
        started["event_id"],
        event["event_id"],
        completed["event_id"],
    }
    progress_update = next(
        update
        for update in details["updates"]
        if update["event_id"] == event["event_id"]
    )
    assert progress_update["progress_percent"] == 63
    assert progress_update["progress_is_estimate"] is True
    assert progress_update["actor_id"] == member_id
    stored = api.get(
        f"/v1/events/{event['event_id']}", headers=member_headers
    ).json()
    assert stored["actor_id"] == member_id


def test_task_event_requires_existing_authorized_task_and_task_id(
    api, device, second_device
):
    owner_token, device_id = device
    conn = db.get_connection()
    project_id = conn.execute(
        "SELECT project_id FROM devices WHERE id = ?", (device_id,)
    ).fetchone()["project_id"]
    conn.close()
    member_token, member_id = add_project_member(project_id, "task-member")
    task = api.post(
        f"/v1/projects/{project_id}/tasks",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"title": "Assigned task", "assignee_id": member_id},
    ).json()
    unauthorized = canonical_event(
        event_type="task.started", task_id=task["id"], metadata={}
    )
    assert api.post(
        "/v1/events",
        headers={"Authorization": f"Bearer {second_device}"},
        json=unauthorized,
    ).status_code == 404

    unassigned_token, _ = add_project_member(project_id, "unassigned-member")
    denied = canonical_event(
        event_type="task.started", task_id=task["id"], metadata={}
    )
    assert api.post(
        "/v1/events",
        headers={"Authorization": f"Bearer {unassigned_token}"},
        json=denied,
    ).status_code == 403

    missing_task = canonical_event(event_type="task.started", metadata={})
    assert api.post(
        "/v1/events",
        headers={"Authorization": f"Bearer {member_token}"},
        json=missing_task,
    ).status_code == 422


def test_task_progress_validation_and_cross_project_isolation(
    api, device, second_device
):
    owner_token, device_id = device
    conn = db.get_connection()
    project_id = conn.execute(
        "SELECT project_id FROM devices WHERE id = ?", (device_id,)
    ).fetchone()["project_id"]
    conn.close()
    task = api.post(
        f"/v1/projects/{project_id}/tasks",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"title": "Private task"},
    ).json()
    headers = {"Authorization": f"Bearer {owner_token}"}
    assert api.patch(
        f"/v1/tasks/{task['id']}",
        headers=headers,
        json={"progress_percent": 101},
    ).status_code == 422
    assert api.patch(
        f"/v1/tasks/{task['id']}",
        headers=headers,
        json={"progress_percent": "45"},
    ).status_code == 422
    assert (
        api.patch(
            f"/v1/tasks/{task['id']}", headers=headers, json={"status": "finished"}
        ).status_code
        == 422
    )
    other_headers = {"Authorization": f"Bearer {second_device}"}
    assert (
        api.get(f"/v1/tasks/{task['id']}", headers=other_headers).status_code == 404
    )
    assert api.patch(
        f"/v1/tasks/{task['id']}",
        headers=other_headers,
        json={"status": "blocked"},
    ).status_code == 404


def test_members_only_see_own_or_assigned_activity(api, device):
    owner_token, device_id = device
    conn = db.get_connection()
    project_id = conn.execute(
        "SELECT project_id FROM devices WHERE id = ?", (device_id,)
    ).fetchone()["project_id"]
    conn.close()
    member_token, member_id = add_project_member(project_id, "activity-member")
    task = api.post(
        f"/v1/projects/{project_id}/tasks",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"title": "Visible assigned task", "assignee_id": member_id},
    ).json()
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    member_headers = {"Authorization": f"Bearer {member_token}"}
    hidden = canonical_event()
    visible = canonical_event(event_type="task.started", task_id=task["id"])
    assert (
        api.post("/v1/events", headers=owner_headers, json=hidden).status_code == 202
    )
    assert (
        api.post("/v1/events", headers=owner_headers, json=visible).status_code == 202
    )

    visible_ids = {
        event["event_id"]
        for event in api.get("/v1/events", headers=member_headers).json()
    }
    assert visible["event_id"] in visible_ids
    assert hidden["event_id"] not in visible_ids
    assert api.get(
        f"/v1/events/{hidden['event_id']}", headers=member_headers
    ).status_code == 404
    assert api.get(
        f"/v1/projects/{project_id}/activity", headers=member_headers
    ).status_code == 403
    assert api.get(
        f"/v1/projects/{project_id}/activity", headers=owner_headers
    ).status_code == 200

    async def stream_member_activity():
        request = SimpleNamespace(
            is_disconnected=lambda: asyncio.sleep(0, result=False)
        )
        response = await stream_activity_events(
            request,
            authorization=member_headers["Authorization"],
            last_event_id="0",
        )
        iterator = response.body_iterator
        assert await iterator.__anext__() == "retry: 3000\n\n"
        frame = await asyncio.wait_for(iterator.__anext__(), timeout=3)
        await iterator.aclose()
        return frame

    frame = asyncio.run(stream_member_activity())
    assert visible["event_id"] in frame
    assert hidden["event_id"] not in frame


def test_development_login_is_opt_in_and_never_works_in_production(
    api, device, monkeypatch
):
    _token, device_id = device
    conn = db.get_connection()
    credential = conn.execute(
        """
        SELECT users.github_username, users.github_login, devices.project_id
        FROM devices JOIN users ON users.id = devices.user_id
        WHERE devices.id = ?
        """,
        (device_id,),
    ).fetchone()
    conn.close()
    payload = {
        "github_username": credential["github_username"] or credential["github_login"],
        "project_id": credential["project_id"],
    }
    assert api.post("/v1/dev/login", json=payload).status_code == 404

    monkeypatch.setenv("TRAC_ENV", "development")
    monkeypatch.setenv("TRAC_DEV_AUTH_ENABLED", "true")
    login = api.post("/v1/dev/login", json=payload)
    assert login.status_code == 200
    development_token = login.json()["access_token"]
    assert api.get(
        "/v1/projects",
        headers={"Authorization": f"Bearer {development_token}"},
    ).status_code == 200
    conn = db.get_connection()
    credential = conn.execute(
        "SELECT token_hash, auth_mode FROM devices WHERE token_hash = ?",
        (hashlib.sha256(development_token.encode()).hexdigest(),),
    ).fetchone()
    conn.close()
    assert credential["auth_mode"] == "development"
    assert credential["token_hash"] != development_token

    monkeypatch.setenv("TRAC_ENV", "production")
    assert api.post("/v1/dev/login", json=payload).status_code == 404
    assert api.get(
        "/v1/projects",
        headers={"Authorization": f"Bearer {development_token}"},
    ).status_code == 401
