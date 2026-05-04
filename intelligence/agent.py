import json
import logging
import os
from datetime import datetime

from anthropic import Anthropic

from storage.db import Database

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a study assistant for a TUM Informatics student.
You have access to the student's course materials database and deadline tracker.

Tool usage policy — follow strictly:
1. For general questions (topics, difficulty, what to study): use search_documents or get_document_content only.
2. read_pdf is LAST RESORT. Only call it when the user explicitly asks to solve a problem, see a proof, or needs exact content. It costs many tokens.
3. Never call read_pdf if get_document_content already answered the question.
4. Prefer fewer tool calls. If search_documents returns enough context, answer directly.

Be concrete and specific. Cite document titles. Walk through problems step by step when solving.
"""

TOOLS = [
    {
        "name": "read_pdf",
        "description": (
            "Read the raw text of a PDF. EXPENSIVE — only call this when summary and key_takeaways "
            "are genuinely insufficient: e.g. the user asks to solve a specific problem, wants a proof, "
            "or needs exact content not captured in the summary. "
            "Do NOT use for general questions about topics, difficulty, or what a document covers — "
            "get_document_content handles those. Always try get_document_content first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "local_path": {
                    "type": "string",
                    "description": "File path as returned in search_documents or get_document_content results",
                },
                "pages": {
                    "type": "integer",
                    "description": "Max pages to read (default 10). Reduce for large docs.",
                    "default": 10,
                },
            },
            "required": ["local_path"],
        },
    },
    {
        "name": "search_documents",
        "description": (
            "Search course documents (lecture slides, tutorials, assignments) by topic or keyword. "
            "Searches title, course name, topics list, summary, and key takeaways. "
            "Use multiple space-separated terms for better results. "
            "Call multiple times with different phrasings (German and English) if first results are insufficient."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Topic, concept, or keyword to search for",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 5)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_deadlines",
        "description": "Get upcoming assignment deadlines and their current status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Look-ahead window in days (default 14)",
                    "default": 14,
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_document_content",
        "description": (
            "Get the full summary and key takeaways for a specific document. "
            "Pass the Title value exactly as returned by search_documents (not the Course prefix). "
            "Use when you need more detail than search_documents provides."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Document title as returned in the 'Title:' field from search_documents",
                },
            },
            "required": ["title"],
        },
    },
]


def _search_documents(db: Database, query: str, limit: int = 5) -> str:
    terms = [t.strip() for t in query.replace(",", " ").split() if len(t.strip()) > 1]
    if not terms:
        return "Empty query."

    scores: dict[str, tuple[int, object]] = {}

    for term in terms:
        pat = f"%{term.lower()}%"

        # title + course match (weight 3)
        for r in db.conn.execute("""
            SELECT source_id, course, title, summary_json, local_path
            FROM documents WHERE processed_at IS NOT NULL AND summary_json IS NOT NULL
              AND (lower(title) LIKE ? OR lower(course) LIKE ?)
        """, (pat, pat)).fetchall():
            sid = r["source_id"]
            prev_score, _ = scores.get(sid, (0, r))
            scores[sid] = (prev_score + 3, r)

        # individual topic array items (weight 2)
        for r in db.conn.execute("""
            SELECT d.source_id, d.course, d.title, d.summary_json, d.local_path
            FROM documents d, json_each(json_extract(d.summary_json, '$.topics')) t
            WHERE d.processed_at IS NOT NULL AND d.summary_json IS NOT NULL
              AND json_valid(d.summary_json)
              AND lower(t.value) LIKE ?
        """, (pat,)).fetchall():
            sid = r["source_id"]
            prev_score, _ = scores.get(sid, (0, r))
            scores[sid] = (prev_score + 2, r)

        # summary text match (weight 1)
        for r in db.conn.execute("""
            SELECT source_id, course, title, summary_json, local_path
            FROM documents WHERE processed_at IS NOT NULL AND summary_json IS NOT NULL
              AND json_valid(summary_json)
              AND lower(json_extract(summary_json, '$.summary')) LIKE ?
        """, (pat,)).fetchall():
            sid = r["source_id"]
            prev_score, _ = scores.get(sid, (0, r))
            scores[sid] = (prev_score + 1, r)

        # key_takeaways match (weight 1)
        for r in db.conn.execute("""
            SELECT d.source_id, d.course, d.title, d.summary_json, d.local_path
            FROM documents d, json_each(json_extract(d.summary_json, '$.key_takeaways')) k
            WHERE d.processed_at IS NOT NULL AND d.summary_json IS NOT NULL
              AND json_valid(d.summary_json)
              AND lower(k.value) LIKE ?
        """, (pat,)).fetchall():
            sid = r["source_id"]
            prev_score, _ = scores.get(sid, (0, r))
            scores[sid] = (prev_score + 1, r)

    if not scores:
        return f"No documents found matching '{query}'."

    ranked = sorted(scores.values(), key=lambda x: x[0], reverse=True)[:limit]

    results = []
    for score, r in ranked:
        try:
            s = json.loads(r["summary_json"])
        except (json.JSONDecodeError, TypeError):
            continue
        topics = ", ".join(s.get("topics", [])[:5])
        mins = s.get("estimated_minutes", "?")
        diff = s.get("difficulty", "?")
        path_note = f" [file: {r['local_path']}]" if r["local_path"] else ""
        results.append(
            f"Course: {r['course']} | Title: {r['title']}{path_note}\n"
            f"  Summary: {s.get('summary', '')}\n"
            f"  Topics: {topics}\n"
            f"  Effort: ~{mins}min, {diff}"
        )
    return "\n\n".join(results)


def _get_deadlines(db, days: int = 14) -> str:
    rows = db.conn.execute("""
        SELECT course, title, due, score, max_points, status, url
        FROM events
        WHERE source = 'artemis'
          AND due BETWEEN datetime('now') AND datetime('now', ? || ' days')
        ORDER BY due
    """, (str(days),)).fetchall()

    if not rows:
        return f"No deadlines in the next {days} days."

    now = datetime.now()
    lines = []
    for r in rows:
        due = datetime.fromisoformat(r["due"]) if r["due"] else None
        due_str = due.strftime("%a %d %b %H:%M") if due else "?"
        delta = (due - now).total_seconds() if due else None
        urgency = " ⚠️ DUE SOON" if delta and delta < 86400 else ""
        score_str = f" (score: {r['score']}/{r['max_points']})" if r["score"] is not None else ""
        status = f" [{r['status']}]" if r["status"] else ""
        lines.append(f"- **{r['course']}: {r['title']}**{score_str}{status} — due {due_str}{urgency}")

    return "\n".join(lines)


def _get_document_content(db, title: str) -> str:
    pat = f"%{title.lower()}%"
    rows = db.conn.execute("""
        SELECT course, title, summary_json, local_path
        FROM documents
        WHERE processed_at IS NOT NULL AND summary_json IS NOT NULL
          AND (
            lower(title) LIKE ?
            OR lower(course || ' ' || title) LIKE ?
            OR lower(course || ': ' || title) LIKE ?
          )
        ORDER BY first_seen DESC
        LIMIT 3
    """, (pat, pat, pat)).fetchall()

    if not rows:
        return f"No document found matching '{title}'."

    results = []
    for r in rows:
        s = json.loads(r["summary_json"])
        takeaways = "\n".join(f"  - {t}" for t in s.get("key_takeaways", []))
        prereqs = ", ".join(s.get("prerequisites", [])) or "none"
        path_note = f"\nFile: {r['local_path']}" if r["local_path"] else ""
        results.append(
            f"**{r['course']}: {r['title']}**{path_note}\n"
            f"Summary: {s.get('summary', '')}\n"
            f"Prerequisites: {prereqs}\n"
            f"Key takeaways:\n{takeaways}"
        )
    return "\n\n".join(results)


def _read_pdf(local_path: str, pages: int = 10) -> str:
    from pathlib import Path
    p = Path(local_path)
    if not p.exists():
        return f"File not found: {local_path}"
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    try:
        with open(p, "rb") as f:
            uploaded = client.beta.files.upload(file=(p.name, f, "application/pdf"))
        try:
            response = client.beta.messages.create(
                model="claude-haiku-4-5",
                max_tokens=4096,
                betas=["files-api-2025-04-14"],
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"Extract and return the full text content of this PDF "
                                f"(first {pages} pages max). Preserve problem numbers, "
                                f"formulas, and structure. No commentary."
                            ),
                        },
                        {
                            "type": "document",
                            "source": {"type": "file", "file_id": uploaded.id},
                        },
                    ],
                }],
            )
            return response.content[0].text
        finally:
            client.beta.files.delete(uploaded.id)
    except Exception as e:
        return f"Error reading PDF: {e}"


def _dispatch_tool(db, name: str, inputs: dict) -> str:
    if name == "search_documents":
        return _search_documents(db, inputs["query"], inputs.get("limit", 5))
    if name == "get_deadlines":
        return _get_deadlines(db, inputs.get("days", 14))
    if name == "get_document_content":
        return _get_document_content(db, inputs["title"])
    if name == "read_pdf":
        return _read_pdf(inputs["local_path"], inputs.get("pages", 10))
    return f"Unknown tool: {name}"



def ask(db, question: str) -> str:
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    messages = [{"role": "user", "content": question}]

    for _ in range(8):
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "end_turn":
            return "\n".join(b.text for b in response.content if hasattr(b, "text"))
        if response.stop_reason != "tool_use":
            break
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            log.debug("Tool call: %s(%s)", block.name, block.input)
            result = _dispatch_tool(db, block.name, block.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })
        messages.append({"role": "user", "content": tool_results})

    return "Could not generate a response."


def ask_stream(db, question: str):
    """Generator yielding text chunks. Runs full tool-use loop then fake-streams the answer."""
    final_text = ask(db, question)
    # Yield in word-sized chunks so the UI still gets a streaming feel
    words = final_text.split(" ")
    for i, word in enumerate(words):
        yield word + (" " if i < len(words) - 1 else "")
