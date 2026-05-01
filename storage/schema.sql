CREATE TABLE IF NOT EXISTS events (
    source        TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    course        TEXT,
    title         TEXT,
    event_type    TEXT,          -- assignment, quiz, lecture, exam, class, etc.
    due           TEXT,          -- YYYY-MM-DD HH:MM
    release       TEXT,          -- YYYY-MM-DD HH:MM
    status        TEXT,          -- NOT_STARTED, STARTED, SUBMITTED, FINISHED, OVERDUE
    score         REAL,
    max_points    REAL,
    url           TEXT,
    extra         TEXT,          -- JSON blob for source-specific fields
    processed_at  TEXT DEFAULT NULL,
    PRIMARY KEY (source, source_id)
);

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
    processed_at  TEXT DEFAULT NULL,
    PRIMARY KEY (source, source_id)
);
