# Autonomous-code-review-agent
LangGraph-based agent that autonomously reviews GitHub PRs for bugs, security issues, code quality, and maintainability using repo-aware RAG

## TRAC versioned activity ingestion

The TRAC backend creates its activity tables on startup. Create a user and device token with:

```text
python -m trac.manage create-user <github_login>
```

The token is printed once; only its SHA-256 hash is stored. Send versioned activity
batches to `POST /v1/events` with `Authorization: Bearer <token>`. Batches are
limited to 100 events and 256 KB, with a per-device limit of 120 requests per
minute. Accepted task, Git, session, and test-command events are also added to
the existing dashboard feed.

Revoke a device with:

```text
python -m trac.manage revoke-device <id>
```

## Install the TRAC activity tracker

From the repository root, install the collector and configure a repository:

```text
pip install -e collector
trac login --url <server> --token <device-token>
trac init
trac init --claude  # Optional: merge Claude Code hooks into .claude/settings.json
trac status
```

The tracker reports task summaries and outcomes plus session boundaries. Git
hooks report commit statistics and branch/push metadata. Optional Claude hooks
report edited-file paths, language and line counts; command kind and exit code;
and a prompt-submission count. TRAC never collects source or file contents,
diffs, prompt text, shell-command text, environment values, secrets, or commit
messages.

Pause collection immediately with `trac pause`; inspect queued and recently
sent metadata with `trac data`; resume with `trac resume`. Offline events stay
queued for `trac flush` when the server is reachable. The progress-reporting
skill is in [`skill/trac-activity/SKILL.md`](skill/trac-activity/SKILL.md).
Claude hook settings can also be viewed in
[`collector/examples/.claude/settings.json`](collector/examples/.claude/settings.json).
