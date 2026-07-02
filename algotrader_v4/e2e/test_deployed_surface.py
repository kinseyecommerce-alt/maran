"""
Deployed-surface smoke tests for the FastAPI backend.

Run with uvicorn already up, or via the convenience script:
    cd algotrader_v4
    uvicorn main:app --host 127.0.0.1 --port 8000 &
    python -m pytest e2e/test_deployed_surface.py -v

No credentials or .env required — PAPER mode default is used.
All assertions target unauthenticated/exempt endpoints only.
"""
import time
import subprocess
import signal
import sys
import pytest
import requests

BASE = "http://127.0.0.1:8000"
_proc: subprocess.Popen | None = None


@pytest.fixture(scope="session", autouse=True)
def _start_server():
    """Boot uvicorn once per session; skip if already running."""
    global _proc
    try:
        r = requests.get(f"{BASE}/health", timeout=2)
        if r.status_code == 200:
            yield  # already running (dev mode / CI pre-boot)
            return
    except requests.ConnectionError:
        pass

    _proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app",
         "--host", "127.0.0.1", "--port", "8000", "--no-access-log"],
        cwd=str(__file__).replace("/e2e/test_deployed_surface.py", ""),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # Wait up to 45 s for the server to be ready.
    # Startup includes TrueData connection attempt (~5 s timeout) + symbol scanner.
    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            r = requests.get(f"{BASE}/health", timeout=2)
            if r.status_code == 200:
                break
        except requests.ConnectionError:
            time.sleep(1)
    else:
        _proc.terminate()
        pytest.fail("uvicorn did not start within 45 s")

    yield

    _proc.terminate()
    try:
        _proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _proc.kill()


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_health_returns_ok_paper():
    """GET /health is exempt from auth and returns PAPER mode."""
    r = requests.get(f"{BASE}/health", timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["mode"] == "PAPER"
    assert "agents" in body


def test_login_page_renders():
    """/login serves the login HTML with a username input."""
    r = requests.get(f"{BASE}/login", timeout=5)
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    html = r.text
    assert "AlgoTrader" in html
    assert 'id="username"' in html
    assert 'id="password"' in html


def test_dashboard_page_renders():
    """/dashboard serves the React SPA shell (vite build) without auth.

    The SPA shell is a small HTML document whose content lives in the bundled
    JS — assert the shell structure and that its referenced assets resolve,
    rather than a server-rendered page size."""
    r = requests.get(f"{BASE}/dashboard", timeout=5)
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    html = r.text
    assert "AlgoTrader" in html
    assert 'id="root"' in html          # React mount point
    # Every /assets/… script or stylesheet the shell references must resolve —
    # a stale hash means a blank dashboard in production.
    import re
    for asset in re.findall(r'(?:src|href)="(/assets/[^"]+)"', html):
        ar = requests.get(f"{BASE}{asset}", timeout=5)
        assert ar.status_code == 200, f"SPA asset {asset} → {ar.status_code}"


def test_root_serves_spa():
    """GET / serves the same React SPA shell (no redirect since the SPA
    handles routing client-side)."""
    r = requests.get(f"{BASE}/", timeout=5, allow_redirects=False)
    assert r.status_code == 200
    assert 'id="root"' in r.text
