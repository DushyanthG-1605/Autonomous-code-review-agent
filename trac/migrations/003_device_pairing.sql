CREATE TABLE device_pairings (
    id TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL,
    project_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    created_by_device_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    revoked_at TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id),
    FOREIGN KEY (project_id) REFERENCES projects(id),
    FOREIGN KEY (created_by_device_id) REFERENCES devices(id)
);

CREATE INDEX idx_device_pairings_expiry
    ON device_pairings (expires_at, consumed_at);

CREATE TABLE api_rate_limits (
    bucket_key TEXT PRIMARY KEY,
    window_started_at INTEGER NOT NULL,
    request_count INTEGER NOT NULL
);
