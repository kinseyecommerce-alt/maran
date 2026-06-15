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
    """/dashboard is not in _SENSITIVE_GETS — serves HTML without auth."""
    r = requests.get(f"{BASE}/dashboard", timeout=5)
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    html = r.text
    assert "AlgoTrader" in html
    # Must be a non-trivial page.
    assert len(html) > 1000


def test_root_redirects_to_dashboard():
    """GET / redirects to /dashboard (no auth cookie → /login flow in browser)."""
    r = requests.get(f"{BASE}/", timeout=5, allow_redirects=False)
    assert r.status_code in (301, 302, 303, 307, 308)
    assert "/dashboard" in r.headers.get("location", "")
