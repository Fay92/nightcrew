"""Local web viewer for nightcrew: list tasks, read logs, delete.

A zero-dependency, read-mostly UI. ``nightcrew web`` starts a stdlib HTTP
server bound to the loopback interface, opens the browser, and serves:

- a task list (auto-refreshing),
- a readable rendering of any task's stream-json log,
- delete (guarded by a per-session token plus a confirm dialog).

Security posture for a tool that exposes a delete: nothing listens beyond
127.0.0.1; every request's Host header must be loopback (blocks DNS-rebinding
from a page you happen to visit); and the only mutating endpoint requires a
token minted into the page at startup (a cross-origin page can neither read
that token nor forge the header). Reads carry no token -- they expose only
your own queue, locally.
"""

from __future__ import annotations

import json
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import runner
from .config import Config
from .queue import AmbiguousTaskId, StaleTask, TaskNotFound, TaskQueue

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
_TOKEN_PLACEHOLDER = "__NIGHTCREW_TOKEN__"


# ---------------------------------------------------------------------------
# Data: task list + readable log rendering (pure, unit-tested directly)
# ---------------------------------------------------------------------------


def tasks_payload(queue: TaskQueue) -> list[dict]:
    """Serialise the queue for the UI; timestamps stay ISO (the browser
    localises them, so the server needs no timezone logic)."""
    return [
        {
            "id": t.id,
            "status": t.status,
            "created_at": t.created_at,
            "repo": t.repo,
            "prompt": t.prompt,
        }
        for t in queue.all()
    ]


def _tool_input_summary(inp) -> str:
    if not isinstance(inp, dict):
        return str(inp)[:300]
    for key in ("command", "file_path", "path", "pattern", "query", "url"):
        value = inp.get(key)
        if isinstance(value, str):
            return value[:500]
    try:
        return json.dumps(inp, ensure_ascii=False)[:300]
    except (TypeError, ValueError):
        return str(inp)[:300]


def _extract_result_content(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts).strip()
    return ""


def _render_assistant(ev: dict) -> list[dict]:
    content = (ev.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"role": "claude", "text": content[:8000]}] if content.strip() else []
    if not isinstance(content, list):
        return []
    out: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            txt = str(block.get("text", "")).strip()
            if txt:
                out.append({"role": "claude", "text": txt[:8000]})
        elif block.get("type") == "tool_use":
            name = block.get("name", "tool")
            out.append({"role": "tool", "text": f"{name}  {_tool_input_summary(block.get('input'))}"})
    return out


def _render_tool_result(ev: dict) -> list[dict]:
    content = (ev.get("message") or {}).get("content")
    blocks = content if isinstance(content, list) else [content]
    out: list[dict] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            text = _extract_result_content(block.get("content"))
            if text:
                role = "tool_result_err" if block.get("is_error") else "tool_result"
                out.append({"role": role, "text": text[:1500]})
    return out


def _render_result(ev: dict) -> list[dict]:
    subtype = str(ev.get("subtype", ""))
    head = f"result ({subtype})" if subtype else "result"
    meta = []
    if ev.get("num_turns") is not None:
        meta.append(f"{ev['num_turns']} turns")
    try:
        if ev.get("total_cost_usd") is not None:
            meta.append(f"${float(ev['total_cost_usd']):.4f}")
    except (TypeError, ValueError):
        pass
    try:
        if ev.get("duration_ms") is not None:
            meta.append(f"{int(ev['duration_ms']) // 1000}s")
    except (TypeError, ValueError):
        pass
    if meta:
        head += "  ·  " + ", ".join(meta)
    out = [{"role": "result", "text": head}]
    text = ev.get("result")
    if isinstance(text, str) and text.strip():
        out.append({"role": "result_text", "text": text.strip()[:4000]})
    return out


def _render_event(ev: dict) -> list[dict]:
    kind = ev.get("type", "")
    if kind == "nightcrew.run":
        return [{"role": "meta", "text": "nightcrew started "
                 + ("(resume)" if ev.get("resume") else "(fresh run)")}]
    if kind == "nightcrew.stderr":
        return [{"role": "stderr", "text": "stderr: " + str(ev.get("text", ""))[:1500]}]
    if kind == "nightcrew.outcome":
        bits = [f"status={ev.get('status')}"]
        if ev.get("detail"):
            bits.append(str(ev["detail"]))
        return [{"role": "outcome", "text": "outcome: " + " — ".join(bits)}]
    if kind == "assistant":
        return _render_assistant(ev)
    if kind == "user":
        return _render_tool_result(ev)
    if kind == "result":
        return _render_result(ev)
    return []  # 'system' init and anything unknown: stay quiet


def log_entries(path: Path) -> list[dict]:
    """Parse a task's ``.jsonl`` into readable ``{role, text}`` entries."""
    if not path.exists():
        return []
    entries: list[dict] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            entries.append({"role": "raw", "text": line[:2000]})
            continue
        if isinstance(ev, dict):
            entries.extend(_render_event(ev))
    return entries


