import sqlite3

from models import Document, Event

CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS events (
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    course      TEXT,
    title       TEXT,
    event_type  TEXT,
    due         TEXT,
    release     TEXT,
    status      TEXT,
    score       REAL,
    max_points  REAL,
    url         TEXT,
    extra       TEXT,
    PRIMARY KEY (source, source_id)
)
"""

CREATE_DOCUMENTS = """
CREATE TABLE IF NOT EXISTS documents (
    source      TEXT NOT NULL,
    source_id   TEXT NOT NULL,
    course      TEXT,
    title       TEXT,
    filename    TEXT,
    local_path  TEXT,
    url         TEXT,
    updated_at  TEXT,
    PRIMARY KEY (source, source_id)
)
"""


class Database:
    def __init__(self, path: str = "data.db"):
        self.conn = sqlite3.connect(path)
        self.conn.execute(CREATE_EVENTS)
        self.conn.execute(CREATE_DOCUMENTS)
        self.conn.commit()

    def upsert_event(self, e: Event) -> None:
        self.conn.execute(
            """
            INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source, source_id) DO UPDATE SET
                course     = excluded.course,
                title      = excluded.title,
                event_type = excluded.event_type,
                due        = excluded.due,
                release    = excluded.release,
                status     = excluded.status,
                score      = excluded.score,
                max_points = excluded.max_points,
                url        = excluded.url,
                extra      = excluded.extra
            """,
            (e.source, e.source_id, e.course, e.title, e.event_type,
             e.due, e.release, e.status, e.score, e.max_points, e.url, e.extra),
        )
        self.conn.commit()

    def upsert_document(self, d: Document) -> None:
        self.conn.execute(
            """
            INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(source, source_id) DO UPDATE SET
                course     = excluded.course,
                title      = excluded.title,
                filename   = excluded.filename,
                local_path = excluded.local_path,
                url        = excluded.url,
                updated_at = excluded.updated_at
            """,
            (d.source, d.source_id, d.course, d.title,
             d.filename, d.local_path, d.url, d.updated_at),
        )
        self.conn.commit()
