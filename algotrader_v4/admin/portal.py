#!/usr/bin/env python3
"""
AlgoTrader Pro v4 — Admin Portal

Web UI to manage all friend instances: create, start, stop, delete, view logs.

Run:
    python admin/portal.py                    # default port 8080
    python admin/portal.py --port 9090        # custom port
    PORTAL_PASSWORD=secret python admin/portal.py

Then open http://YOUR_SERVER:8080/ in your browser.
Default password: admin (change via PORTAL_PASSWORD env var)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets as _secrets

# ── Paths (same as provision.py) ──────────────────────────────────────────────
_HERE      = Path(__file__).resolve().parent
_APP_DIR   = _HERE.parent
sys.path.insert(0, str(_APP_DIR))   # allow importing provision helpers

from provision import (
    _load_registry, _save_registry, _is_running, _port_free, _health_check,
    _INSTANCES, _TEMPLATE,
    cmd_create as _cmd_create, cmd_stop as _cmd_stop,
)

# ── Auth ──────────────────────────────────────────────────────────────────────
_PORTAL_USER = os.environ.get("PORTAL_USER", "admin")
_PORTAL_PASS = os.environ.get("PORTAL_PASSWORD", "admin")

app = FastAPI(title="AlgoTrader Admin Portal", docs_url=None, redoc_url=None)
_security = HTTPBasic()

def _auth(creds: HTTPBasicCredentials = Depends(_security)):
    ok = (
        _secrets.compare_digest(creds.username.encode(), _PORTAL_USER.encode()) and
        _secrets.compare_digest(creds.password.encode(), _PORTAL_PASS.encode())
    )
    if not ok:
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return creds.username


# ── API routes ─────────────────────────────────────────────────────────────────

@app.get("/api/status")
def api_status(_: str = Depends(_auth)):
    reg = _load_registry()
    out = []
    for username, entry in sorted(reg.items()):
        pid    = entry.get("pid")
        port   = entry["port"]
        alive  = _is_running(pid)
        health: dict = {}
        if alive:
            try:
                import requests
                r = requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
                health = r.json()
            except Exception:
                health = {"error": "unreachable"}
        out.append({
            "user": username, "port": port, "pid": pid,
            "running": alive, "capital": entry.get("capital"),
            "created": entry.get("created_at", "")[:10],
            "mode": health.get("mode", "—"),
            "agents_up": sum(1 for v in health.get("agents", {}).values() if v),
        })
    return out


@app.post("/api/create")
async def api_create(req: Request, _: str = Depends(_auth)):
    body = await req.json()
    username = body.get("user", "").strip()
    port     = int(body.get("port", 0))
    capital  = float(body.get("capital", 100_000))

    if not username:
        raise HTTPException(400, "username required")
    if not port:
        raise HTTPException(400, "port required")

    reg = _load_registry()
    if username in reg:
        raise HTTPException(409, f"User '{username}' already exists")
    if not _port_free(port):
        raise HTTPException(409, f"Port {port} is already in use")

    # Reuse provision logic (capture printed output)
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _cmd_create(username, port, capital)

    output = buf.getvalue()
    # Extract password from output
    password = ""
    for line in output.splitlines():
        if "Password:" in line:
            password = line.split("Password:")[-1].strip()

    return {"ok": True, "user": username, "port": port, "password": password, "output": output}


@app.post("/api/{username}/start")
def api_start(username: str, _: str = Depends(_auth)):
    reg = _load_registry()
    if username not in reg:
        raise HTTPException(404, f"Unknown user '{username}'")

    entry    = reg[username]
    if _is_running(entry.get("pid")):
        return {"ok": True, "message": "already running", "pid": entry["pid"]}

    inst_dir = _INSTANCES / username
    env_file = inst_dir / ".env"
    if not env_file.exists():
        raise HTTPException(500, f".env missing for {username}")

    log_path = inst_dir / "logs" / "server.log"
    log_file = open(log_path, "a")

    env = {
        **os.environ,
        "APP_ENV_FILE": str(env_file),
        "KITE_ACCOUNTS_FILE": str(inst_dir / "kite_accounts.json"),
        "ADAPTIVE_DATA_DIR":  str(inst_dir / "logs" / "adaptive"),
        "DATABASE_PATH":      str(inst_dir / "logs" / "algotrader.db"),
    }

    port = entry["port"]
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app",
         "--host", "0.0.0.0", "--port", str(port), "--workers", "1"],
        cwd=str(_APP_DIR), env=env,
        stdout=log_file, stderr=log_file,
    )

    entry["pid"] = proc.pid
    _save_registry(reg)

    # Non-blocking: return PID immediately; UI polls /api/status
    return {"ok": True, "pid": proc.pid, "port": port}


@app.post("/api/{username}/stop")
def api_stop(username: str, _: str = Depends(_auth)):
    reg = _load_registry()
    if username not in reg:
        raise HTTPException(404, f"Unknown user '{username}'")
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        _cmd_stop(username)
    return {"ok": True}


@app.delete("/api/{username}")
async def api_delete(username: str, _: str = Depends(_auth)):
    reg = _load_registry()
    if username not in reg:
        raise HTTPException(404, f"Unknown user '{username}'")
    if _is_running(reg[username].get("pid")):
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            _cmd_stop(username)
    inst_dir = _INSTANCES / username
    if inst_dir.exists():
        import shutil
        shutil.rmtree(inst_dir)
    reg = _load_registry()
    del reg[username]
    _save_registry(reg)
    return {"ok": True}


@app.get("/api/{username}/logs")
def api_logs(username: str, lines: int = 100, _: str = Depends(_auth)):
    log = _INSTANCES / username / "logs" / "server.log"
    if not log.exists():
        return PlainTextResponse("No logs yet.")
    text = log.read_text(errors="replace")
    tail = "\n".join(text.splitlines()[-lines:])
    return PlainTextResponse(tail)


@app.get("/api/nginx")
def api_nginx(_: str = Depends(_auth)):
    reg = _load_registry()
    lines = ["# nginx config — add to /etc/nginx/sites-enabled/algotrader\n"]
    for username, entry in sorted(reg.items()):
        port = entry["port"]
        lines.append(f"# {username}")
        lines.append(f"server {{")
        lines.append(f"    listen 80;")
        lines.append(f"    server_name {username}.YOUR_DOMAIN;")
        lines.append(f"    location / {{")
        lines.append(f"        proxy_pass http://127.0.0.1:{port};")
        lines.append(f"        proxy_http_version 1.1;")
        lines.append(f"        proxy_set_header Upgrade $http_upgrade;")
        lines.append(f"        proxy_set_header Connection \"upgrade\";")
        lines.append(f"        proxy_set_header Host $host;")
        lines.append(f"    }}")
        lines.append(f"}}\n")
    return PlainTextResponse("\n".join(lines))


# ── HTML UI ───────────────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AlgoTrader Admin</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#0d1117;color:#c9d1d9;min-height:100vh}
header{background:#161b22;border-bottom:1px solid #30363d;padding:14px 24px;display:flex;align-items:center;gap:12px}
header h1{font-size:1.1rem;font-weight:600;color:#e6edf3}
header span{font-size:.8rem;color:#8b949e;background:#21262d;padding:2px 8px;border-radius:12px}
.wrap{max-width:1100px;margin:0 auto;padding:24px}
.toolbar{display:flex;gap:12px;margin-bottom:24px;align-items:center;flex-wrap:wrap}
.toolbar h2{font-size:.95rem;font-weight:600;color:#8b949e;flex:1}
button{cursor:pointer;border:none;border-radius:6px;padding:7px 14px;font-size:.85rem;font-weight:500;transition:opacity .15s}
button:hover{opacity:.85}
.btn-green{background:#238636;color:#fff}
.btn-red{background:#da3633;color:#fff}
.btn-grey{background:#21262d;color:#c9d1d9;border:1px solid #30363d}
.btn-blue{background:#1f6feb;color:#fff}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px}
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:18px}
.card-header{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.card-name{font-size:1rem;font-weight:600;color:#e6edf3}
.badge{font-size:.72rem;font-weight:600;padding:2px 8px;border-radius:10px}
.badge.running{background:#1a3a2a;color:#3fb950}
.badge.stopped{background:#2d2d2d;color:#8b949e}
.card-meta{font-size:.8rem;color:#8b949e;margin-bottom:14px;display:grid;grid-template-columns:1fr 1fr;gap:4px}
.card-meta strong{color:#c9d1d9}
.card-actions{display:flex;gap:8px;flex-wrap:wrap}
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:100;align-items:center;justify-content:center}
.modal-bg.open{display:flex}
.modal{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:24px;width:min(90vw,500px);max-height:80vh;overflow-y:auto}
.modal h3{margin-bottom:16px;font-size:1rem}
.form-row{margin-bottom:12px}
.form-row label{display:block;font-size:.8rem;color:#8b949e;margin-bottom:4px}
.form-row input{width:100%;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px 10px;color:#e6edf3;font-size:.9rem}
.form-row input:focus{outline:none;border-color:#1f6feb}
.form-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}
pre{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:12px;font-size:.75rem;overflow:auto;max-height:400px;white-space:pre-wrap;word-break:break-all}
.toast{position:fixed;bottom:20px;right:20px;background:#238636;color:#fff;padding:10px 18px;border-radius:8px;font-size:.85rem;display:none;z-index:200}
.toast.err{background:#da3633}
#cred-box{background:#0d1117;border:1px solid #3fb950;border-radius:8px;padding:14px;margin-top:12px;font-size:.83rem}
#cred-box strong{color:#3fb950}
</style>
</head>
<body>
<header>
  <h1>⚡ AlgoTrader Admin</h1>
  <span id="hdr-count">0 users</span>
  <span id="hdr-running" style="background:#1a3a2a;color:#3fb950">0 running</span>
</header>
<div class="wrap">
  <div class="toolbar">
    <h2>Friend Instances</h2>
    <button class="btn-grey" onclick="showNginx()">nginx config</button>
    <button class="btn-green" onclick="openCreate()">+ Add Friend</button>
  </div>
  <div class="grid" id="grid">
    <p style="color:#8b949e;font-size:.9rem">Loading...</p>
  </div>
</div>

<!-- Create modal -->
<div class="modal-bg" id="create-modal">
  <div class="modal">
    <h3>Add Friend Instance</h3>
    <div class="form-row"><label>Username</label><input id="f-user" placeholder="e.g. alice" autocomplete="off"></div>
    <div class="form-row"><label>Port</label><input id="f-port" type="number" placeholder="8001"></div>
    <div class="form-row"><label>Starting Capital (₹)</label><input id="f-cap" type="number" value="100000"></div>
    <div id="cred-box" style="display:none"></div>
    <div class="form-actions">
      <button class="btn-grey" onclick="closeCreate()">Cancel</button>
      <button class="btn-green" id="create-btn" onclick="doCreate()">Create</button>
    </div>
  </div>
</div>

<!-- Log modal -->
<div class="modal-bg" id="log-modal">
  <div class="modal" style="width:min(95vw,760px)">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">
      <h3 id="log-title">Logs</h3>
      <span style="flex:1"></span>
      <button class="btn-grey" onclick="closeLogs()">✕</button>
    </div>
    <pre id="log-pre">Loading...</pre>
  </div>
</div>

<!-- nginx modal -->
<div class="modal-bg" id="nginx-modal">
  <div class="modal" style="width:min(95vw,680px)">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">
      <h3>nginx Reverse-Proxy Config</h3>
      <span style="flex:1"></span>
      <button class="btn-grey" onclick="document.getElementById('nginx-modal').classList.remove('open')">✕</button>
    </div>
    <pre id="nginx-pre">Loading...</pre>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let _logUser = null, _logTimer = null;

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body) { opts.body = JSON.stringify(body); opts.headers['Content-Type'] = 'application/json'; }
  const r = await fetch(path, opts);
  if (!r.ok) { const t = await r.text(); throw new Error(t); }
  const ct = r.headers.get('content-type') || '';
  return ct.includes('json') ? r.json() : r.text();
}

function toast(msg, err=false) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.className = 'toast' + (err ? ' err' : '');
  t.style.display = 'block';
  setTimeout(() => t.style.display = 'none', 3500);
}

async function refresh() {
  try {
    const users = await api('GET', '/api/status');
    const grid = document.getElementById('grid');
    document.getElementById('hdr-count').textContent = users.length + ' user' + (users.length !== 1 ? 's' : '');
    document.getElementById('hdr-running').textContent = users.filter(u=>u.running).length + ' running';
    if (!users.length) {
      grid.innerHTML = '<p style="color:#8b949e;font-size:.9rem">No friends yet. Click &ldquo;+ Add Friend&rdquo; to get started.</p>';
      return;
    }
    grid.innerHTML = users.map(u => `
      <div class="card">
        <div class="card-header">
          <span class="card-name">${u.user}</span>
          <span class="badge ${u.running ? 'running' : 'stopped'}">${u.running ? '● running' : '○ stopped'}</span>
        </div>
        <div class="card-meta">
          <div>Port <strong>:${u.port}</strong></div>
          <div>Capital <strong>₹${Number(u.capital||0).toLocaleString('en-IN')}</strong></div>
          <div>Mode <strong>${u.mode}</strong></div>
          <div>Agents active <strong>${u.agents_up}</strong></div>
          <div>Created <strong>${u.created}</strong></div>
          <div><a href="http://${location.hostname}:${u.port}/dashboard" target="_blank" style="color:#58a6ff;font-size:.8rem">Open dashboard ↗</a></div>
        </div>
        <div class="card-actions">
          ${u.running
            ? `<button class="btn-grey" onclick="stopUser('${u.user}')">■ Stop</button>`
            : `<button class="btn-green" onclick="startUser('${u.user}')">▶ Start</button>`}
          <button class="btn-grey" onclick="viewLogs('${u.user}')">Logs</button>
          <button class="btn-red" style="margin-left:auto" onclick="deleteUser('${u.user}')">Delete</button>
        </div>
      </div>
    `).join('');
  } catch(e) { console.error(e); }
}

async function startUser(user) {
  try {
    toast(`Starting ${user}...`);
    await api('POST', `/api/${user}/start`);
    setTimeout(refresh, 2000);
    toast(`${user} starting — dashboard link will appear in ~15s`);
  } catch(e) { toast(e.message, true); }
}

async function stopUser(user) {
  if (!confirm(`Stop ${user}?`)) return;
  try {
    await api('POST', `/api/${user}/stop`);
    toast(`${user} stopped`);
    setTimeout(refresh, 800);
  } catch(e) { toast(e.message, true); }
}

async function deleteUser(user) {
  if (!confirm(`Permanently delete ALL data for "${user}"? This cannot be undone.`)) return;
  try {
    await api('DELETE', `/api/${user}`);
    toast(`${user} deleted`);
    refresh();
  } catch(e) { toast(e.message, true); }
}

function openCreate() {
  document.getElementById('cred-box').style.display = 'none';
  document.getElementById('cred-box').innerHTML = '';
  document.getElementById('f-user').value = '';
  document.getElementById('f-cap').value = '100000';
  // suggest next available port
  api('GET', '/api/status').then(users => {
    const ports = users.map(u => u.port);
    let p = 8001; while (ports.includes(p)) p++;
    document.getElementById('f-port').value = p;
  }).catch(() => {});
  document.getElementById('create-modal').classList.add('open');
  setTimeout(() => document.getElementById('f-user').focus(), 100);
}

function closeCreate() {
  document.getElementById('create-modal').classList.remove('open');
}

async function doCreate() {
  const user = document.getElementById('f-user').value.trim();
  const port = parseInt(document.getElementById('f-port').value);
  const cap  = parseFloat(document.getElementById('f-cap').value);
  if (!user || !port) { toast('Username and port are required', true); return; }
  const btn = document.getElementById('create-btn');
  btn.disabled = true; btn.textContent = 'Creating...';
  try {
    const res = await api('POST', '/api/create', { user, port, capital: cap });
    const box = document.getElementById('cred-box');
    box.style.display = 'block';
    box.innerHTML = `
      <strong>✅ ${user} created!</strong><br><br>
      <b>URL:</b> <a href="http://${location.hostname}:${port}/login" target="_blank" style="color:#58a6ff">
        http://${location.hostname}:${port}/login</a><br>
      <b>Username:</b> ${user}<br>
      <b>Password:</b> <code style="background:#1a1a2e;padding:2px 6px;border-radius:4px">${res.password}</code><br><br>
      <span style="color:#8b949e;font-size:.8rem">Share the URL and password with ${user}.
      They fill in their Zerodha API key in their dashboard Settings.</span>`;
    btn.textContent = 'Created ✓';
    refresh();
  } catch(e) {
    toast(e.message, true);
    btn.disabled = false; btn.textContent = 'Create';
  }
}

function viewLogs(user) {
  _logUser = user;
  document.getElementById('log-title').textContent = user + ' — Server Log';
  document.getElementById('log-modal').classList.add('open');
  fetchLogs();
  _logTimer = setInterval(fetchLogs, 3000);
}

async function fetchLogs() {
  if (!_logUser) return;
  try {
    const t = await api('GET', `/api/${_logUser}/logs?lines=150`);
    const pre = document.getElementById('log-pre');
    pre.textContent = t;
    pre.scrollTop = pre.scrollHeight;
  } catch(e) {}
}

function closeLogs() {
  clearInterval(_logTimer); _logUser = null;
  document.getElementById('log-modal').classList.remove('open');
}

async function showNginx() {
  document.getElementById('nginx-modal').classList.add('open');
  document.getElementById('nginx-pre').textContent = 'Loading...';
  const t = await api('GET', '/api/nginx');
  document.getElementById('nginx-pre').textContent = t;
}

// Auto-refresh every 15s
refresh();
setInterval(refresh, 15000);

// Close modals on backdrop click
document.querySelectorAll('.modal-bg').forEach(m => {
  m.addEventListener('click', e => { if (e.target === m) { closeLogs(); m.classList.remove('open'); }});
});
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def ui(_: str = Depends(_auth)):
    return HTMLResponse(_HTML)


# ── CLI entry ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="AlgoTrader Admin Portal")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--host", default="0.0.0.0")
    args = p.parse_args()

    user = os.environ.get("PORTAL_USER", "admin")
    pw   = os.environ.get("PORTAL_PASSWORD", "admin")
    if pw == "admin":
        print("⚠️  Using default password 'admin'. Set PORTAL_PASSWORD=yourpassword to secure it.")

    print(f"✅  Admin portal starting on http://0.0.0.0:{args.port}/")
    print(f"    Login: {user} / {pw}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
