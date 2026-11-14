ALTER TABLE tasks ADD COLUMN external_key TEXT;

CREATE UNIQUE INDEX idx_tasks_project_external_key
    ON tasks (project_id, external_key COLLATE NOCASE)
    WHERE external_key IS NOT NULL;

CREATE TABLE github_repository_mappings (
    repository TEXT PRIMARY KEY COLLATE NOCASE,
    project_id TEXT NOT NULL,
    actor_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (actor_user_id) REFERENCES users(id)
);

CREATE INDEX idx_github_repository_mappings_project
    ON github_repository_mappings (project_id);

CREATE TABLE github_webhook_deliveries (
    delivery_id TEXT PRIMARY KEY,
    event_name TEXT NOT NULL,
    repository TEXT NOT NULL,
    project_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    normalized_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
