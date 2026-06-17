#!/usr/bin/env python3
"""
AlgoTrader Pro v4 — Multi-User Provisioning Tool

Each friend gets an isolated AlgoTrader instance: their own port, their own
Zerodha credentials, their own database, and their own dashboard password.
Zero changes to the production trading code are required.

Prerequisites (all already in requirements.txt):
  pip install passlib[bcrypt] requests

Usage:
  python admin/provision.py create --user alice --port 8001
  python admin/provision.py create --user bob   --port 8002 --capital 200000
  python admin/provision.py list
  python admin/provision.py start  alice
  python admin/provision.py stop   alice
  python admin/provision.py logs   alice        # live tail of alice's server log
  python admin/provision.py status alice        # JSON health + process info
  python admin/provision.py delete alice        # stop + remove all data
  python admin/provision.py nginx               # print nginx reverse-proxy config

After creating a user:
  1. Share their URL (http://SERVER:PORT/login) and password with them.
  2. They log in, go to Settings → fill in their Kite API key + secret.
  3. They click "Connect Kite Account" to do the Zerodha OAuth flow.
  4. They configure their capital and risk limits in Settings.
  5. They click "Start Bot" — their agents trade their own Zerodha account.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import warnings
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from passlib.context import CryptContext

try:
    import requests as _requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

# ── Paths ─────────────────────────────────────────────────────────────────────

_HERE      = Path(__file__).resolve().parent          # admin/
_APP_DIR   = _HERE.parent                             # algotrader_v4/
_INSTANCES = _HERE / "instances"
_REGISTRY  = _HERE / "registry.json"
_TEMPLATE  = _HERE / "user_env.template"

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ── Registry helpers ──────────────────────────────────────────────────────────

def _load_registry() -> dict:
    if _REGISTRY.exists():
        return json.loads(_REGISTRY.read_text())
    return {}


def _save_registry(reg: dict) -> None:
    _REGISTRY.write_text(json.dumps(reg, indent=2, default=str))


# ── Process helpers ───────────────────────────────────────────────────────────

def _is_running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _port_free(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _health_check(port: int, timeout: float = 30.0) -> bool:
    if not _REQUESTS_OK:
        time.sleep(3)
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = _requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_create(username: str, port: int, capital: float) -> None:
    reg = _load_registry()

    if username in reg:
        sys.exit(f"❌  User '{username}' already exists. Use 'start' to restart it.")
    if not _port_free(port):
        sys.exit(f"❌  Port {port} is already in use. Choose a different port.")

    # Instance directory
    inst_dir = _INSTANCES / username
    log_dir  = inst_dir / "logs"
    adap_dir = log_dir / "adaptive"
    for d in (inst_dir, log_dir, adap_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Credentials
    password      = secrets.token_urlsafe(12)
    password_hash = _pwd.hash(password)
    jwt_secret    = secrets.token_urlsafe(32)

    # Write .env from template
    template = _TEMPLATE.read_text()
    env_content = template.format(
        username=username,
        created_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        port=port,
        password_hash=password_hash,
        jwt_secret=jwt_secret,
        capital=int(capital),
        db_path=str(log_dir / "algotrader.db"),
        kite_accounts_file=str(inst_dir / "kite_accounts.json"),
        adaptive_data_dir=str(adap_dir),
    )
    env_file = inst_dir / ".env"
    env_file.write_text(env_content)
    env_file.chmod(0o600)

    # Register
    reg[username] = {
        "port": port,
        "pid": None,
        "capital": capital,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_registry(reg)

    print(f"✅  Created user: {username}")
    print(f"    Port    : {port}")
    print(f"    Password: {password}")
    print(f"    URL     : http://YOUR_SERVER:{port}/login")
    print()
    print(f"    Share the URL and password above with {username}.")
    print(f"    They fill in their Zerodha Kite API key in their dashboard Settings.")
    print(f"    Run: python admin/provision.py start {username}")


def cmd_start(username: str) -> None:
    reg = _load_registry()
    if username not in reg:
        sys.exit(f"❌  Unknown user '{username}'. Run 'create' first.")

    entry = reg[username]
    if _is_running(entry.get("pid")):
        print(f"ℹ️   {username} is already running (PID {entry['pid']}, port {entry['port']}).")
        return

    inst_dir = _INSTANCES / username
    log_file = open(inst_dir / "logs" / "server.log", "a")
    env_file = inst_dir / ".env"

    if not env_file.exists():
        sys.exit(f"❌  {env_file} not found. Run 'create' first.")

    env = {
        **os.environ,
        "APP_ENV_FILE": str(env_file),
        "KITE_ACCOUNTS_FILE": str(inst_dir / "kite_accounts.json"),
        "ADAPTIVE_DATA_DIR": str(inst_dir / "logs" / "adaptive"),
        "DATABASE_PATH": str(inst_dir / "logs" / "algotrader.db"),
    }

    port = entry["port"]
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app",
         "--host", "0.0.0.0", "--port", str(port), "--workers", "1"],
        cwd=str(_APP_DIR),
        env=env,
        stdout=log_file,
        stderr=log_file,
    )
    log_file.close()  # child inherited the fd; parent's copy can be freed

    print(f"⏳  Starting {username} on port {port} (PID {proc.pid}) ...")
    if _health_check(port):
        entry["pid"] = proc.pid
        _save_registry(reg)
        print(f"✅  {username} is ready → http://YOUR_SERVER:{port}/login")
    else:
        proc.terminate()
        sys.exit(f"❌  {username} did not become healthy within 30s. "
                 f"Check logs: admin/instances/{username}/logs/server.log")


def cmd_stop(username: str) -> None:
    reg = _load_registry()
    if username not in reg:
        sys.exit(f"❌  Unknown user '{username}'.")

    entry = reg[username]
    pid   = entry.get("pid")

    if not _is_running(pid):
        entry["pid"] = None
        _save_registry(reg)
        print(f"ℹ️   {username} is not running.")
        return

    os.kill(pid, signal.SIGTERM)
    for _ in range(15):
        if not _is_running(pid):
            break
        time.sleep(1)
    else:
        os.kill(pid, signal.SIGKILL)

    entry["pid"] = None
    _save_registry(reg)
    print(f"✅  Stopped {username} (was PID {pid}).")


def cmd_list() -> None:
    reg = _load_registry()
    if not reg:
        print("No users provisioned yet. Run 'create' to add one.")
        return

    hdr = f"{'USER':<16} {'PORT':>5}  {'STATUS':<10} {'PID':>7}  {'CAPITAL':>10}  CREATED"
    print(hdr)
    print("─" * len(hdr))
    for username, entry in sorted(reg.items()):
        pid    = entry.get("pid")
        status = "running" if _is_running(pid) else "stopped"
        pid_s  = str(pid) if pid else "—"
        cap_s  = f"₹{int(entry.get('capital', 0)):,}"
        created = entry.get("created_at", "")[:10]
        print(f"{username:<16} {entry['port']:>5}  {status:<10} {pid_s:>7}  {cap_s:>10}  {created}")


def cmd_logs(username: str) -> None:
    reg = _load_registry()
    if username not in reg:
        sys.exit(f"❌  Unknown user '{username}'.")
    log = _INSTANCES / username / "logs" / "server.log"
    if not log.exists():
        sys.exit(f"❌  No log file yet: {log}")
    try:
        subprocess.run(["tail", "-f", str(log)])
    except KeyboardInterrupt:
        pass


def cmd_status(username: str) -> None:
    reg = _load_registry()
    if username not in reg:
        sys.exit(f"❌  Unknown user '{username}'.")

    entry = reg[username]
    pid   = entry.get("pid")
    port  = entry["port"]
    up    = _is_running(pid)
    health: dict = {}

    if up and _REQUESTS_OK:
        try:
            r = _requests.get(f"http://127.0.0.1:{port}/health", timeout=3)
            health = r.json()
        except Exception:
            health = {"error": "unreachable"}

    out = {
        "user":    username,
        "port":    port,
        "pid":     pid,
        "running": up,
        "capital": entry.get("capital"),
        "created": entry.get("created_at"),
        "health":  health,
    }
    print(json.dumps(out, indent=2))


def cmd_delete(username: str) -> None:
    reg = _load_registry()
    if username not in reg:
        sys.exit(f"❌  Unknown user '{username}'.")

    confirm = input(f"Delete ALL data for '{username}'? This cannot be undone. Type 'yes': ")
    if confirm.strip().lower() != "yes":
        print("Aborted.")
        return

    # Stop if running
    if _is_running(reg[username].get("pid")):
        cmd_stop(username)

    # Remove files
    inst_dir = _INSTANCES / username
    if inst_dir.exists():
        import shutil
        shutil.rmtree(inst_dir)

    del reg[username]
    _save_registry(reg)
    print(f"✅  Deleted user '{username}' and all their data.")


def cmd_nginx() -> None:
    reg = _load_registry()
    if not reg:
        print("No users yet.")
        return

    print("# nginx reverse-proxy config — add to /etc/nginx/sites-enabled/algotrader")
    print("# Replace YOUR_DOMAIN with your actual domain or server IP.\n")
    for username, entry in sorted(reg.items()):
        port = entry["port"]
        print(f"# {username}")
        print(f"server {{")
        print(f"    listen 80;")
        print(f"    server_name {username}.YOUR_DOMAIN;")
        print(f"    location / {{")
        print(f"        proxy_pass         http://127.0.0.1:{port};")
        print(f"        proxy_set_header   Host $host;")
        print(f"        proxy_set_header   X-Real-IP $remote_addr;")
        print(f"        proxy_http_version 1.1;")
        print(f"        proxy_set_header   Upgrade $http_upgrade;")
        print(f"        proxy_set_header   Connection \"upgrade\";")
        print(f"    }}")
        print(f"}}\n")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="AlgoTrader Pro v4 — multi-user instance manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # create
    c = sub.add_parser("create", help="Create a new user instance")
    c.add_argument("--user",    required=True, help="Username (e.g. alice)")
    c.add_argument("--port",    required=True, type=int, help="Port for this instance (e.g. 8001)")
    c.add_argument("--capital", default=100_000, type=float,
                   help="Starting capital in ₹ (default: 100000)")

    # start / stop / logs / status / delete
    for name, hlp in [
        ("start",  "Start a user's instance"),
        ("stop",   "Stop a user's instance"),
        ("logs",   "Tail a user's server log"),
        ("status", "Show JSON health for a user"),
        ("delete", "Stop + permanently delete a user"),
    ]:
        s = sub.add_parser(name, help=hlp)
        s.add_argument("user", help="Username")

    # list / nginx
    sub.add_parser("list",  help="List all users and their status")
    sub.add_parser("nginx", help="Print nginx reverse-proxy config")

    args = p.parse_args()

    if args.cmd == "create":
        cmd_create(args.user, args.port, args.capital)
    elif args.cmd == "start":
        cmd_start(args.user)
    elif args.cmd == "stop":
        cmd_stop(args.user)
    elif args.cmd == "list":
        cmd_list()
    elif args.cmd == "logs":
        cmd_logs(args.user)
    elif args.cmd == "status":
        cmd_status(args.user)
    elif args.cmd == "delete":
        cmd_delete(args.user)
    elif args.cmd == "nginx":
        cmd_nginx()


if __name__ == "__main__":
    main()
