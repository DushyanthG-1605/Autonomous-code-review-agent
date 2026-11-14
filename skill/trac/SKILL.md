---
name: trac
description: Use TRAC to read assigned work and report meaningful task lifecycle milestones during authorized coding work.
---

# TRAC task reporting

TRAC is optional progress reporting. The developer's code changes and requested
work take priority over TRAC.

## Permission and setup

1. Before activating TRAC for a workspace or task, ask the developer for
   explicit permission. Do not treat installing the collector, an existing
   credential, or an earlier approval for another workspace as consent.
2. If the developer declines, do not inspect or transmit TRAC activity. Continue
   the requested development work normally.
3. If the developer agrees, check `trac status`. If not connected, ask the
   developer to pair the collector with their authorized project:

   ```text
   trac login --url https://<trac-server>
   ```

   The command prompts for a one-time pairing code without echoing it. Do not
   request or handle a long-lived token in chat.
4. If the skill is not installed for the active agent, the developer may install
   it from the TRAC repository using `trac init --target <environment>`.

## Per-task workflow

After permission is granted and the collector is connected:

1. At the beginning of relevant work, run `trac tasks` and review the tasks
   assigned to the authenticated developer. Do not query another project's
   tasks or use another person's credentials.
2. Associate the request with one assigned task using its UUID shown by the
   collector. If there is no clear match, ask the developer which task applies;
   never create or claim a task or impersonate an assignee.
3. When substantive work actually begins, report:

   ```text
   trac task start <task-uuid>
   ```

4. Report only meaningful milestones, not every prompt, keystroke, file
   operation, or routine command. An optional short note must be a high-level
   summary written by the developer/agent. Never include code, diffs, file
   contents, prompts, command text, paths that reveal private data, or secrets:

   ```text
   trac task progress <task-uuid> --percent 50 --note "Core flow implemented"
   ```

   Percentages are subjective progress estimates, not measured completion.
5. If blocked on a material dependency, report a fixed reason code and, only
   when safe, a short non-sensitive note:

   ```text
   trac task block <task-uuid> --reason-code dependency --note "Waiting on API access"
   ```

6. Report `done` only after running the checks appropriate to the change and
   confirming the requested behavior. If verification is incomplete or failed,
   do not report completion as done:

   ```text
   trac task done <task-uuid> --note "Relevant tests passed"
   ```

## Privacy and availability

- Never transmit source code, diffs, prompts, secrets, credentials, file
  contents, environment variables, command text, or private test output.
- Only the version-2 task event allowlist is transmitted: task ID, event type,
  source, timestamp, a bounded integer progress estimate, an approved blocker
  reason code, an outcome, and an optional short redacted note.
- Do not add tracking hooks, change IDE settings, or enable background
  collection unless the developer explicitly requests that setup.
- If TRAC is unavailable, authentication fails, or an event cannot be queued,
  tell the developer briefly when useful and continue the actual coding task.
  Do not repeatedly retry, block work, or substitute a guessed/synthetic
  activity event.
- The developer can inspect local queued/sent records with `trac data`, pause
  with `trac pause`, resume with `trac resume`, or revoke this device and clear
  pending records with `trac disconnect`.
