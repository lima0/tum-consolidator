import json
import logging
import time
from pathlib import Path

from anthropic import Anthropic, RateLimitError
import os

from pypdf import PdfReader
from storage import models

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You analyze TUM Informatics/Informatik course PDFs for a student preparing for exams.

Match language of the course: German source → German output, English source → English output. Fields may mix languages where the source does.
Be precise: if the PDF doesn't explicitly state something, do not infer it. Prefer null or empty list over guessing."""

USER_PROMPT = """Analyze this PDF and populate these fields:
- summary: 2-3 sentences describing the document and why a student should care.
- topics: specific concepts covered, max 8 items.
- estimated_minutes: realistic study time (reading time for lectures, solving time for assignments).
- prerequisites: concepts needed before tackling this. Empty list if introductory.
- difficulty: easy, medium, or hard.
- key_takeaways: 3-5 bullet points a student would write in their notes.
- problem_count: count of distinct Aufgaben/problems for assignments/tutorials, null otherwise.
- release_date: YYYY-MM-DD if the document states a release/issue/Ausgabe date - could be a week range etc. - use the first day, null if unknown."""


def summarize_document(doc: models.Document) -> dict:
    if not doc.local_path:
        raise ValueError(f"Document {doc.source_id} has no local_path")

    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    with open(doc.local_path, "rb") as f:
        uploaded = client.beta.files.upload(file=("document.pdf", f, "application/pdf"))
    log.debug("Uploaded file id=%s", uploaded.id)

    try:
        response = client.beta.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            betas=["files-api-2025-04-14"],
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": USER_PROMPT},
                        {
                            "type": "document",
                            "source": {
                                "type": "file",
                                "file_id": uploaded.id,
                            },
                        },
                    ],
                }
            ],
            output_config={
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "summary": {"type": "string"},
                            "topics": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "estimated_minutes": {"type": "integer"},
                            "prerequisites": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "difficulty": {
                                "type": "string",
                                "enum": ["easy", "medium", "hard"],
                            },
                            "key_takeaways": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "problem_count": {"type": ["integer", "null"]},
                            "release_date": {"type": ["string", "null"]},
                        },
                        "required": [
                            "summary",
                            "topics",
                            "estimated_minutes",
                            "prerequisites",
                            "difficulty",
                            "key_takeaways",
                            "problem_count",
                            "release_date",
                        ],
                        "additionalProperties": False,
                    },
                }
            },
        )
    finally:
        client.beta.files.delete(uploaded.id)
        log.debug("Deleted remote file id=%s", uploaded.id)
    return json.loads(response.content[0].text)


SUPPORTED_EXTENSIONS = {".pdf"}


def process_unprocessed(db: Database, limit: int = 0) -> None:
    """Process unprocessed PDFs. limit=0 means no cap."""
    docs = db.get_unprocessed_documents()
    processed = 0
    for doc in docs:
        if limit and processed >= limit:
            log.info("Limit of %d reached, stopping. %d doc(s) remain.", limit, len(docs) - processed)
            break
        if not doc.local_path or Path(doc.local_path).suffix.lower() not in SUPPORTED_EXTENSIONS:
            log.debug("Skipping non-PDF: %s", doc.filename)
            db.mark_document_processed(doc.source, doc.source_id)
            continue

        try:
            page_count = len(PdfReader(doc.local_path).pages)
        except Exception as e:
            log.warning("Skipping unreadable PDF %s: %s", doc.filename, e)
            db.mark_document_processed(doc.source, doc.source_id)
            continue

        if page_count > 20:
            log.info("Skipping %s: %d pages (max 20)", doc.filename, page_count)
            db.mark_document_processed(doc.source, doc.source_id)
            continue

        for attempt in range(4):
            try:
                summary = summarize_document(doc)
                db.upsert_document(models.Document(
                    source=doc.source,
                    source_id=doc.source_id,
                    course=doc.course,
                    title=doc.title,
                    filename=doc.filename,
                    local_path=doc.local_path,
                    url=doc.url,
                    summary_json=json.dumps(summary),
                    updated_at=doc.updated_at,
                ))
                db.mark_document_processed(doc.source, doc.source_id)
                try:
                    from intelligence.embeddings import embed_document
                    import numpy as np
                    vec = embed_document(summary)
                    db.conn.execute(
                        "UPDATE documents SET embedding = ? WHERE source = ? AND source_id = ?",
                        (vec.astype(np.float32).tobytes(), doc.source, doc.source_id),
                    )
                    db.conn.commit()
                except Exception as e:
                    log.warning("Embedding failed for %s: %s", doc.title, e)
                log.info("✓ %s", doc.title)
                break
            except RateLimitError:
                #Claude API limits at 30k input tokens per minute
                if attempt == 3:
                    log.error("✗ %s: rate limit, giving up", doc.title)
                else:
                    wait = 60 * (attempt + 1)
                    log.warning("Rate limit hit, waiting %ds…", wait)
                    time.sleep(wait)
            except Exception as e:
                log.error("✗ %s: %s", doc.title, e)
                break

if __name__ == "__main__":
    from storage.db import Database
    process_unprocessed(Database())