"""
kite_accounts.py — Multi-account Kite credential store.
Persists accounts to kite_accounts.json alongside main.py.
Secrets are stored as-is (plain text on disk) — protect the file.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

_STORE_PATH = Path(__file__).parent / "kite_accounts.json"
_lock = threading.Lock()
_accounts: dict[str, dict] = {}
_loaded = False


def _load() -> None:
    global _accounts, _loaded
    if _loaded:
        return
    if _STORE_PATH.exists():
        try:
            _accounts = json.loads(_STORE_PATH.read_text())
        except Exception:
            _accounts = {}
    _loaded = True


def _save() -> None:
    _STORE_PATH.write_text(json.dumps(_accounts, indent=2))


def _mask(key: str) -> str:
    return f"{key[:4]}…{key[-4:]}" if len(key) >= 8 else ("*" * len(key))


# ── Public API ─────────────────────────────────────────────────────────────────

def list_accounts() -> list[dict]:
    """Return all accounts with secrets masked."""
    with _lock:
        _load()
        return [
            {
                "name":           name,
                "api_key_masked": _mask(acc.get("api_key", "")) if acc.get("api_key") else "",
                "has_secret":     bool(acc.get("api_secret")),
                "has_token":      bool(acc.get("access_token")),
                "active":         acc.get("active", False),
            }
            for name, acc in _accounts.items()
        ]


def add_or_update(name: str, api_key: str, api_secret: str) -> None:
    """Add a new account or update an existing one's key/secret."""
    with _lock:
        _load()
        existing = _accounts.get(name, {})
        _accounts[name] = {
            "api_key":      api_key,
            "api_secret":   api_secret,
            "access_token": existing.get("access_token", ""),
            "active":       existing.get("active", False),
        }
        _save()


def delete(name: str) -> bool:
    with _lock:
        _load()
        if name not in _accounts:
            return False
        del _accounts[name]
        _save()
        return True


def activate(name: str) -> Optional[dict]:
    """Mark account as active, deactivate all others. Returns credentials dict or None."""
    with _lock:
        _load()
        if name not in _accounts:
            return None
        for n in _accounts:
            _accounts[n]["active"] = (n == name)
        _save()
        return dict(_accounts[name])


def get_active() -> Optional[dict]:
    with _lock:
        _load()
        for name, acc in _accounts.items():
            if acc.get("active"):
                return {"name": name, **acc}
        return None


def update_access_token(name: str, token: str) -> None:
    with _lock:
        _load()
        if name in _accounts:
            _accounts[name]["access_token"] = token
            _save()
