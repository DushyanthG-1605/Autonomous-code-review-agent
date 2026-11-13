CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

ALTER TABLE users ADD COLUMN public_id TEXT;
ALTER TABLE devices ADD COLUMN project_id TEXT REFERENCES projects(id);
ALTER TABLE devices ADD COLUMN scopes TEXT NOT NULL DEFAULT '["events:ingest","events:read"]';
ALTER TABLE events ADD COLUMN project_id TEXT REFERENCES projects(id);
ALTER TABLE events ADD COLUMN actor_user_id INTEGER REFERENCES users(id);
ALTER TABLE events ADD COLUMN activity_event_id TEXT;
ALTER TABLE activity_events ADD COLUMN project_id TEXT REFERENCES projects(id);
ALTER TABLE activity_events ADD COLUMN task_id TEXT;

CREATE TABLE project_memberships (
    project_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner', 'member', 'viewer')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, user_id),
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE activity_event_stream (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES activity_events(event_id) ON DELETE CASCADE,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

DROP INDEX idx_events_execution_id;
CREATE UNIQUE INDEX idx_events_project_execution_id
    ON events (project_id, execution_id) WHERE execution_id IS NOT NULL;
CREATE UNIQUE INDEX idx_events_activity_event_id
    ON events (activity_event_id) WHERE activity_event_id IS NOT NULL;
CREATE INDEX idx_events_project_timestamp
    ON events (project_id, timestamp DESC);
CREATE INDEX idx_activity_events_project_received
    ON activity_events (project_id, received_at DESC);
CREATE INDEX idx_activity_event_stream_project_sequence
    ON activity_event_stream (project_id, sequence);
