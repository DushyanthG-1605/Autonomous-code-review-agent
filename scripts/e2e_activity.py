import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"
FAKE_KEY = "sk-live-FAKEKEY1234567890"


def _trac_executable():
    name = "trac.exe" if os.name == "nt" else "trac"
    candidate = Path(sys.executable).with_name(name)
    if candidate.is_file():
        return candidate
    located = shutil.which("trac")
    if located:
        return Path(located)
    raise RuntimeError("Install the collector in this Python environment first")


def _run(command, *, cwd, env, input_text=None, timeout=30):
    result = subprocess.run(
        [str(part) for part in command],
        cwd=cwd,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(
            f"{Path(str(command[0])).name} failed with exit code {result.returncode}"
        )
    return result


def _run_trac(trac, *args, cwd, env, input_text=None):
    return _run([trac, *args], cwd=cwd, env=env, input_text=input_text)


def _run_git(git, *args, cwd, env):
    return _run([git, *args], cwd=cwd, env=env)


def _read_jsonl(path):
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _activity_rows(database, user_id):
    connection = sqlite3.connect(database)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT event_id, type, repo, branch, data FROM activity_events "
            "WHERE user_id = ? ORDER BY received_at, event_id",
            (user_id,),
        ).fetchall()
    finally:
        connection.close()
    return [dict(row) for row in rows]


def _reserve_port():
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


def _log_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-3000:]
    except OSError:
        return ""


def _start_backend(env, port, log_path):
    log_file = log_path.open("ab")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "trac.main:app",
            "--host",
            HOST,
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    return process, log_file


def _stop_backend(process, log_file):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    if log_file is not None:
        log_file.close()


def _wait_for_backend(process, base_url, log_path):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"TRAC backend exited during startup:\n{_log_text(log_path)}")
        try:
            with urlopen(f"{base_url}/", timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, URLError, HTTPError):
            time.sleep(0.1)
    raise RuntimeError(f"TRAC backend did not become ready:\n{_log_text(log_path)}")


def _request_json(url, payload=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=body, headers=headers)
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _assert_no_private_content(database, state_dir, values):
    paths = [database, Path(f"{database}-wal"), Path(f"{database}-shm")]
    paths.extend(path for path in state_dir.rglob("*") if path.is_file())
    for path in paths:
        if not path.exists():
            continue
        content = path.read_bytes()
        for value in values:
            if value.encode("utf-8") in content:
                raise AssertionError("Private file, prompt, or command text was persisted")


