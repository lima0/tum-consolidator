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

# TODO - Content Hashing
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
    summary_json  TEXT,
    content_hash  TEXT,
    first_seen    TEXT,
    processed_at  TEXT DEFAULT NULL,
    PRIMARY KEY (source, source_id)
)
"""
class Database:
    def __init__(self, path: str = "state/data.db"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(CREATE_EVENTS)
        self.conn.execute(CREATE_DOCUMENTS)
        # migration: add first_seen column
        try:
            self.conn.execute("ALTER TABLE documents ADD COLUMN first_seen TEXT")
            self.conn.execute("UPDATE documents SET first_seen = datetime('now') WHERE first_seen IS NULL")
        except sqlite3.OperationalError:
            pass
        # migration: reset processed_at for rows where summary_json was wiped by a connector
        # re-sync (connector upsert previously overwrote summary_json with NULL)
        self.conn.execute("""
            UPDATE documents
            SET processed_at = NULL
            WHERE processed_at IS NOT NULL
              AND summary_json IS NULL
        """)
        self.conn.commit()

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
                                  local_path, url, updated_at, summary_json, first_seen)
            VALUES (?,?,?,?,?,?,?,?,?, datetime('now'))
            ON CONFLICT(source, source_id) DO UPDATE SET
                course       = excluded.course,
                title        = excluded.title,
                filename     = excluded.filename,
                local_path   = excluded.local_path,
                url          = excluded.url,
                updated_at   = excluded.updated_at,
                summary_json = COALESCE(documents.summary_json, excluded.summary_json)
            """,
            (d.source, d.source_id, d.course, d.title,
             d.filename, d.local_path, d.url, d.updated_at, d.summary_json),
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

    def get_unprocessed_events(self) -> list[Event]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE processed_at IS NULL ORDER BY due ASC NULLS LAST"
        ).fetchall()
        return [
            Event(
                source=r["source"], source_id=r["source_id"], course=r["course"],
                title=r["title"], event_type=r["event_type"], due=r["due"],
                release=r["release"], status=r["status"], score=r["score"],
                max_points=r["max_points"], url=r["url"], extra=r["extra"],
            )
            for r in rows
        ]

    def get_unprocessed_documents(self) -> list[Document]:
        rows = self.conn.execute(
            "SELECT * FROM documents WHERE processed_at IS NULL ORDER BY course, title"
        ).fetchall()
        return [
            Document(
                source=r["source"], source_id=r["source_id"], course=r["course"],
                title=r["title"], filename=r["filename"], local_path=r["local_path"],
                url=r["url"], summary_json=r["summary_json"], updated_at=r["updated_at"],
                first_seen=r["first_seen"],
            )
            for r in rows
        ]
