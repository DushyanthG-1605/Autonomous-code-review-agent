import io
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trac_collector import cli


class Response:
    def __init__(self, status_code, body, headers=None, text=""):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self._body


def setup_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "STATE_DIR", tmp_path / ".trac")

    def fake_git(*args, **kwargs):
        return {
            ("rev-parse", "--show-toplevel"): str(tmp_path),
            ("remote", "get-url", "origin"): "git@github.com:org/repo.git",
            ("rev-parse", "--abbrev-ref", "HEAD"): "main",
        }.get(args)

    monkeypatch.setattr(cli, "_git_output", fake_git)


def queued_events():
    return cli._read_jsonl(cli.STATE_DIR / "queue.jsonl")


def test_offline_queue_then_flush(tmp_path, monkeypatch):
    setup_cli(tmp_path, monkeypatch)
    cli.emit_event("task.started", summary="Build", data=[])
    assert len(queued_events()) == 2

    cli._save_login("https://trac.example", "token")
    monkeypatch.setattr(
        cli.requests,
        "post",
        lambda *args, **kwargs: Response(
            202, {"accepted": 2, "duplicates": 0, "rejected": []}
        ),
    )

    result = cli.flush_queue(force=True)

    assert result["sent"] == 2
    assert queued_events() == []
    sent = cli._read_jsonl(cli.STATE_DIR / "sent.jsonl")
    assert len(sent) == 2


def test_duplicate_safe_retry_after_lost_response(tmp_path, monkeypatch):
    setup_cli(tmp_path, monkeypatch)
    cli.emit_event("git.push", data=["commits=1"])
    cli._save_login("https://trac.example", "token")
    calls = []

    def post(*args, **kwargs):
        calls.append(kwargs["json"]["events"])
        if len(calls) == 1:
            raise requests.ConnectionError("response lost after server commit")
        return Response(202, {"accepted": 0, "duplicates": 2, "rejected": []})

    monkeypatch.setattr(cli.requests, "post", post)
    cli.flush_queue(force=True)
    assert len(queued_events()) == 2

    result = cli.flush_queue(force=True)

    assert result["sent"] == 2
    assert queued_events() == []
    assert len(calls) == 2


def test_pause_prevents_collection(tmp_path, monkeypatch):
    setup_cli(tmp_path, monkeypatch)
    cli._ensure_state_dir()
    (cli.STATE_DIR / "paused").touch()

    cli.emit_event("task.started", summary="Do not collect", data=[])

    assert queued_events() == []


def test_summary_redacts_fake_key(tmp_path, monkeypatch):
    setup_cli(tmp_path, monkeypatch)

    cli.emit_event(
        "task.completed",
        summary='Used api_key="fakeSecretValue123"',
        data=[],
    )

    event = queued_events()[-1]
    assert "fakeSecretValue123" not in json.dumps(event)
    assert "[REDACTED]" in event["data"]["summary"]


def test_idle_session_ends_and_starts_a_new_session(tmp_path, monkeypatch):
    setup_cli(tmp_path, monkeypatch)
    now = [1_000.0]
    monkeypatch.setattr(cli.time, "time", lambda: now[0])

    cli.emit_event("task.started", data=[])
    first_session_id = queued_events()[0]["session_id"]
    now[0] += cli.SESSION_IDLE_SECONDS + 1
    cli.emit_event("task.completed", data=[])

    events = queued_events()
    assert [event["type"] for event in events] == [
        "session.start",
        "task.started",
        "session.end",
        "session.start",
        "task.completed",
    ]
    assert events[2]["session_id"] == first_session_id
    assert events[2]["data"]["duration_s"] == cli.SESSION_IDLE_SECONDS + 1
    assert events[3]["session_id"] != first_session_id


def test_file_paths_are_queued_repo_relative(tmp_path, monkeypatch):
    setup_cli(tmp_path, monkeypatch)
    path = tmp_path / "src" / "module.py"

    cli.emit_event("file.edited", data=[f"path={path}"])

    assert queued_events()[-1]["data"]["path"] == "src/module.py"


