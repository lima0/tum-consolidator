import sqlite3

from storage.models import Document, Event

CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS events (
    source        TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    course        TEXT,
    title         TEXT,
    event_type    TEXT,
    due           TEXT,
    release       TEXT,
    status        TEXT,
    score         REAL,
    max_points    REAL,
    url           TEXT,
    extra         TEXT,
    processed_at  TEXT DEFAULT NULL,
    PRIMARY KEY (source, source_id)
)
"""

CREATE_DOCUMENTS = """
CREATE TABLE IF NOT EXISTS documents (
    source        TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    course        TEXT,
    title         TEXT,
    filename      TEXT,
    local_path    TEXT,
    url           TEXT,
    updated_at    TEXT,
    processed_at  TEXT DEFAULT NULL,
    PRIMARY KEY (source, source_id)
)
"""

MIGRATE_EVENTS = "ALTER TABLE events ADD COLUMN processed_at TEXT DEFAULT NULL"
MIGRATE_DOCUMENTS = "ALTER TABLE documents ADD COLUMN processed_at TEXT DEFAULT NULL"


class Database:
    def __init__(self, path: str = "state/data.db"):
        self.conn = sqlite3.connect(path)
        self.conn.execute(CREATE_EVENTS)
        self.conn.execute(CREATE_DOCUMENTS)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        existing = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(events)")
        }
        if "processed_at" not in existing:
            self.conn.execute(MIGRATE_EVENTS)
        existing = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(documents)")
        }
        if "processed_at" not in existing:
            self.conn.execute(MIGRATE_DOCUMENTS)

    def upsert_event(self, e: Event) -> None:
        self.conn.execute(
            """
            INSERT INTO events(source, source_id, course, title, event_type,
                               due, release, status, score, max_points, url, extra)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
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
            INSERT INTO documents(source, source_id, course, title, filename,
                                  local_path, url, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
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

    def mark_event_processed(self, source: str, source_id: str) -> None:
        self.conn.execute(
            "UPDATE events SET processed_at = datetime('now') WHERE source = ? AND source_id = ?",
            (source, source_id),
        )
        self.conn.commit()

    def mark_document_processed(self, source: str, source_id: str) -> None:
        self.conn.execute(
            "UPDATE documents SET processed_at = datetime('now') WHERE source = ? AND source_id = ?",
            (source, source_id),
        )
        self.conn.commit()
