"""
TUMsolidator web UI.

    python app.py          # starts server on http://localhost:5050
"""

import html as _html
import json
import threading
import time
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, Response, request, stream_with_context

load_dotenv(override=True)

from intelligence.planner import _CSS, _deadlines_html, _materials_html, build_briefing
from intelligence.agent import ask_stream
from storage.db import Database

app = Flask(__name__)
db  = Database()

# ── Briefing cache ────────────────────────────────────────────────────────────

_cache_lock      = threading.Lock()
_cached_briefing = ""
_cache_ts        = 0.0
_generating      = False
_CACHE_TTL       = 6000  # seconds


def _get_briefing() -> str:
    """Return cached briefing immediately; kick off background regen if stale."""
    global _cached_briefing, _cache_ts, _generating
    with _cache_lock:
        fresh   = time.time() - _cache_ts < _CACHE_TTL
        current = _cached_briefing
        if fresh:
            return current
        if _generating:
            return current  # already regenerating, return stale

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
    return current  # return stale/empty immediately


def _warm_cache():
    """Prime the cache at startup (runs in background)."""
    _get_briefing()


# ── CSS ───────────────────────────────────────────────────────────────────────

_CHAT_CSS = """
#chat-wrap { max-width: 740px; margin: 0 auto 40px; }
#chat-log { display: flex; flex-direction: column; gap: 12px; margin-bottom: 16px; }
.msg { padding: 12px 16px; border-radius: 10px; font-size: 14px; line-height: 1.7; max-width: 90%; }
.msg.user { background: #0071e3; color: #fff; align-self: flex-end; border-bottom-right-radius: 3px; }
.msg.assistant { background: #fff; border: 1px solid #e5e5ea; align-self: flex-start;
  border-bottom-left-radius: 3px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
.msg.assistant h1,.msg.assistant h2,.msg.assistant h3 { font-size:13px;font-weight:600;margin:10px 0 4px; }
.msg.assistant p { margin: 5px 0; }
.msg.assistant ul,.msg.assistant ol { padding-left:18px; margin:5px 0; }
.msg.assistant li { margin: 3px 0; }
.msg.assistant strong { font-weight: 600; }
.msg.assistant code { background:#f2f2f7; padding:1px 5px; border-radius:4px; font-size:12px; }
.msg.assistant pre { background:#f2f2f7; padding:10px; border-radius:6px; overflow-x:auto; font-size:12px; }
.msg.thinking { color:#aeaeb2; font-style:italic; font-size:13px; align-self:flex-start;
  padding:8px 0; background:none; border:none; box-shadow:none; }
#chat-form { display:flex; gap:10px; }
#chat-input { flex:1; border:1px solid #d2d2d7; border-radius:10px; padding:10px 14px;
  font-size:14px; font-family:inherit; outline:none; resize:none; height:44px; line-height:1.4;
  transition: border-color .15s; }
#chat-input:focus { border-color: #0071e3; }
#chat-send { background:#0071e3; color:#fff; border:none; border-radius:10px; padding:0 20px;
  font-size:14px; font-weight:500; cursor:pointer; height:44px; transition:background .15s; }
#chat-send:hover { background:#0077ed; }
#chat-send:disabled { background:#aeaeb2; cursor:default; }
"""


def _build_page(briefing: str) -> str:
    if briefing:
        briefing_inner = f"<div class='briefing-text briefing-md' data-md='{_html.escape(briefing, quote=True)}'></div>"
    else:
        briefing_inner = "<p class='muted' id='brief-loading'>&#9203; Generating briefing&hellip; <script>setTimeout(()=>location.reload(),4000);</script></p>"
    briefing_card = (
        "<div class='card'>"
        "<h2>Today's Briefing</h2>"
        + briefing_inner +
        "</div>"
    )
    cards = _deadlines_html(db) + _materials_html(db) + briefing_card
    ts    = datetime.now().strftime("%a %d %b %Y, %H:%M")

    return (
        "<!doctype html><html><head>"
        "<meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>TUMsolidator</title>"
        "<script src='https://cdn.jsdelivr.net/npm/marked/marked.min.js'></script>"
        f"<style>{_CSS}{_CHAT_CSS}</style>"
        "</head><body>"
        + cards +
        """
<div id="chat-wrap">
  <div class="card" style="padding-bottom:20px">
    <h2>Ask your study assistant</h2>
    <div id="chat-log"></div>
    <form id="chat-form" onsubmit="return false">
      <textarea id="chat-input" placeholder="What do I need for GAD Blatt 5?" rows="1"
        onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendMsg();}"></textarea>
      <button id="chat-send" onclick="sendMsg()">Ask</button>
    </form>
  </div>
</div>
<script>
const log   = document.getElementById('chat-log');
const input = document.getElementById('chat-input');
const send  = document.getElementById('chat-send');

document.querySelectorAll('.briefing-md').forEach(el => {
  el.innerHTML = marked.parse(el.dataset.md);
});

function addMsg(role, html) {
  const d = document.createElement('div');
  d.className = 'msg ' + role;
  d.innerHTML = html;
  log.appendChild(d);
  d.scrollIntoView({behavior:'smooth', block:'end'});
  return d;
}

function sendMsg() {
  const q = input.value.trim();
  if (!q) return;
  addMsg('user', q.replace(/</g,'&lt;'));
  input.value = '';
  send.disabled = true;

  const thinking = addMsg('thinking', '&#9203; Searching your materials&hellip;');
  const bubble   = addMsg('assistant', '');
  let raw = '';

  const es = new EventSource('/ask?q=' + encodeURIComponent(q));
  es.onmessage = e => {
    if (e.data === '[DONE]') {
      es.close(); send.disabled = false;
      thinking.remove();
      bubble.innerHTML = marked.parse(raw);
      bubble.scrollIntoView({behavior:'smooth', block:'end'});
      return;
    }
    if (thinking.parentNode) thinking.remove();
    raw += JSON.parse(e.data);
    bubble.innerHTML = marked.parse(raw);
    bubble.scrollIntoView({behavior:'smooth', block:'end'});
  };
  es.onerror = () => {
    es.close(); send.disabled = false; thinking.remove();
    if (!raw) bubble.innerHTML = 'Error &mdash; check server logs.';
  };
}
</script>
"""
        + f"<p class='ts'>Updated {ts}</p>"
        "</body></html>"
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    briefing = _get_briefing()
    return Response(_build_page(briefing), mimetype="text/html")


@app.route("/refresh")
def refresh():
    global _cache_ts
    with _cache_lock:
        _cache_ts = 0.0
    briefing = _get_briefing()
    return Response(_build_page(briefing), mimetype="text/html")

# Browsers Block file:// paths from localhost
@app.route("/file")
def serve_file():
    from pathlib import Path
    from flask import send_file, abort
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
    _warm_cache()  # start briefing generation in background immediately
    url = "http://localhost:5050"
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"TUMsolidator → {url}")
    app.run(port=5050, debug=False, threaded=True)
