"""Intentional red tests for the single-source timeline contract."""
import hashlib, hmac, json, re, subprocess, sys, uuid
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from trac import db
from trac.main import app, append_events, redact_value, stream_personal_events
from trac.manage import map_github_repository
from trac.models import now_iso
from tests.fake_secrets import (
    aws_access_key,
    bearer_token,
    github_app_credential,
    github_token,
    private_key_block,
    slack_token,
    stripe_live_key,
)


@pytest.fixture
def timeline(tmp_path, monkeypatch):
    path = tmp_path / "timeline.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("TRAC_DB_PATH", str(path))
    monkeypatch.setenv("TRAC_GITHUB_WEBHOOK_SECRET", "timeline-webhook-secret")
    with TestClient(app) as client:
        conn = db.get_connection()
        users = {}
        for name in ("alice", "bob", "admin"):
            role = "admin" if name == "admin" else "developer"
            cur = conn.execute("INSERT INTO users (github_login, display_name, role, created_at, public_id) VALUES (?, ?, ?, ?, ?)", (name, name.title(), role, now_iso(), str(uuid.uuid4())))
            users[name] = cur.lastrowid
        project = str(uuid.uuid4())
        conn.execute("INSERT INTO projects (id, slug, name, project_key, project_head_id, created_at) VALUES (?, 'timeline', 'Timeline', 'TIM', ?, ?)", (project, users["admin"], now_iso()))
        for user_id in users.values():
            conn.execute("INSERT INTO project_memberships (project_id, user_id, role, created_at) VALUES (?, ?, 'owner', ?)", (project, user_id, now_iso()))
        devices = {}
        for name in ("alice", "bob", "admin", "mcp"):
            owner = users["alice"] if name == "mcp" else users[name]
            token = f"legacy-{name}-device-token"
            cur = conn.execute("INSERT INTO devices (user_id, token_hash, label, created_at, project_id, scopes) VALUES (?, ?, ?, ?, ?, ?)", (owner, hashlib.sha256(token.encode()).hexdigest(), name, now_iso(), project, '["events:read","events:ingest"]'))
            devices[name] = (token, cur.lastrowid, owner)
        keys = {}
        for name in ("alice", "bob"):
            token = f"trc_{name}_personal_key_123456789"
            cur = conn.execute("INSERT INTO api_keys (user_id, key_hash, name, created_at) VALUES (?, ?, ?, ?)", (users[name], hashlib.sha256(token.encode()).hexdigest(), name, now_iso()))
            keys[name] = (token, cur.lastrowid, users[name])
        task = str(uuid.uuid4())
        conn.execute("INSERT INTO tasks (id, project_id, title, assignee_id, created_at, updated_at) VALUES (?, ?, 'Timeline task', ?, ?, ?)", (task, project, users["alice"], now_iso(), now_iso()))
        conn.commit(); conn.close()
        map_github_repository("timeline", "acme/timeline")
        yield {"client": client, "path": path, "users": users, "devices": devices, "keys": keys, "project": project, "task": task}


def personal(t, owner="alice", **extra):
    body = {"type": "task.started", "task": "TIM-1", "repo": "acme/timeline", "summary": "personal event", "payload": {}}
    body.update(extra)
    return t["client"].post("/v1/events", json=body, headers={"Authorization": f"Bearer {t['keys'][owner][0]}"})


def device_headers(t, name="alice"):
    return {"Authorization": f"Bearer {t['devices'][name][0]}"}


def webhook(t, delivery=None):
    delivery = delivery or str(uuid.uuid4())
    payload = {"repository": {"full_name": "acme/timeline"}, "ref": "refs/heads/main", "head_commit": {"id": "a" * 40}}
    body = json.dumps(payload, separators=(",", ":")).encode()
    sig = hmac.new(b"timeline-webhook-secret", body, hashlib.sha256).hexdigest()
    headers = {"X-GitHub-Event": "push", "X-GitHub-Delivery": delivery, "X-Hub-Signature-256": f"sha256={sig}"}
    return t["client"].post("/v1/integrations/github/webhook", content=body, headers=headers)


def assert_http_success(response, writer):
    message = f"{writer}: HTTP {response.status_code}; response body: {response.text}"
    if response.status_code >= 400:
        print(message)
    assert 200 <= response.status_code < 300, message
    print(message)
    return response


