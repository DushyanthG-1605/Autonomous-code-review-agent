ALTER TABLE users ADD COLUMN email TEXT;
ALTER TABLE users ADD COLUMN github_username TEXT;
ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'developer';
ALTER TABLE devices ADD COLUMN auth_mode TEXT NOT NULL DEFAULT 'device';
ALTER TABLE projects ADD COLUMN description TEXT;
ALTER TABLE projects ADD COLUMN project_key TEXT;
ALTER TABLE projects ADD COLUMN project_head_id INTEGER REFERENCES users(id);

UPDATE users SET github_username = github_login
WHERE github_username IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM users AS duplicate
      WHERE lower(duplicate.github_login) = lower(users.github_login)
        AND duplicate.id != users.id
  );
UPDATE projects SET project_key = slug WHERE project_key IS NULL;
UPDATE projects SET project_head_id = (
    SELECT project_memberships.user_id FROM project_memberships
    WHERE project_memberships.project_id = projects.id
      AND project_memberships.role = 'owner'
    ORDER BY project_memberships.created_at LIMIT 1
)
WHERE project_head_id IS NULL;

CREATE UNIQUE INDEX idx_users_github_username
    ON users (lower(github_username)) WHERE github_username IS NOT NULL;
CREATE UNIQUE INDEX idx_projects_project_key
    ON projects (project_key) WHERE project_key IS NOT NULL;

CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    assignee_id INTEGER,
    status TEXT NOT NULL DEFAULT 'todo'
        CHECK (status IN ('todo', 'in_progress', 'blocked', 'in_review', 'done')),
    priority TEXT NOT NULL DEFAULT 'medium'
        CHECK (priority IN ('low', 'medium', 'high', 'urgent')),
    progress_percent INTEGER NOT NULL DEFAULT 0
        CHECK (progress_percent BETWEEN 0 AND 100),
    progress_is_estimate INTEGER NOT NULL DEFAULT 0
        CHECK (progress_is_estimate IN (0, 1)),
    due_date TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (assignee_id) REFERENCES users(id)
);

CREATE TABLE task_updates (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    actor_id INTEGER NOT NULL,
    event_id TEXT,
    update_type TEXT NOT NULL,
    note TEXT,
    progress_percent INTEGER
        CHECK (progress_percent IS NULL OR progress_percent BETWEEN 0 AND 100),
    progress_is_estimate INTEGER NOT NULL DEFAULT 0
        CHECK (progress_is_estimate IN (0, 1)),
    timestamp TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    FOREIGN KEY (actor_id) REFERENCES users(id),
    FOREIGN KEY (event_id) REFERENCES activity_events(event_id)
);

CREATE INDEX idx_tasks_project_status
    ON tasks (project_id, status, updated_at DESC);
CREATE INDEX idx_tasks_assignee_project
    ON tasks (assignee_id, project_id, updated_at DESC);
CREATE INDEX idx_task_updates_task_timestamp
    ON task_updates (task_id, timestamp DESC);
CREATE UNIQUE INDEX idx_task_updates_event
    ON task_updates (event_id) WHERE event_id IS NOT NULL;