def test_init_and_uninit_respect_hooks_path_and_preserve_user_content(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.hooksPath", "custom-hooks"],
        check=True,
    )
    hooks_dir = repo / "custom-hooks"
    hooks_dir.mkdir()
    post_commit = hooks_dir / "post-commit"
    original = b"#!/bin/sh\nprintf 'my hook\\n'\n"
    post_commit.write_bytes(original)
    pre_push = hooks_dir / "pre-push"
    original_without_newline = b"#!/bin/sh\nprintf 'my push hook'"
    pre_push.write_bytes(original_without_newline)
    post_checkout = hooks_dir / "post-checkout"
    original_empty_hook = b"#!/bin/sh\n"
    post_checkout.write_bytes(original_empty_hook)
    monkeypatch.chdir(repo)

    assert cli.init_hooks() == hooks_dir.resolve()
    installed = post_commit.read_bytes()
    assert installed.startswith(original)
    assert installed.count(b"# >>> trac >>>") == 1
    cli.init_hooks()
    assert post_commit.read_bytes().count(b"# >>> trac >>>") == 1

    cli.uninit_hooks()

    restored = post_commit.read_bytes()
    assert restored == original
    assert pre_push.read_bytes() == original_without_newline
    assert post_checkout.read_bytes() == original_empty_hook


def test_uninit_deletes_hooks_created_by_trac(tmp_path, monkeypatch):
    repo = tmp_path / "empty-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo)

    cli.init_hooks()
    cli.uninit_hooks()

    for name in cli.HOOK_NAMES:
        assert not (cli._current_hooks_dir() / name).exists()


