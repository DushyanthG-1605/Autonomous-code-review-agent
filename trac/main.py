import json
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path
from typing import Optional, List

from trac.db import get_connection, init_db
from trac.models import EventCreate, Event, now_iso

app = FastAPI(title="TRAC — Engineering Intelligence Layer")

STATIC_DIR = Path(__file__).parent / "static"


@app.on_event("startup")
def on_startup():
    init_db()


# ---------- Ingest an event ----------

@app.post("/api/events", response_model=Event)
def create_event(event: EventCreate):
    conn = get_connection()
    timestamp = now_iso()
    metadata_json = json.dumps(event.metadata)

    values = (
        event.type, event.source, event.summary, event.repo, event.ref, event.actor,
        timestamp, event.execution_id, event.status, metadata_json,
    )
    if event.execution_id:
        conn.execute(
            """
            INSERT INTO events
                (type, source, summary, repo, ref, actor, timestamp, execution_id, status, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(execution_id) WHERE execution_id IS NOT NULL DO UPDATE SET
                type = excluded.type,
                source = excluded.source,
                summary = excluded.summary,
                repo = excluded.repo,
                ref = excluded.ref,
                actor = excluded.actor,
                status = excluded.status,
                metadata = excluded.metadata
            """,
            values,
        )
        row = conn.execute(
            "SELECT * FROM events WHERE execution_id = ?", (event.execution_id,)
        ).fetchone()
    else:
        cursor = conn.execute(
            """
            INSERT INTO events
                (type, source, summary, repo, ref, actor, timestamp, execution_id, status, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        row = conn.execute(
            "SELECT * FROM events WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    conn.commit()
    conn.close()

    return _event_response(row)


def _event_response(row):
    event = dict(row)
    event["metadata"] = json.loads(event["metadata"] or "{}")
    return event


# ---------- List events (powers the timeline) ----------

@app.get("/api/events", response_model=List[Event])
def list_events(source: Optional[str] = None, limit: int = 100):
    conn = get_connection()

    if source:
        rows = conn.execute(
            "SELECT * FROM events WHERE source = ? ORDER BY timestamp DESC LIMIT ?",
            (source, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()

    conn.close()
    return [_event_response(row) for row in rows]


# ---------- Get a single event ----------

@app.get("/api/events/{event_id}", response_model=Event)
def get_event(event_id: int):
    conn = get_connection()
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return _event_response(row)


# ---------- Serve the dashboard ----------

@app.get("/")
def dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")