def assert_mcp_success(result, writer):
    body = json.dumps(result, sort_keys=True, default=str)
    message = f"{writer}: MCP result; response body: {body}"
    assert isinstance(result, dict) and result.get("ok") is True, message
    print(message)
    return result


@pytest.mark.xfail(strict=True, reason="U1, U3")
def test_personal_event_is_in_dashboard_feed(timeline):
    response = assert_http_success(personal(timeline), "personal event writer")
    rows = timeline["client"].get("/v1/events", headers=device_headers(timeline)).json()
    assert any(row.get("summary") == "personal event" for row in rows), response.text


def test_legacy_event_is_in_v1_events(timeline):
    event_id = "01JGFJJZ00A1B2C3D4E5F6G7H8"
    response = assert_http_success(timeline["client"].post("/v1/events", json={"type": "task.started", "schema_version": 1, "event_id": event_id, "source": "cli", "ts": now_iso(), "data": {"summary": "legacy exact event"}}, headers=device_headers(timeline)), "legacy event writer")
    rows = timeline["client"].get("/v1/events", headers=device_headers(timeline)).json()
    assert any(row.get("event_id") == event_id or row.get("metadata", {}).get("summary") == "legacy exact event" for row in rows), response.text


def test_webhook_is_visible_and_delivery_is_idempotent(timeline):
    delivery = str(uuid.uuid4())
    first = webhook(timeline, delivery)
    second = webhook(timeline, delivery)
    assert first.status_code == 202, f"first webhook writer: HTTP {first.status_code}; response body: {first.text}"
    assert second.status_code == 202 and second.json().get("duplicate") is True, f"duplicate webhook writer: HTTP {second.status_code}; response body: {second.text}"
    assert len(timeline["client"].get("/v1/events", headers=device_headers(timeline)).json()) == 1


@pytest.mark.asyncio
@pytest.mark.xfail(strict=True, reason="U2, U3")
async def test_mcp_lifecycle_is_visible(timeline, monkeypatch):
    from tests.test_trac_mcp import _call_tool
    monkeypatch.setenv("TRAC_DB_PATH", str(timeline["path"]))
    token = timeline["devices"]["mcp"][0]
    assert_mcp_success(await _call_tool(token, "trac_start_task", {"task_id": timeline["task"]}), "MCP start_task")
    assert_mcp_success(await _call_tool(token, "trac_update_progress", {"task_id": timeline["task"], "percent": 50, "note": "halfway"}), "MCP update_progress")
    assert_mcp_success(await _call_tool(token, "trac_complete_task", {"task_id": timeline["task"], "note": "done"}), "MCP complete_task")
    feed_response = timeline["client"].get("/v1/events", headers={"Authorization": f"Bearer {timeline['keys']['alice'][0]}"})
    assert len(feed_response.json()) >= 3, feed_response.text


def test_legacy_reporter_writes_one_events_row(timeline):
    response = timeline["client"].post("/api/events", headers=device_headers(timeline), json={"type": "agent.review_completed", "source": "review-agent", "summary": "review done", "metadata": {}})
    assert response.status_code == 200, f"legacy reporter writer: HTTP {response.status_code}; response body: {response.text}"
    conn = db.get_connection(); count = conn.execute("SELECT COUNT(*) FROM events WHERE summary = 'review done'").fetchone()[0]; conn.close()
    assert count == 1


