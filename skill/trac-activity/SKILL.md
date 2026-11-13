---
name: trac-activity
description: Reports high-level progress (task started, completed, blocked) to TRAC so the team dashboard shows what the developer is working on; use at the start and end of every coding task.
---

At the start of every coding task, run:

`trac emit task.started --summary "<one sentence>"`

At the end, run:

`trac emit task.completed --summary "<one sentence>" --outcome done|partial|abandoned`

If stuck, run:

`trac emit task.blocked --summary "<one sentence>" --reason "<short reason>"`

Rules:

- Summaries must be one plain sentence about the goal and must never include code, file contents, prompts, secrets, or personal data.
- If a command fails or `trac` is not installed, continue silently.
- Report nothing else.
