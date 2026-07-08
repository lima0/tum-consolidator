"""
TUMsolidator web UI.

    python app.py          # starts server on http://localhost:5050
"""

import html as _html
import json
import threading
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, render_template, request, send_file, abort, stream_with_context

load_dotenv(override=True)

from intelligence.planner import _deadlines_html, _materials_html, build_briefing
from intelligence.agent import ask_stream
from storage.db import Database

app = Flask(__name__)
db  = Database()

# ── Briefing cache ────────────────────────────────────────────────────────────

_cache_lock      = threading.Lock()
_cached_briefing = ""
_cache_ts        = 0.0
_generating      = False
_CACHE_TTL       = 6000


def _get_briefing() -> str:
    global _cached_briefing, _cache_ts, _generating
    with _cache_lock:
        if time.time() - _cache_ts < _CACHE_TTL:
            return _cached_briefing
        if _generating:
            return _cached_briefing

    def _regen():
        global _cached_briefing, _cache_ts, _generating
        try:
            result = build_briefing(db)
            with _cache_lock:
                _cached_briefing = result
                _cache_ts        = time.time()
        finally:
            with _cache_lock:
                _generating = False

    with _cache_lock:
        _generating = True
    threading.Thread(target=_regen, daemon=True).start()
    return _cached_briefing


def _build_cards(briefing: str) -> str:
    if briefing:
        briefing_inner = f"<div class='briefing-text briefing-md' data-md='{_html.escape(briefing, quote=True)}'></div>"
    else:
        briefing_inner = "<p class='muted'>&#9203; Generating briefing&hellip;<script>setTimeout(()=>location.reload(),4000);</script></p>"
    briefing_card = "<div class='card'><h2>Today's Briefing</h2>" + briefing_inner + "</div>"
    return _deadlines_html(db) + _materials_html(db) + briefing_card


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    cards = _build_cards(_get_briefing())
    ts    = datetime.now().strftime("%a %d %b %Y, %H:%M")
    return render_template("index.html", cards=cards, ts=ts)


@app.route("/refresh")
def refresh():
    global _cache_ts
    with _cache_lock:
        _cache_ts = 0.0
    cards = _build_cards(_get_briefing())
    ts    = datetime.now().strftime("%a %d %b %Y, %H:%M")
    return render_template("index.html", cards=cards, ts=ts)


@app.route("/file")
def serve_file():
    rel = request.args.get("path", "")
    if not rel:
        abort(400)
    root = Path(__file__).parent.resolve()
    path = (root / rel).resolve()
    if not str(path).startswith(str(root)):
        abort(403)
    if not path.exists():
        abort(404)
    return send_file(path)


@app.route("/deadlines.ics")
def deadlines_ics():
    from ics_export import build_ics
    return Response(
        build_ics(db),
        mimetype="text/calendar",
        headers={"Content-Disposition": "inline; filename=deadlines.ics"},
    )


@app.route("/ask")
def ask_endpoint():
    question = request.args.get("q", "").strip()
    if not question:
        return Response("data: [DONE]\n\n", mimetype="text/event-stream")

    def generate():
        try:
            for chunk in ask_stream(db, question):
                yield f"data: {json.dumps(chunk)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps(f'Error: {e}')}\n\n"
        yield "data: [DONE]\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import webbrowser
    _get_briefing()  # warm cache in background
    url = "http://localhost:5050"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"TUMsolidator → {url}")
    app.run(port=5050, debug=False, threaded=True)