def test_hooks_capture_commit_checkout_and_push_without_message_text(tmp_path):
    git = shutil.which("git")
    trac_path = Path(sys.executable).parent / ("trac.exe" if os.name == "nt" else "trac")
    if not git or not trac_path.is_file():
        pytest.skip("Git and the installed trac entry point are required")

    repo = tmp_path / "repo"
    bare = tmp_path / "remote.git"
    repo.mkdir()
    subprocess.run([git, "init", "-q", "--initial-branch=main", str(repo)], check=True)
    subprocess.run([git, "init", "-q", "--bare", str(bare)], check=True)

    def run_git(*args, env=None):
        subprocess.run(
            [git, "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

    run_git("config", "user.name", "TRAC Test")
    run_git("config", "user.email", "trac-test@example.invalid")
    (repo / "README.txt").write_text("baseline\n", encoding="utf-8")
    run_git("add", "README.txt")
    run_git("commit", "-m", "baseline")
    run_git("remote", "add", "origin", str(bare))

    home = tmp_path / "home"
    state_dir = home / ".trac"
    state_dir.mkdir(parents=True)
    session_id = "test-session"
    now = time.time()
    (state_dir / "session.json").write_text(
        json.dumps(
            {"session_id": session_id, "started_at": now, "last_activity": now}
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["TRAC_SESSION_ID"] = session_id
    env["PATH"] = str(trac_path.parent) + os.pathsep + env.get("PATH", "")

    subprocess.run([str(trac_path), "init"], cwd=repo, env=env, check=True, capture_output=True)
    (repo / "change.txt").write_text("added content sentinel\n", encoding="utf-8")
    run_git("add", "change.txt")
    subprocess.run(
        [git, "-C", str(repo), "commit", "-m", "PRIVATE_COMMIT_MESSAGE_SENTINEL"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    (repo / "change.txt").write_text("temporary checkout content\n", encoding="utf-8")
    run_git("checkout", "--", "change.txt", env=env)
    run_git("switch", "-c", "feature", env=env)
    subprocess.run(
        [git, "-C", str(repo), "push", "-u", "origin", "feature"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    queue = [
        json.loads(line)
        for line in (state_dir / "queue.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert len(queue) == 3
    assert [event["type"] for event in queue] == [
        "git.commit",
        "git.branch",
        "git.push",
    ]
    assert queue[0]["data"]["files_changed"] == 1
    assert queue[0]["data"]["sha"]
    assert queue[0]["data"]["lines_added"] == 1
    assert queue[0]["data"]["lines_removed"] == 0
    assert queue[1]["data"] == {"action": "created", "name": "feature"}
    assert queue[2]["data"] == {"commits": 2, "remote_branch": "feature"}
    serialized = json.dumps(queue)
    assert "PRIVATE_COMMIT_MESSAGE_SENTINEL" not in serialized
    assert "added content sentinel" not in serialized


def test_emit_returns_zero_on_server_failure(tmp_path, monkeypatch, capsys):
    setup_cli(tmp_path, monkeypatch)
    cli._save_login("https://trac.example", "token")

    def fail(*args, **kwargs):
        raise requests.ConnectionError("server unavailable")

    monkeypatch.setattr(cli.requests, "post", fail)

    assert cli.main(["emit", "task.started", "--summary", "Still safe"]) == 0
    assert capsys.readouterr().err == ""
    assert len(queued_events()) == 2


def test_claude_hooks_queue_allowlisted_metadata_only(tmp_path, monkeypatch):
    setup_cli(tmp_path, monkeypatch)

    def run_hook(payload):
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(payload)))
        assert cli.main(["hook", "claude"]) == 0

    run_hook({
        "session_id": "claude-session",
        "cwd": str(tmp_path),
        "hook_event_name": "SessionStart",
        "source": "startup",
    })
    run_hook({
        "session_id": "claude-session",
        "cwd": str(tmp_path),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "PRIVATE_PROMPT_SENTINEL",
    })
    run_hook({
        "session_id": "claude-session",
        "cwd": str(tmp_path),
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(tmp_path / "src" / "module.py"),
            "old_string": "PRIVATE_OLD_CODE_SENTINEL\nsecond line",
            "new_string": "PRIVATE_NEW_CODE_SENTINEL\nsecond\nthird",
        },
        "tool_response": {"filePath": str(tmp_path / "src" / "module.py")},
    })
    run_hook({
        "session_id": "claude-session",
        "cwd": str(tmp_path),
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": "pytest -q PRIVATE_COMMAND_SENTINEL"},
        "error": "Exit code 7\nPRIVATE_COMMAND_OUTPUT_SENTINEL",
    })
    run_hook({
        "session_id": "claude-session",
        "cwd": str(tmp_path),
        "hook_event_name": "SessionEnd",
        "reason": "other",
    })

    events = queued_events()
    assert [event["type"] for event in events] == [
        "session.start",
        "agent.prompt_submitted",
        "file.edited",
        "command.run",
        "session.end",
    ]
    assert all(event["session_id"] == "claude-session" for event in events)
    edited = events[2]["data"]
    assert edited == {
        "path": "src/module.py",
        "language": "python",
        "lines_added": 3,
        "lines_removed": 2,
    }
    assert events[1]["data"] == {"count": 1}
    assert events[3]["data"] == {"kind": "test", "exit_code": 7}
    serialized = json.dumps(events)
    for sentinel in (
        "PRIVATE_PROMPT_SENTINEL",
        "PRIVATE_OLD_CODE_SENTINEL",
        "PRIVATE_NEW_CODE_SENTINEL",
        "PRIVATE_COMMAND_SENTINEL",
        "PRIVATE_COMMAND_OUTPUT_SENTINEL",
    ):
        assert sentinel not in serialized


def test_claude_write_hook_counts_new_file_lines(tmp_path, monkeypatch):
    setup_cli(tmp_path, monkeypatch)
    payload = {
        "session_id": "claude-session",
        "cwd": str(tmp_path),
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(tmp_path / "README.md"),
            "content": "first\nsecond\n",
        },
        "tool_response": {"type": "create"},
    }
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(payload)))

    assert cli.main(["hook", "claude"]) == 0

    event = queued_events()[-1]
    assert event["type"] == "file.edited"
    assert event["data"] == {
        "path": "README.md",
        "language": "markdown",
        "lines_added": 2,
        "lines_removed": 0,
    }


def test_init_claude_merges_settings_and_is_idempotent(tmp_path, monkeypatch):
    setup_cli(tmp_path, monkeypatch)
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    original = {
        "enabledPlugins": {"example": True},
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "custom-hook"}]}],
            "PostToolUse": [
                {
                    "matcher": "Write",
                    "hooks": [{"type": "command", "command": "existing-hook"}],
                }
            ],
        },
    }
    settings_path.write_text(json.dumps(original), encoding="utf-8")

    assert cli.init_claude_hooks() == settings_path
    first = json.loads(settings_path.read_text(encoding="utf-8"))
    assert cli.init_claude_hooks() == settings_path
    merged = json.loads(settings_path.read_text(encoding="utf-8"))

    assert first == merged
    assert merged["enabledPlugins"] == original["enabledPlugins"]
    assert merged["hooks"]["Stop"] == original["hooks"]["Stop"]
    assert merged["hooks"]["PostToolUse"][0] == original["hooks"]["PostToolUse"][0]
    assert len(merged["hooks"]["PostToolUse"]) == 2
    assert len(merged["hooks"]["SessionStart"]) == 1
    assert len(merged["hooks"]["SessionEnd"]) == 1
    assert len(merged["hooks"]["UserPromptSubmit"]) == 1
    assert len(merged["hooks"]["PostToolUseFailure"]) == 1


