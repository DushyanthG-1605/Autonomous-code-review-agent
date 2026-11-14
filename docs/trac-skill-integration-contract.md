# Universal TRAC Skill Integration Contract

The shared skill is [`skill/trac/SKILL.md`](../skill/trac/SKILL.md), and the
host compatibility and installation workflow is documented in
[`TRAC_UNIVERSAL_INTEGRATION.md`](TRAC_UNIVERSAL_INTEGRATION.md).

Every host uses the same collector, consent policy, version-2 event contract,
project-bound credentials, task authorization, pause/revoke behavior, and
privacy rules. Host-specific adapters may install the skill or expose optional
hooks; they do not define a separate backend or event schema.

The event API, metadata allowlists, pairing APIs, and task authorization are
specified in [`TRAC_EVENT_CONTRACT.md`](TRAC_EVENT_CONTRACT.md). Version-2
task events contain a real task UUID and only approved structured metadata.
Credentials determine the project and actor; caller-supplied identity claims
are ignored. The API rejects invalid metadata and recognizable secret values.

The collector does not track automatically. The agent must ask for explicit
developer permission before using TRAC for a workspace or task. It reports
meaningful task milestones only; unavailable TRAC must never block the actual
development work.
