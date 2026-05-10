import json
import logging

import numpy as np
from sentence_transformers import SentenceTransformer

log = logging.getLogger(__name__)

_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        log.info("Loading embedding model…")
        _model = SentenceTransformer("intfloat/multilingual-e5-small")
    return _model


def _doc_to_text(summary_json: dict) -> str:
    summary   = summary_json.get("summary", "")
    topics    = " ".join(summary_json.get("topics", []))
    takeaways = " ".join(summary_json.get("key_takeaways", []))
    return f"passage: {summary} {topics} {takeaways}"


def embed(text: str) -> np.ndarray:
    return _get_model().encode(text, normalize_embeddings=True)


def embed_document(summary_json: dict) -> np.ndarray:
    return embed(_doc_to_text(summary_json))


def search_by_embedding(db, query: str, limit: int = 5) -> list:
    q_vec = embed(f"query: {query}")
    rows = db.conn.execute("""
        SELECT source_id, course, title, summary_json, local_path, embedding
        FROM documents
        WHERE processed_at IS NOT NULL AND embedding IS NOT NULL
    """).fetchall()
    if not rows:
        return []
    scored = []
    for r in rows:
        doc_vec = np.frombuffer(r["embedding"], dtype=np.float32)
        score   = float(np.dot(q_vec, doc_vec))
        scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:limit]]


def backfill(db) -> None:
    """Generate embeddings for all summarized docs that don't have one yet."""
    rows = db.conn.execute("""
        SELECT source, source_id, summary_json
        FROM documents
        WHERE processed_at IS NOT NULL
          AND summary_json IS NOT NULL
          AND embedding IS NULL
    """).fetchall()
    if not rows:
        log.info("All documents already have embeddings.")
        return
    log.info("Backfilling embeddings for %d document(s)…", len(rows))
    for r in rows:
        try:
            s   = json.loads(r["summary_json"])
            vec = embed_document(s)
            db.conn.execute(
                "UPDATE documents SET embedding = ? WHERE source = ? AND source_id = ?",
                (vec.astype(np.float32).tobytes(), r["source"], r["source_id"]),
            )
        except Exception as e:
            log.warning("Failed to embed %s/%s: %s", r["source"], r["source_id"], e)
    db.conn.commit()
    log.info("Backfill complete.")