@pytest.mark.parametrize("writer", [
    "personal",
    pytest.param("legacy_device", marks=pytest.mark.xfail(strict=True, reason="U1 to U3")),
    pytest.param("legacy_reporter", marks=pytest.mark.xfail(strict=True, reason="U1 to U3")),
    pytest.param("canonical", marks=pytest.mark.xfail(strict=True, reason="U1 to U3")),
    pytest.param("webhook", marks=pytest.mark.xfail(strict=True, reason="U1 to U3")),
    pytest.param("mcp", marks=pytest.mark.xfail(strict=True, reason="U1 to U3")),
])
def test_each_writer_reaches_one_stream_and_other_user_isolated(timeline, writer, monkeypatch):
    client = timeline["client"]
    if writer == "personal":
        marker = "stream-personal"; response = assert_http_success(personal(timeline, summary=marker), writer)
    elif writer == "legacy_device":
        marker = "stream-legacy-device"; response = assert_http_success(client.post("/v1/events", json={"type": "task.started", "schema_version": 1, "event_id": "01JGFJJZ00A1B2C3D4E5F6G7H8", "source": "cli", "ts": now_iso(), "data": {"summary": marker}}, headers=device_headers(timeline)), writer)
    elif writer == "legacy_reporter":
        marker = "stream-reporter"; response = assert_http_success(client.post("/api/events", headers=device_headers(timeline), json={"type": "agent.review_completed", "source": "review-agent", "summary": marker, "metadata": {}}), writer)
    elif writer == "canonical":
        marker = "stream-canonical"; response = assert_http_success(client.post("/v1/events", json={"schema_version": 2, "event_id": str(uuid.uuid4()), "event_type": "task.started", "source": "mcp", "occurred_at": now_iso(), "task_id": timeline["task"], "metadata": {}}, headers=device_headers(timeline)), writer)
    elif writer == "webhook":
        marker = "stream-webhook"; response = webhook(timeline)
        assert response.status_code == 202, f"{writer}: HTTP {response.status_code}; response body: {response.text}"
        print(f"{writer}: HTTP {response.status_code}; response body: {response.text}")
    else:
        marker = "stream-mcp"
        from tests.test_trac_mcp import _call_tool
        monkeypatch.setenv("TRAC_DB_PATH", str(timeline["path"]))
        result = asyncio.run(_call_tool(timeline["devices"]["mcp"][0], "trac_start_task", {"task_id": timeline["task"]}))
        assert_mcp_success(result, writer)
    class Request:
        def __init__(self): self.calls = 0
        async def is_disconnected(self): self.calls += 1; return self.calls > 8
    async def read(owner):
        response = await stream_personal_events(Request(), f"Bearer {timeline['keys'][owner][0]}")
        chunks = []
        async for chunk in response.body_iterator: chunks.append(chunk)
        return "".join(chunks)
    alice = asyncio.run(read("alice")); bob = asyncio.run(read("bob"))
    assert marker in alice, f"{writer} writer was {response.text if writer != 'mcp' else json.dumps(result, sort_keys=True)}; marker missing from Alice's personal stream"
    assert marker not in bob


def test_filter_matrix_and_cursor(timeline):
    ids = {}
    for i in range(2):
        response = personal(timeline, summary=f"personal-needle-{i}", externalId=f"filter-personal-{i}", occurredAt=(datetime.now(timezone.utc) - timedelta(hours=i)).isoformat())
        assert response.status_code == 201, f"personal filter writer: HTTP {response.status_code}; response body: {response.text}"
        conn = db.get_connection(); ids[f"p{i}"] = conn.execute("SELECT id FROM events WHERE external_id = ?", (f"filter-personal-{i}",)).fetchone()[0]; conn.close()
    reporter = timeline["client"].post("/api/events", headers=device_headers(timeline), json={"type": "agent.review_completed", "source": "review-agent", "summary": "reporter-needle", "repo": "repo/reporter", "metadata": {}})
    assert 200 <= reporter.status_code < 300, f"reporter filter writer: HTTP {reporter.status_code}; response body: {reporter.text}"
    legacy_id = str(uuid.uuid4())
    legacy = timeline["client"].post("/v1/events", json={"type": "build.completed", "schema_version": 1, "event_id": "01JGFJJZ00A1B2C3D4E5F6G7H8", "source": "cli", "repo": "repo/legacy", "ts": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), "data": {"summary": "legacy-needle", "task": "LEG-2"}}, headers=device_headers(timeline))
    assert 200 <= legacy.status_code < 300, f"legacy filter writer: HTTP {legacy.status_code}; response body: {legacy.text}"
    client = timeline["client"]; headers = {"Authorization": f"Bearer {timeline['keys']['alice'][0]}"}
    expected = {
        "actor=Alice": {ids["p0"], ids["p1"]},
        "type=task.started": {ids["p0"], ids["p1"]},
        "repo=acme/timeline": {ids["p0"], ids["p1"]},
        "task=TIM-1": {ids["p0"], ids["p1"]},
        "since=2020-01-01": {ids["p0"], ids["p1"]},
        "until=2099-01-01": {ids["p0"], ids["p1"]},
        "search=needle": {ids["p0"], ids["p1"]},
        "type=task.started&repo=acme/timeline&task=TIM-1&search=needle": {ids["p0"], ids["p1"]},
    }
    for query, wanted in expected.items():
        rows = client.get(f"/v1/events?{query}", headers=headers).json()
        assert {row["id"] for row in rows} == wanted
    all_rows = client.get("/v1/events?limit=2", headers=headers).json(); page_ids = [row["id"] for row in all_rows]
    while all_rows:
        all_rows = client.get(f"/v1/events?cursor={all_rows[-1]['id']}&limit=2", headers=headers).json()
        page_ids.extend(row["id"] for row in all_rows)
    assert len(page_ids) == len(set(page_ids))


