## Script to summarize all the downloaded documents with Claude

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
Your output is consumed by an automated study planner, so you must return strict JSON only.
no preamble, no markdown fences, no commentary.

Student speaks German and English. Output fields can mix languages where the source does. Match the language of the course if german then german, if english then english.
Be precise: if the PDF doesn't explicitly state something, DO NOT infer it. Prefer "unknown" over guessing. NO MARKDOWN FENCES, NO ```json... WHATSOEVER

User: "Analyze this file..."
Response: "{
  "summary": "Übungsblatt 2 zu Grundlagen: Algorithmen und Datenstrukturen. Behandelt mathematische Induktion, Laufzeit-Analyse von Funktionen, asymptotische Notation (O, o, Ω, ω, Θ) und deren Eigenschaften. Für Klausurvorbereitung essentiell.",
  "topics": [
    "Mathematische Induktion",
    "Laufzeit-Analyse",
    "O-Notation und asymptotische Notation (o, O, ω, Ω, Θ)",
    "Transitivitätsregeln für Landau-Notation",
    "Funktionswachstum vergleichen",
    "Rekursive Funktionen analysieren",
    "Summen und Reihen"
  ],
  "estimated_minutes": 240,
  "prerequisites": [
    "Mathematische Induktion (Grundlagen)",
    "Programmierung und Kontrollflussverstehen",
    "Grundlagen der mathematischen Analysis",
    "Vertrautheit mit asymptotischer Notation aus Vorlesung"
  ],
  "difficulty": "hard",
  "key_takeaways": [
    "Induktionsbeweise erfordern sorgfältige Basis- und Induktionsschritte; Formeln aus Hinweisen verwenden",
    "Laufzeitanalyse: Verschachtelte Schleifen multiplizieren (Funktion 1: O(|A|·|B|)), Fibonacci rekursiv ist exponentiell (Funktion 2: O(2^n)), einfache Rekursion linear (Funktion 3: O(n))",
    "Landau-Symbole präzise anwenden: o (echt kleiner), O (≤), ω (echt größer), Ω (≥), Θ (gleich); 'u.' wenn unvergleichbar",
    "Transitivitätsregeln ermöglichen Zwischenschritte bei komplexen Beweisen: o und O sind transitiv",
    "Ordnung von Funktionen: konstant < logarithmisch < polynomial < exponentiell; 1.1^n wächst langsamer als polynomial mit hohem Grad"
  ],
  "problem_count": 8
}"
"""

USER_PROMPT = """Analyze this file and return JSON with these fields:

{
  "summary": "2-3 sentence description of what this document is and why a student should care.",
  "topics": ["specific concepts covered, max 8, use the language the source uses"],
  "estimated_minutes": <integer, realistic time for an average student to work through this. For lectures: reading time. For tutorials/assignments: solving time.>,
  "prerequisites": ["concepts the student needs before tackling this. Empty list if it's introductory."],
  "difficulty": "easy" | "medium" | "hard",
  "key_takeaways": ["3-5 bullet points a student would write in their notes"],
  "problem_count": <integer or null. Only for tutorials/assignments. Count distinct Aufgaben/problems.>
}

Return only the JSON object.
DO NOT UNDER ANY CIRCUMSTANCES WHATSOEVER APPEND MARKDOWN FENCES"""


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
        )
    finally:
        client.beta.files.delete(uploaded.id)
        log.debug("Deleted remote file id=%s", uploaded.id)
    return json.loads(response.content[0].text)


SUPPORTED_EXTENSIONS = {".pdf"}


def process_unprocessed(db, limit: int = 0) -> None:
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