def delete_task(config: Config, task_id: str) -> tuple[bool, str]:
    """Remove a task by id/prefix. Running tasks are protected (no force),
    so a stray click can't kill an in-flight run. Returns (ok, detail)."""
    queue = TaskQueue(config.home)
    try:
        task = queue.remove(task_id, force=False)
    except (TaskNotFound, AmbiguousTaskId, StaleTask) as exc:
        return False, str(exc)
    return True, task.status


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "nightcrew"

    # Quiet by default; the daemon's own log is elsewhere and this runs in the
    # user's foreground terminal.
    def log_message(self, *args) -> None:  # noqa: D401
        pass

    @property
    def _config(self) -> Config:
        return self.server.nc_config  # type: ignore[attr-defined]

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in _LOOPBACK_HOSTS

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def _segments(self) -> list[str]:
        return urlparse(self.path).path.strip("/").split("/")

    def do_GET(self) -> None:
        if not self._host_ok():
            self._json({"error": "non-loopback host rejected"}, 403)
            return
        seg = self._segments()
        if seg in ([""], ["index.html"]):
            html = _PAGE.replace(_TOKEN_PLACEHOLDER, self.server.nc_token)  # type: ignore[attr-defined]
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        elif seg == ["api", "tasks"]:
            self._json({"tasks": tasks_payload(TaskQueue(self._config.home))})
        elif len(seg) == 4 and seg[:2] == ["api", "tasks"] and seg[3] == "log":
            path = self._config.logs_dir / f"{seg[2]}.jsonl"
            self._json({"id": seg[2], "entries": log_entries(path)})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if not self._host_ok():
            self._json({"error": "non-loopback host rejected"}, 403)
            return
        seg = self._segments()
        if len(seg) == 4 and seg[:2] == ["api", "tasks"] and seg[3] == "delete":
            token = self.headers.get("X-Nightcrew-Token", "")
            expected = self.server.nc_token  # type: ignore[attr-defined]
            if not secrets.compare_digest(token, expected):
                self._json({"ok": False, "error": "bad or missing token"}, 403)
                return
            ok, detail = delete_task(self._config, seg[2])
            self._json({"ok": ok, "detail": detail}, 200 if ok else 409)
        else:
            self._json({"error": "not found"}, 404)


def make_server(config: Config, *, host: str = "127.0.0.1", port: int = 8787):
    """Build (httpd, token, url). Falls back to a free port if *port* is busy."""
    config.ensure_dirs()
    token = secrets.token_urlsafe(24)
    try:
        httpd = ThreadingHTTPServer((host, port), _Handler)
    except OSError:
        if port == 0:
            raise
        print(f"nightcrew: port {port} is busy, picking a free one", file=sys.stderr)
        httpd = ThreadingHTTPServer((host, 0), _Handler)
    httpd.nc_config = config  # type: ignore[attr-defined]
    httpd.nc_token = token    # type: ignore[attr-defined]
    actual = httpd.server_address[1]
    return httpd, token, f"http://{host}:{actual}/"


def serve(config: Config, *, port: int = 8787, open_browser: bool = True) -> int:
    httpd, _token, url = make_server(config, port=port)
    print(f"nightcrew web: serving at {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nnightcrew web: stopped")
    finally:
        httpd.server_close()
    return 0