def test_dedupe_is_per_owner(timeline):
    first = assert_http_success(personal(timeline, externalId="shared"), "personal dedupe first writer")
    second = assert_http_success(personal(timeline, externalId="shared"), "personal dedupe repeat writer")
    other_owner = assert_http_success(personal(timeline, "bob", externalId="shared"), "personal other-owner writer")
    assert first.json()["inserted"] == 1, first.text
    assert second.json()["deduplicated"] == 1, second.text
    assert other_owner.json()["inserted"] == 1, other_owner.text


def test_raw_database_files_contain_no_seeded_secrets(timeline):
    secret = stripe_live_key(); ghp = github_token()
    value = f"{secret} {ghp}"
    assert_http_success(personal(
        timeline,
        summary=value,
        reason=value,
        task=value,
        repo=value,
        tool=value,
        engineer=value,
        payload={"nested": {"value": value}, "items": [value, {"value": value}]},
    ), "raw-secret test writer")

    conn = db.get_connection()
    locations = []
    for table_row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ):
        table = table_row["name"].replace('"', '""')
        for column_row in conn.execute(f'PRAGMA table_info("{table}")'):
            column = column_row["name"].replace('"', '""')
            for candidate in (secret, ghp):
                matches = conn.execute(
                    f'SELECT 1 FROM "{table}" WHERE CAST("{column}" AS TEXT) LIKE ? LIMIT 1',
                    (f"%{candidate}%",),
                ).fetchone()
                if matches:
                    locations.append(f"{table_row['name']}.{column_row['name']}")
    assert not locations, f"secrets remained in database columns: {sorted(set(locations))}"
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    for suffix in ("", "-wal", "-shm"):
        path = Path(str(timeline["path"]) + suffix)
        if path.exists():
            raw = path.read_bytes()
            assert secret.encode() not in raw and ghp.encode() not in raw, f"secret remained in {path.name}"

    class Request:
        def __init__(self):
            self.calls = 0

        async def is_disconnected(self):
            self.calls += 1
            return self.calls > 1

    async def read_stream():
        stream = await stream_personal_events(
            Request(), f"Bearer {timeline['keys']['alice'][0]}"
        )
        chunks = []
        async for chunk in stream.body_iterator:
            chunks.append(chunk)
        return "".join(chunks)

    stream_body = asyncio.run(read_stream())
    assert secret not in stream_body and ghp not in stream_body, stream_body

    error_response = timeline["client"].post(
        "/v1/events",
        headers={"Authorization": f"Bearer {timeline['keys']['alice'][0]}"},
        json={"type": "invalid type", "summary": value},
    )
    assert error_response.status_code == 202, error_response.text
    assert secret not in error_response.text and ghp not in error_response.text