def run_e2e():
    git = shutil.which("git")
    if not git:
        raise RuntimeError("Git is required to run this end-to-end flow")
    trac = _trac_executable()
    passed_event_count = 0
    duplicate_count = 0

    with tempfile.TemporaryDirectory(prefix="trac-activity-e2e-") as temporary:
        root = Path(temporary)
        database = root / "trac-e2e.db"
        home = root / "home"
        repo = root / "repo"
        helper = root / "python-helper"
        state_dir = home / ".trac"
        helper.mkdir()
        home.mkdir()
        repo.mkdir()

        # Isolate the existing module-level DB_PATH in child processes only.
        (helper / "sitecustomize.py").write_text(
            "import os\n"
            "from pathlib import Path\n"
            "from trac import db\n"
            "db.DB_PATH = Path(os.environ['TRAC_E2E_DB_PATH'])\n",
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["HOME"] = str(home)
        if os.name == "nt":
            env["USERPROFILE"] = str(home)
            env["HOMEDRIVE"] = home.drive
            env["HOMEPATH"] = str(home)[len(home.drive):]
        env["TRAC_E2E_DB_PATH"] = str(database)
        python_path = [str(helper), str(ROOT)]
        if env.get("PYTHONPATH"):
            python_path.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(python_path)
        env["PATH"] = os.pathsep.join([str(trac.parent), env.get("PATH", "")])
        env["GIT_TERMINAL_PROMPT"] = "0"

        port = _reserve_port()
        base_url = f"http://{HOST}:{port}"
        backend_log = root / "backend.log"
        process, log_file = _start_backend(env, port, backend_log)

        try:
            _wait_for_backend(process, base_url, backend_log)

            login = f"trac-e2e-{os.getpid()}"
            manage_result = _run(
                [
                    sys.executable,
                    "-m",
                    "trac.manage",
                    "create-user",
                    login,
                ],
                cwd=ROOT,
                env=env,
            )
            token = manage_result.stdout.strip()
            if not token or "\n" in token or "\r" in token:
                raise AssertionError("trac.manage did not return exactly one device token")

            _run_trac(trac, "login", "--url", base_url, "--token", token, cwd=repo, env=env)
            _run_git(git, "init", "--quiet", cwd=repo, env=env)
            _run_git(git, "config", "user.name", "TRAC E2E", cwd=repo, env=env)
            _run_git(
                git,
                "config",
                "user.email",
                "trac-e2e@example.invalid",
                cwd=repo,
                env=env,
            )
            _run_git(
                git,
                "remote",
                "add",
                "origin",
                "git@github.com:trac-e2e/activity-demo.git",
                cwd=repo,
                env=env,
            )
            _run_trac(trac, "init", cwd=repo, env=env)

            file_content = f"temporary fixture: {FAKE_KEY}\n"
            secret_file = repo / "secret.txt"
            secret_file.write_text(file_content, encoding="utf-8")
            _run_git(git, "add", "secret.txt", cwd=repo, env=env)
            commit_message = "E2E_PRIVATE_COMMIT_MESSAGE_SENTINEL"
            _run_git(git, "commit", "-m", commit_message, cwd=repo, env=env)
            _run_git(git, "checkout", "-b", "e2e-validation-branch", cwd=repo, env=env)

            _run_trac(
                trac,
                "emit",
                "task.started",
                "--summary",
                "Exercise the activity tracker",
                cwd=repo,
                env=env,
            )
            _run_trac(
                trac,
                "emit",
                "task.completed",
                "--summary",
                "Complete the activity tracker flow",
                "--outcome",
                "done",
                cwd=repo,
                env=env,
            )
            _run_trac(
                trac,
                "emit",
                "task.blocked",
                "--summary",
                "Record a safe blocked-task example",
                "--reason",
                "validation",
                cwd=repo,
                env=env,
            )

            prompt_text = f"Please inspect this private value: {FAKE_KEY} PROMPT_E2E_SENTINEL"
            command_text = f"pytest --token {FAKE_KEY} COMMAND_E2E_SENTINEL"
            hook_payloads = [
                {
                    "hook_event_name": "PostToolUse",
                    "tool_name": "Write",
                    "tool_input": {
                        "file_path": str(secret_file),
                        "content": file_content,
                    },
                },
                {
                    "hook_event_name": "UserPromptSubmit",
                    "prompt": prompt_text,
                },
                {
                    "hook_event_name": "PostToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": command_text},
                    "tool_response": {"exit_code": 1},
                },
            ]
            for payload in hook_payloads:
                payload.update(
                    {
                        "session_id": "trac-activity-e2e-claude",
                        "cwd": str(repo),
                    }
                )
                _run_trac(
                    trac,
                    "hook",
                    "claude",
                    cwd=repo,
                    env=env,
                    input_text=json.dumps(payload),
                )
            _run_trac(trac, "flush", cwd=repo, env=env)

            status = _run_trac(trac, "status", cwd=repo, env=env).stdout.lower()
            if "tracking: on" not in status or "login: logged in" not in status:
                raise AssertionError("Collector did not report active tracking and login")

            rows_before_pause = _activity_rows(database, _user_id(database, login))
            queue_path = state_dir / "queue.jsonl"
            queue_before_pause = _read_jsonl(queue_path)
            _run_trac(trac, "pause", cwd=repo, env=env)
            _run_trac(
                trac,
                "emit",
                "task.started",
                "--summary",
                "This paused event must not be collected",
                cwd=repo,
                env=env,
            )
            paused_prompt = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "trac-activity-e2e-claude",
                "cwd": str(repo),
                "prompt": "PAUSED_PROMPT_E2E_SENTINEL",
            }
            _run_trac(
                trac,
                "hook",
                "claude",
                cwd=repo,
                env=env,
                input_text=json.dumps(paused_prompt),
            )
            if len(_activity_rows(database, _user_id(database, login))) != len(rows_before_pause):
                raise AssertionError("Paused activity reached the server")
            if _read_jsonl(queue_path) != queue_before_pause:
                raise AssertionError("Paused activity changed the local queue")
            _run_trac(trac, "resume", cwd=repo, env=env)

            _stop_backend(process, log_file)
            process = None
            log_file = None
            offline_summary = "Queue an event while the server is offline"
            _run_trac(
                trac,
                "emit",
                "task.completed",
                "--summary",
                offline_summary,
                "--outcome",
                "partial",
                cwd=repo,
                env=env,
            )
            _run_trac(trac, "flush", cwd=repo, env=env)
            offline_queue = _read_jsonl(queue_path)
            if not any(
                event.get("data", {}).get("summary") == offline_summary
                for event in offline_queue
            ):
                raise AssertionError("Offline activity was not retained in the queue")

            process, log_file = _start_backend(env, port, backend_log)
            _wait_for_backend(process, base_url, backend_log)
            _run_trac(trac, "flush", cwd=repo, env=env)
            if _read_jsonl(queue_path):
                raise AssertionError("The queue was not cleared after the server restarted")

            user_id = _user_id(database, login)
            rows = _activity_rows(database, user_id)
            sent_records = _read_jsonl(state_dir / "sent.jsonl")
            sent_events = [record["event"] for record in sent_records]
            sent_ids = [event["event_id"] for event in sent_events]
            row_ids = [row["event_id"] for row in rows]
            if not sent_ids or len(sent_ids) != len(set(sent_ids)):
                raise AssertionError("The collector sent no events or repeated an event ID")
            if len(row_ids) != len(set(row_ids)) or set(row_ids) != set(sent_ids):
                raise AssertionError("Server events did not match unique collector sends")

            event_counts = Counter(row["type"] for row in rows)
            required_counts = {
                "git.commit": 1,
                "git.branch": 1,
                "task.started": 1,
                "task.completed": 2,
                "task.blocked": 1,
                "file.edited": 1,
                "agent.prompt_submitted": 1,
                "command.run": 1,
            }
            if any(event_counts[event_type] < count for event_type, count in required_counts.items()):
                raise AssertionError("One or more required Git, task, or Claude events are missing")
            branch_rows = [row for row in rows if row["type"] == "git.branch"]
            if branch_rows[0]["branch"] != "e2e-validation-branch":
                raise AssertionError("The branch checkout event has the wrong branch")

            event_count_before_replay = len(rows)
            status_code, duplicate_result = _request_json(
                f"{base_url}/v1/events",
                {"events": sent_events},
                token,
            )
            if (
                status_code != 202
                or duplicate_result.get("accepted") != 0
                or duplicate_result.get("duplicates") != len(sent_events)
                or duplicate_result.get("rejected")
            ):
                raise AssertionError("Resending sent events was not duplicate-safe")
            if len(_activity_rows(database, user_id)) != event_count_before_replay:
                raise AssertionError("Resending events created duplicate database rows")

            _assert_no_private_content(
                database,
                state_dir,
                [
                    FAKE_KEY,
                    file_content,
                    prompt_text,
                    command_text,
                    commit_message,
                    "PAUSED_PROMPT_E2E_SENTINEL",
                ],
            )

            passed_event_count = len(rows)
            duplicate_count = len(sent_events)
        finally:
            _stop_backend(process, log_file)

    print("PASS: isolated backend and device user initialized")
    print("PASS: Git hooks captured commit and branch checkout metadata")
    print("PASS: task and Claude hook events were accepted")
    print("PASS: pause suppressed CLI and Claude hook collection")
    print("PASS: offline event stayed queued and flushed after restart")
    print(f"PASS: {passed_event_count} unique events arrived exactly once")
    print(f"PASS: replay was deduplicated ({duplicate_count} duplicates)")
    print("PASS: fake key, file content, prompt, command, and commit message were not persisted")

def _user_id(database, login):
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT id FROM users WHERE github_login = ?", (login,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise AssertionError("The E2E user was not created")
    return row[0]


def main():
    try:
        run_e2e()
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