# ---------------------------------------------------------------------------
# Single-page UI (one inline document, no build step, no external assets)
# ---------------------------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>nightcrew</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--fg:#e6e9ef;--mut:#8b93a7;--accent:#6ea8fe}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--fg)}
  header{display:flex;align-items:center;gap:12px;padding:12px 18px;border-bottom:1px solid var(--line);background:var(--panel)}
  header .logo{font-weight:600;letter-spacing:.3px}
  header .count{color:var(--mut);font-size:13px}
  header button{margin-left:auto;background:#222836;color:var(--fg);border:1px solid var(--line);border-radius:7px;padding:5px 12px;cursor:pointer}
  header button:hover{border-color:var(--accent)}
  main{display:grid;grid-template-columns:minmax(300px,38%) 1fr;height:calc(100vh - 53px)}
  #list{overflow:auto;border-right:1px solid var(--line)}
  .row{padding:11px 16px;border-bottom:1px solid var(--line);cursor:pointer;display:flex;flex-direction:column;gap:4px}
  .row:hover{background:#1b1f28}
  .row.sel{background:#1d2535;box-shadow:inset 3px 0 0 var(--accent)}
  .row .top{display:flex;align-items:center;gap:8px}
  .row .prompt{font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .row .meta{color:var(--mut);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .badge{font-size:11px;padding:1px 8px;border-radius:20px;text-transform:uppercase;letter-spacing:.4px;font-weight:600;flex:none}
  .badge.done{background:#10331f;color:#5fd08a}
  .badge.failed{background:#3a1b1f;color:#f08a8a}
  .badge.running{background:#152a44;color:#6ea8fe}
  .badge.pending{background:#23262e;color:#9aa3b5}
  .badge.blocked_limit{background:#3a2c13;color:#e0a44c}
  .badge.retry{background:#33310f;color:#d9d05a}
  #detail{display:flex;flex-direction:column;min-width:0}
  #detailhead{display:flex;align-items:center;gap:12px;padding:11px 16px;border-bottom:1px solid var(--line);background:var(--panel);min-height:46px}
  #detailhead .title{font-weight:600}
  #detailhead .del{margin-left:auto;background:#3a1b1f;color:#f0a3a3;border:1px solid #5a2a30;border-radius:7px;padding:5px 12px;cursor:pointer}
  #detailhead .del:hover{background:#52232a}
  #log{overflow:auto;padding:14px 16px;flex:1}
  .entry{white-space:pre-wrap;word-break:break-word;border-radius:8px;padding:9px 12px;margin-bottom:9px;border:1px solid transparent}
  .entry.claude{background:#161b24;border-color:#23304a}
  .entry.tool{background:#14181f;color:#9ec1ff;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
  .entry.tool_result{background:#11151b;color:#aeb6c6;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
  .entry.tool_result_err{background:#1f1416;color:#f0a3a3;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
  .entry.result{background:#10241a;color:#7fdca0;font-weight:600}
  .entry.result_text{background:#10241a;color:#cfe9d8}
  .entry.outcome{background:#1c1f27;color:#cdd4e3;font-weight:600}
  .entry.meta{color:var(--mut);font-size:12.5px;padding:3px 12px;border:none}
  .entry.stderr{background:#1f1416;color:#e6a0a0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
  .entry.raw{color:var(--mut);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
  .empty{color:var(--mut);padding:30px 16px;text-align:center}
</style>
</head>
<body>
<header>
  <span class="logo">🌙 nightcrew</span>
  <span class="count" id="count"></span>
  <button id="refresh" title="refresh">↻ refresh</button>
</header>
<main>
  <section id="list"></section>
  <section id="detail">
    <div id="detailhead"></div>
    <div id="log"><div class="empty">select a task to view its log</div></div>
  </section>
</main>
<script>
const TOKEN = "__NIGHTCREW_TOKEN__";
let selected = null;

function el(tag, cls, text){ const e=document.createElement(tag); if(cls)e.className=cls; if(text!=null)e.textContent=text; return e; }

async function loadTasks(){
  let d; try{ d = await (await fetch('/api/tasks')).json(); }catch(e){ return; }
  const list = document.getElementById('list');
  list.innerHTML = '';
  document.getElementById('count').textContent = d.tasks.length + (d.tasks.length===1?' task':' tasks');
  if(!d.tasks.length){ list.appendChild(el('div','empty','queue is empty')); }
  for(const t of d.tasks){
    const row = el('div', 'row' + (t.id===selected?' sel':''));
    row.dataset.id = t.id;
    row.onclick = () => selectTask(t.id);
    const top = el('div','top');
    top.appendChild(el('span','badge '+t.status, t.status));
    top.appendChild(el('span','prompt', t.prompt));
    row.appendChild(top);
    const when = t.created_at ? new Date(t.created_at).toLocaleString() : '';
    row.appendChild(el('div','meta', when + '  ·  ' + t.repo));
    list.appendChild(row);
  }
}

function highlight(id){
  document.querySelectorAll('.row').forEach(r => r.classList.toggle('sel', r.dataset.id===id));
}

async function selectTask(id){
  selected = id; highlight(id);
  const head = document.getElementById('detailhead');
  head.innerHTML = '';
  head.appendChild(el('span','title','log · ' + id));
  const del = el('button','del','🗑 delete');
  del.onclick = () => delTask(id);
  head.appendChild(del);
  const log = document.getElementById('log');
  log.innerHTML = '<div class="empty">loading…</div>';
  let d; try{ d = await (await fetch('/api/tasks/'+encodeURIComponent(id)+'/log')).json(); }
  catch(e){ log.innerHTML = '<div class="empty">failed to load log</div>'; return; }
  if(selected !== id) return;            // user moved on while we fetched
  log.innerHTML = '';
  if(!d.entries.length){ log.appendChild(el('div','empty','no log yet for this task')); return; }
  for(const entry of d.entries){ log.appendChild(el('div','entry '+entry.role, entry.text)); }
}

async function delTask(id){
  if(!confirm('Delete task ' + id + '?\\nThis removes it from the queue (logs stay on disk).')) return;
  let d; try{
    d = await (await fetch('/api/tasks/'+encodeURIComponent(id)+'/delete', {
      method:'POST', headers:{'X-Nightcrew-Token': TOKEN}
    })).json();
  }catch(e){ alert('delete failed'); return; }
  if(!d.ok){ alert('delete failed: ' + (d.error || d.detail || 'unknown')); return; }
  selected = null;
  document.getElementById('detailhead').innerHTML = '';
  document.getElementById('log').innerHTML = '<div class="empty">select a task to view its log</div>';
  loadTasks();
}

document.getElementById('refresh').onclick = loadTasks;
loadTasks();
setInterval(loadTasks, 8000);   // keep statuses live while you watch
</script>
</body>
</html>
"""