def test_redact_value_handles_nested_values_lists_and_unicode():
    private_key = private_key_block()
    values = [
        stripe_live_key("1234567890abcdef"),
        github_token("p", "abcdefghijklmnopqrstuvwxyz0123456789"),
        github_token("o", "abcdefghijklmnopqrstuvwxyz0123456789"),
        github_token("u", "abcdefghijklmnopqrstuvwxyz0123456789"),
        github_token("s", "abcdefghijklmnopqrstuvwxyz0123456789"),
        github_token("r", "abcdefghijklmnopqrstuvwxyz0123456789"),
        github_app_credential(),
        aws_access_key(),
        slack_token(),
        bearer_token(),
        private_key,
        "api_key=0123456789abcdef",
    ]
    data = {"nested": {"values": values, "client_secret": "long-plain-secret-value"}}
    redacted = redact_value(data)

    assert list(redacted) == ["nested"]
    assert list(redacted["nested"]) == ["values", "client_secret"]
    assert redacted["nested"]["values"] == (
        ["[REDACTED]"] * (len(values) - 1) + ["api_key=[REDACTED]"]
    )
    assert redacted["nested"]["client_secret"] == "[REDACTED]"

    unicode_value = "Résumé 東京 — task progress ✓"
    assert redact_value({"note": unicode_value}) == {"note": unicode_value}


def test_redact_value_preserves_non_secret_text():
    values = ["risk-assessment-notes-2026", "task-42", "regular API design notes"]
    assert redact_value(values) == values


def test_append_events_redacts_all_user_supplied_storage_fields(timeline):
    secret = stripe_live_key()
    ghp = github_token()
    value = f"{secret} {ghp}"
    conn = db.get_connection()
    actor = conn.execute(
        "SELECT api_keys.id, api_keys.key_hash, api_keys.user_id AS user_id, "
        "users.display_name FROM api_keys JOIN users ON users.id = api_keys.user_id "
        "WHERE api_keys.id = ?",
        (timeline["keys"]["alice"][1],),
    ).fetchone()
    conn.close()

    append_events(actor, [{
        "type": "task.started",
        "summary": value,
        "reason": value,
        "task": value,
        "repo": value,
        "tool": value,
        "engineer": value,
        "externalId": value,
        "payload": {"nested": {"value": value}, "items": [value]},
    }], "append-test")

    conn = db.get_connection()
    row = conn.execute(
        "SELECT * FROM events WHERE external_id = ?",
        ("[REDACTED] [REDACTED]",),
    ).fetchone()
    conn.close()
    assert row is not None
    for column in ("summary", "reason", "task_key", "repo", "tool", "actor_hint"):
        assert row[column] == "[REDACTED] [REDACTED]"
    assert json.loads(row["payload"]) == {
        "nested": {"value": "[REDACTED] [REDACTED]"},
        "items": ["[REDACTED] [REDACTED]"],
    }


@pytest.mark.xfail(strict=True, reason="U4")
def test_source_scan_rejects_direct_backup_writes():
    root = Path(__file__).parents[1]
    production = "\n".join(p.read_text(encoding="utf-8") for p in (root / "trac").rglob("*.py") if p.name != "db.py")
    assert not re.search(r"INSERT\s+INTO\s+activity_event(?:s|_stream)", production, re.I)


def test_activity_migration_is_idempotent(timeline):
    db.init_db()
    initial_count_conn = db.get_connection()
    initial_count = initial_count_conn.execute("SELECT COUNT(*) FROM events WHERE legacy_id IS NOT NULL").fetchone()[0]
    initial_count_conn.close()

    legacy_id = str(uuid.uuid4())
    conn = db.get_connection(); conn.execute("INSERT INTO activity_events (event_id, schema_version, type, ts, received_at, user_id, repo, source, data, project_id) VALUES (?, 1, 'legacy.type', ?, ?, ?, 'repo/source', 'cli', ?, ?)", (legacy_id, now_iso(), now_iso(), timeline["users"]["alice"], json.dumps({"summary": "migrated source"}), timeline["project"])); conn.commit(); conn.close()
    db.init_db()
    conn = db.get_connection(); row = conn.execute("SELECT * FROM events WHERE legacy_id = ?", (legacy_id,)).fetchone(); after_second_init = conn.execute("SELECT COUNT(*) FROM events WHERE legacy_id IS NOT NULL").fetchone()[0]; conn.close()
    assert row["type"] == "legacy.type" and row["actor_user_id"] == timeline["users"]["alice"] and row["repo"] == "repo/source" and row["summary"] == "migrated source" and row["timestamp"]
    assert after_second_init == initial_count + 1

    db.init_db()
    conn = db.get_connection(); after_third_init = conn.execute("SELECT COUNT(*) FROM events WHERE legacy_id IS NOT NULL").fetchone()[0]; conn.close()
    assert after_third_init == after_second_init
