# TRAC GitHub and CI Integration

The GitHub integration accepts signed GitHub webhooks and writes normalized
records into the existing version-2 TRAC activity pipeline. It does not create
a second event store or accept project identity from the webhook body.

## Configure the webhook

1. Generate a high-entropy webhook secret and store it in the server's secret
   manager as `TRAC_GITHUB_WEBHOOK_SECRET`. Do not commit the value or put it in
   application logs.
2. Map each repository to exactly one authorized TRAC project using a
   deployment/admin shell:

   ```text
   python -m trac.manage map-github-repo payments example/payments
   python -m trac.manage unmap-github-repo example/payments
   ```

   The mapping command creates a dedicated `GitHub Integration` system actor
   for that project. GitHub usernames are not treated as verified TRAC
   identities; events are attributed to this system actor rather than
   impersonating a developer.
3. In the GitHub repository's **Settings → Webhooks**, configure the public
   HTTPS URL:

   ```text
   https://<trac-host>/v1/integrations/github/webhook
   ```

   Set the same secret and select JSON payloads. Subscribe to `push`,
   `pull_request`, `workflow_run`, and `check_run`. GitHub signs the raw request
   body with `X-Hub-Signature-256`; TRAC verifies the HMAC-SHA256 signature
   using constant-time comparison before JSON parsing or persistence.

The endpoint returns `503` if the server secret is not configured, `401` for a
missing or invalid signature, `404` when a supported event names an unmapped
repository, and `202` for accepted, duplicate, or ignored deliveries. Keep the
webhook secret synchronized when rotating it in GitHub and the server secret
manager.

## Normalized activity

The endpoint is `POST /v1/integrations/github/webhook`. Accepted webhook
deliveries are normalized as strict `schema_version: 2` activity events and
persisted with the canonical event store and SSE stream. A stable UUID is
derived from the signed delivery ID and event subject; the delivery table's
primary key and the activity event primary key prevent duplicate replay.
Re-delivery returns `{"accepted":0,"duplicate":true}`.

| GitHub webhook | Normalized TRAC event | Behavior |
| --- | --- | --- |
| `push` to a branch | `git.commit` (`source: github`) | Records the push head commit only; tag refs and pushes without a valid head SHA are ignored. Commit messages, diffs, file contents, and author names are not stored. |
| `pull_request` action `opened` | `git.pull_request.opened` (`source: github`) | Records PR number, base/head branch, and mapped repository. |
| `pull_request` action `synchronize` | `git.pull_request.updated` (`source: github`) | Records a PR update without duplicating an `opened` event. |
| `pull_request` action `closed` with `merged: true` | `git.pull_request.merged` (`source: github`) | Records merge evidence only; it does not complete the task. Unmerged closes are ignored. |
| `workflow_run` action `completed` | `build.completed` (`source: ci`) | Records workflow name/ID/run ID, outcome, duration when timestamps are valid, repository, and branch. |
| `check_run` action `completed` | `build.completed` (`source: ci`) | Records check name/ID, outcome, duration when timestamps are valid, repository, and branch. |

Workflow/check conclusions normalize to `passed`, `failed`, or `cancelled`.
Raw workflow logs, check output, source code, commit messages, environment
variables, and secrets are never copied into event metadata. Only allowlisted
fields that validate against the canonical event models are persisted.

`GET /v1/projects/{project_id}/activity`, `GET /v1/events`, and
`GET /v1/events/stream` apply their existing credential and project
authorization rules. Project-wide activity remains limited to project heads;
members see GitHub/CI items when linked to one of their assigned tasks.

## Explicit task association

Project heads can set an optional `external_key` when creating or patching a
task, for example:

```json
{
  "title": "Harden payment retries",
  "external_key": "TRAC-101"
}
```

Keys are unique within a project and use the form
`<letters/digits/underscore/hyphen prefix>-<positive integer>`. TRAC searches
only the mapped project. A commit branch, PR title, PR head branch, or CI branch
must contain exactly one key that resolves to a task in that project before
the normalized event receives a `task_id`. Multiple distinct references,
unknown keys, and missing references remain project-level activity without a
task link. The original PR title and commit message are not retained.

GitHub evidence linked to a task is recorded in task history with the
integration actor and event ID. These `git.*` and `build.completed` event
types do not transition task status or progress. Only explicit task lifecycle
events or authorized manual task updates change task state.

## Origin and identity

- Agent-reported activity is labeled `source: skill`, `cli`, or `mcp`.
- GitHub-signature-verified activity is labeled `source: github`.
- GitHub Actions workflow/check evidence is labeled `source: ci`.
- Manual task edits remain task-history records with no canonical event ID;
  they are not represented as GitHub or CI verified activity.

The current integration intentionally attributes webhook events to a
project-scoped `GitHub Integration` actor. It does not resolve GitHub authors
to TRAC users because the application does not yet have a verified GitHub
account-link flow.

## Database migration

Migration `trac/migrations/004_github_ci_integration.sql` adds:

- `tasks.external_key` and a project-scoped unique index.
- `github_repository_mappings`, which explicitly maps case-insensitive
  `owner/repository` names to a TRAC project and integration actor.
- `github_webhook_deliveries`, whose unique delivery ID makes webhook
  processing idempotent and stores only normalized delivery bookkeeping.

The migration is additive. Existing tasks, events, users, and project data are
preserved.

## Limitations

- The webhook secret is one server-configured secret; per-repository secrets,
  GitHub App installation authorization, OAuth identity verification, and
  remote secret rotation automation are not implemented.
- Repository mappings are managed through the local trusted admin CLI rather
  than an HTTP endpoint.
- A push produces one event for its head commit, not one event per commit.
- Only completed workflows/check runs and the listed pull-request actions
  produce records; other actions are acknowledged but ignored.
- `workflow_run` and `check_run` both map to `build.completed`; test results
  are not inferred from arbitrary workflow names.
- SSE durability uses the existing SQLite event stream. Multi-host deployment
  still requires shared transactional storage or an external event bus.
