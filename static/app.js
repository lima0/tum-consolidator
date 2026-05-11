document.querySelectorAll('.briefing-md').forEach(el => {
  el.innerHTML = marked.parse(el.dataset.md);
});

const log   = document.getElementById('chat-log');
const input = document.getElementById('chat-input');
const send  = document.getElementById('chat-send');

function addMsg(role, html) {
  const d = document.createElement('div');
  d.className = 'msg ' + role;
  d.innerHTML = html;
  log.appendChild(d);
  d.scrollIntoView({ behavior: 'smooth', block: 'end' });
  return d;
}

function sendMsg() {
  const q = input.value.trim();
  if (!q) return;
  addMsg('user', q.replace(/</g, '&lt;'));
  input.value = '';
  send.disabled = true;

  const thinking = addMsg('thinking', '&#9203; Searching your materials&hellip;');
  const bubble   = addMsg('assistant', '');
  let raw = '';

  const es = new EventSource('/ask?q=' + encodeURIComponent(q));
  es.onmessage = e => {
    if (e.data === '[DONE]') {
      es.close();
      send.disabled = false;
      thinking.remove();
      bubble.innerHTML = marked.parse(raw);
      bubble.scrollIntoView({ behavior: 'smooth', block: 'end' });
      return;
    }
    if (thinking.parentNode) thinking.remove();
    raw += JSON.parse(e.data);
    bubble.innerHTML = marked.parse(raw);
    bubble.scrollIntoView({ behavior: 'smooth', block: 'end' });
  };
  es.onerror = () => {
    es.close();
    send.disabled = false;
    thinking.remove();
    if (!raw) bubble.innerHTML = 'Error &mdash; check server logs.';
  };
}