def test_init_claude_option_installs_both_hook_sets(tmp_path, monkeypatch, capsys):
    git_hooks = tmp_path / "git-hooks"
    settings_path = tmp_path / ".claude" / "settings.json"
    monkeypatch.setattr(cli, "init_hooks", lambda: git_hooks)
    monkeypatch.setattr(cli, "init_claude_hooks", lambda: settings_path)

    assert cli.main(["init", "--claude"]) == 0

    output = capsys.readouterr().out
    assert str(git_hooks) in output
    assert str(settings_path) in output


def test_claude_hook_errors_are_silent_and_exit_zero(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("not json"))

    assert cli.main(["hook", "claude"]) == 0
    assert capsys.readouterr().out == ""
    assert capsys.readouterr().err == ""


def test_claude_hook_cli_reads_piped_json_without_storing_private_text(tmp_path):
    trac_path = Path(sys.executable).parent / (
        "trac.exe" if os.name == "nt" else "trac"
    )
    if not trac_path.is_file():
        pytest.skip("The installed trac entry point is required")

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    home = tmp_path / "home"
    state_dir = home / ".trac"
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["PATH"] = str(trac_path.parent) + os.pathsep + env.get("PATH", "")
    payloads = [
        {
            "session_id": "piped-session",
            "cwd": str(repo),
            "hook_event_name": "UserPromptSubmit",
            "prompt": "PRIVATE_PIPED_PROMPT_SENTINEL",
        },
        {
            "session_id": "piped-session",
            "cwd": str(repo),
            "hook_event_name": "PostToolUse",
            "tool_name": "Edit",
            "tool_input": {
                "file_path": str(repo / "src" / "module.py"),
                "old_string": "PRIVATE_PIPED_OLD_CODE_SENTINEL",
                "new_string": "PRIVATE_PIPED_NEW_CODE_SENTINEL",
            },
            "tool_response": {"filePath": str(repo / "src" / "module.py")},
        },
        {
            "session_id": "piped-session",
            "cwd": str(repo),
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest PRIVATE_PIPED_COMMAND_SENTINEL"},
            "tool_response": {"exit_code": 0},
        },
    ]
    results = [
        subprocess.run(
            [str(trac_path), "hook", "claude"],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=repo,
            env=env,
            check=True,
        )
        for payload in payloads
    ]

    queue_text = (state_dir / "queue.jsonl").read_text(encoding="utf-8")
    assert all(result.stdout == "" and result.stderr == "" for result in results)
    for sentinel in (
        "PRIVATE_PIPED_PROMPT_SENTINEL",
        "PRIVATE_PIPED_OLD_CODE_SENTINEL",
        "PRIVATE_PIPED_NEW_CODE_SENTINEL",
        "PRIVATE_PIPED_COMMAND_SENTINEL",
    ):
        assert sentinel not in queue_text
    assert '"type":"agent.prompt_submitted"' in queue_text
    assert '"type":"file.edited"' in queue_text
    assert '"type":"command.run"' in queue_text
