ALTER TABLE activity_events
    ADD COLUMN device_id INTEGER REFERENCES devices(id);

CREATE INDEX idx_activity_events_device_received
    ON activity_events (device_id, received_at DESC);

ALTER TABLE device_pairings
    ADD COLUMN claimed_device_id INTEGER REFERENCES devices(id);
