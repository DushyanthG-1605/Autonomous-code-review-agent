import hashlib
import hmac
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from trac import db
from trac.main import app
from trac.manage import map_github_repository
from trac.models import now_iso

WEBHOOK_SECRET = "test-github-webhook-secret"
REPOSITORY = "example/payments"


@pytest.fixture
def github_api(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "github.db")
    monkeypatch.setenv("TRAC_GITHUB_WEBHOOK_SECRET", WEBHOOK_SECRET)
    with TestClient(app) as client:
        yield client


def create_project(slug="payments"):
    conn = db.get_connection()
    user_id = conn.execute(
        """
        INSERT INTO users (github_login, display_name, created_at, public_id)
        VALUES (?, ?, ?, ?)
        """,
        (f"{slug}-owner", f"{slug.title()} Owner", now_iso(), str(uuid.uuid4())),
    ).lastrowid
    project_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO projects
            (id, slug, name, project_key, project_head_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (project_id, slug, slug.title(), slug, user_id, now_iso()),
    )
    conn.execute(
        """
        INSERT INTO project_memberships (project_id, user_id, role, created_at)
        VALUES (?, ?, 'owner', ?)
        """,
        (project_id, user_id, now_iso()),
    )
    token = f"{slug}-token"
    conn.execute(
        """
        INSERT INTO devices
            (user_id, token_hash, label, created_at, project_id, scopes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            hashlib.sha256(token.encode()).hexdigest(),
            "project head",
            now_iso(),
            project_id,
            '["events:read","events:ingest"]',
        ),
    )
    conn.commit()
    conn.close()
    return project_id, token


def create_task(project_id, external_key="TRAC-101"):
    conn = db.get_connection()
    task_id = str(uuid.uuid4())
    timestamp = now_iso()
    conn.execute(
        """
        INSERT INTO tasks
            (id, project_id, title, external_key, status, priority,
             progress_percent, progress_is_estimate, created_at, updated_at)
        VALUES (?, ?, 'Example task', ?, 'todo', 'medium', 0, 0, ?, ?)
        """,
        (task_id, project_id, external_key, timestamp, timestamp),
    )
    conn.commit()
    conn.close()
    return task_id


def signed_headers(event_name, payload, delivery_id=None):
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, {
        "X-GitHub-Event": event_name,
        "X-GitHub-Delivery": delivery_id or str(uuid.uuid4()),
        "X-Hub-Signature-256": f"sha256={signature}",
    }


def send_webhook(client, event_name, payload, delivery_id=None):
    body, headers = signed_headers(event_name, payload, delivery_id)
    return client.post(
        "/v1/integrations/github/webhook",
        content=body,
        headers=headers,
    )


def test_push_signature_persists_project_event_and_matches_task(github_api):
    project_id, token = create_project()
    task_id = create_task(project_id)
    map_github_repository("payments", REPOSITORY)
    payload = {
        "repository": {"full_name": REPOSITORY},
        "ref": "refs/heads/feature/TRAC-101",
        "pushed_at": "2026-11-14T07:00:00Z",
        "head_commit": {
            "id": "a" * 40,
            "timestamp": "2026-11-14T06:59:00Z",
            "message": "implementation details must not be stored",
        },
    }

    response = send_webhook(github_api, "push", payload)

    assert response.status_code == 202
    assert response.json() == {"accepted": 1, "duplicate": False}
    activity = github_api.get(
        f"/v1/projects/{project_id}/activity",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert len(activity) == 1
    assert activity[0]["event_type"] == "git.commit"
    assert activity[0]["source"] == "github"
    assert activity[0]["task_id"] == task_id
    assert activity[0]["metadata"] == {
        "commit_sha": "a" * 40,
        "repository": REPOSITORY,
        "branch": "feature/TRAC-101",
        "task_reference": "TRAC-101",
    }
    assert "message" not in activity[0]["metadata"]


def test_invalid_and_missing_signatures_are_rejected_before_body_parsing(github_api):
    invalid = github_api.post(
        "/v1/integrations/github/webhook",
        content=b"not json",
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": "sha256=" + "0" * 64,
        },
    )
    missing = github_api.post(
        "/v1/integrations/github/webhook",
        content=b"not json",
        headers={
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": str(uuid.uuid4()),
        },
    )

    assert invalid.status_code == 401
    assert missing.status_code == 401
    assert github_api.get("/v1/projects").status_code == 401


def test_webhook_requires_server_secret(github_api, monkeypatch):
    monkeypatch.delenv("TRAC_GITHUB_WEBHOOK_SECRET")
    response = github_api.post("/v1/integrations/github/webhook", content=b"{}")
    assert response.status_code == 503


def test_project_head_can_create_unique_external_task_keys(github_api):
    project_id, token = create_project()
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"title": "Task with external reference", "external_key": "TRAC-201"}

    created = github_api.post(
        f"/v1/projects/{project_id}/tasks", headers=headers, json=payload
    )
    duplicate = github_api.post(
        f"/v1/projects/{project_id}/tasks", headers=headers, json=payload
    )

    assert created.status_code == 201
    assert created.json()["external_key"] == "TRAC-201"
    assert duplicate.status_code == 409


def test_webhook_delivery_id_is_idempotent(github_api):
    project_id, token = create_project()
    map_github_repository("payments", REPOSITORY)
    delivery_id = str(uuid.uuid4())
    payload = {
        "repository": {"full_name": REPOSITORY},
        "ref": "refs/heads/main",
        "head_commit": {"id": "b" * 40},
    }

    first = send_webhook(github_api, "push", payload, delivery_id)
    second = send_webhook(github_api, "push", payload, delivery_id)
    activity = github_api.get(
        f"/v1/projects/{project_id}/activity",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    conn = db.get_connection()
    delivery_count = conn.execute(
        "SELECT COUNT(*) FROM github_webhook_deliveries"
    ).fetchone()[0]
    conn.close()

    assert first.json() == {"accepted": 1, "duplicate": False}
    assert second.json() == {"accepted": 0, "duplicate": True}
    assert len(activity) == 1
    assert delivery_count == 1


def test_unmapped_repository_is_not_persisted(github_api):
    create_project()
    response = send_webhook(
        github_api,
        "push",
        {
            "repository": {"full_name": "unauthorized/private"},
            "ref": "refs/heads/main",
            "head_commit": {"id": "c" * 40},
        },
    )
    conn = db.get_connection()
    deliveries = conn.execute(
        "SELECT COUNT(*) FROM github_webhook_deliveries"
    ).fetchone()[0]
    conn.close()

    assert response.status_code == 404
    assert deliveries == 0


@pytest.mark.parametrize(
    ("action", "event_type", "merged"),
    [
        ("opened", "git.pull_request.opened", False),
        ("synchronize", "git.pull_request.updated", False),
        ("closed", "git.pull_request.merged", True),
    ],
)
def test_pull_request_events_match_explicit_task_without_completing_it(
    github_api, action, event_type, merged
):
    project_id, token = create_project()
    task_id = create_task(project_id)
    map_github_repository("payments", REPOSITORY)
    payload = {
        "action": action,
        "repository": {"full_name": REPOSITORY},
        "pull_request": {
            "number": 17,
            "title": "Work for TRAC-101",
            "merged": merged,
            "created_at": "2026-11-14T06:00:00Z",
            "updated_at": "2026-11-14T07:00:00Z",
            "base": {"ref": "main"},
            "head": {"ref": "feature/TRAC-101"},
        },
    }

    response = send_webhook(github_api, "pull_request", payload)

    assert response.status_code == 202
    activity = github_api.get(
        f"/v1/projects/{project_id}/activity",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    task = github_api.get(
        f"/v1/tasks/{task_id}",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert activity[0]["event_type"] == event_type
    assert activity[0]["task_id"] == task_id
    assert task["status"] == "todo"
    assert task["updates"][0]["update_type"] == event_type


@pytest.mark.parametrize(
    ("event_name", "body_key", "conclusion", "source", "expected_result"),
    [
        ("workflow_run", "workflow_run", "failure", "ci", "failed"),
        ("check_run", "check_run", "success", "ci", "passed"),
    ],
)
def test_workflow_and_check_results_are_normalized_without_raw_payload_data(
    github_api, event_name, body_key, conclusion, source, expected_result
):
    project_id, token = create_project()
    map_github_repository("payments", REPOSITORY)
    if body_key == "workflow_run":
        detail = {
            "id": 543,
            "workflow_id": 12,
            "name": "Build and test",
            "head_branch": "main",
            "conclusion": conclusion,
            "run_started_at": "2026-11-14T06:00:00Z",
            "updated_at": "2026-11-14T06:02:00Z",
            "logs_url": "must not be persisted",
        }
        payload = {"action": "completed", "repository": {"full_name": REPOSITORY}}
    else:
        detail = {
            "id": 654,
            "name": "Unit tests",
            "head_branch": "main",
            "conclusion": conclusion,
            "started_at": "2026-11-14T06:00:00Z",
            "completed_at": "2026-11-14T06:01:00Z",
            "output": {"text": "raw logs must not be persisted"},
        }
        payload = {
            "action": "completed",
            "repository": {"full_name": REPOSITORY},
        }
        detail["check_suite"] = {"head_branch": "main"}
    payload[body_key] = detail

    response = send_webhook(github_api, event_name, payload)

    assert response.status_code == 202
    activity = github_api.get(
        f"/v1/projects/{project_id}/activity",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    assert len(activity) == 1
    assert activity[0]["event_type"] == "build.completed"
    assert activity[0]["source"] == source
    assert activity[0]["metadata"]["result"] == expected_result
    assert activity[0]["metadata"]["duration_ms"] in {60_000, 120_000}
    assert "logs_url" not in activity[0]["metadata"]
    assert "output" not in activity[0]["metadata"]


def test_project_mapping_ignores_payload_project_id_and_isolates_other_project(
    github_api,
):
    project_id, token = create_project()
    other_project, other_token = create_project("private")
    map_github_repository("payments", REPOSITORY)
    payload = {
        "project_id": other_project,
        "repository": {"full_name": REPOSITORY},
        "ref": "refs/heads/main",
        "head_commit": {"id": "d" * 40},
    }

    response = send_webhook(github_api, "push", payload)

    assert response.status_code == 202
    project_activity = github_api.get(
        f"/v1/projects/{project_id}/activity",
        headers={"Authorization": f"Bearer {token}"},
    ).json()
    other_activity = github_api.get(
        f"/v1/projects/{other_project}/activity",
        headers={"Authorization": f"Bearer {other_token}"},
    ).json()
    assert len(project_activity) == 1
    assert project_activity[0]["project_id"] == project_id
    assert other_activity == []


def test_unmatched_and_ambiguous_task_references_remain_project_activity(github_api):
    project_id, token = create_project()
    create_task(project_id)
    map_github_repository("payments", REPOSITORY)
    payload = {
        "repository": {"full_name": REPOSITORY},
        "ref": "refs/heads/TRAC-101-and-TRAC-999",
        "head_commit": {"id": "e" * 40},
    }

    response = send_webhook(github_api, "push", payload)
    activity = github_api.get(
        f"/v1/projects/{project_id}/activity",
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    assert response.status_code == 202
    assert len(activity) == 1
    assert activity[0]["task_id"] is None
    assert "task_reference" not in activity[0]["metadata"]
