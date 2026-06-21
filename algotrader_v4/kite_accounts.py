"""
kite_accounts.py — Multi-account Kite credential store.
Persists accounts to kite_accounts.json alongside main.py.

Security tiers (best → acceptable):
  1. Set KITE_ACCOUNTS_KEY in .env — secrets are Fernet-encrypted at rest.
  2. No key set — secrets are plaintext JSON; file is chmod 600 (set by deploy script).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

from loguru import logger

import os as _os_ka
_STORE_PATH = Path(_os_ka.environ.get("KITE_ACCOUNTS_FILE",
                                      str(Path(__file__).parent / "kite_accounts.json")))
_lock = threading.Lock()
_accounts: dict[str, dict] = {}
_loaded = False

# ── Encryption helpers ────────────────────────────────────────────────────────

def _get_fernet():
    """Return a Fernet instance if KITE_ACCOUNTS_KEY is configured, else None."""
    try:
        from config import settings
        key = (settings.kite_accounts_key or "").strip()
        if not key:
            return None
        from cryptography.fernet import Fernet
        return Fernet(key.encode())
    except Exception:
        return None


def _encrypt_fields(account: dict) -> dict:
    """Encrypt sensitive string fields in-place for storage."""
    f = _get_fernet()
    if f is None:
        return account
    out = dict(account)
    for field in ("api_key", "api_secret", "access_token"):
        val = out.get(field, "")
        if val:
            out[field] = f.encrypt(val.encode()).decode()
    return out


def _decrypt_fields(account: dict) -> dict:
    """Decrypt sensitive string fields loaded from storage."""
    f = _get_fernet()
    if f is None:
        return account
    out = dict(account)
    for field in ("api_key", "api_secret", "access_token"):
        val = out.get(field, "")
        if val:
            try:
                out[field] = f.decrypt(val.encode()).decode()
            except Exception:
                # Value wasn't encrypted (e.g. migration from plaintext) — keep as-is
                pass
    return out


# ── Persistence ───────────────────────────────────────────────────────────────

def _load() -> None:
    global _accounts, _loaded
    if _loaded:
        return
    if _STORE_PATH.exists():
        try:
            raw = json.loads(_STORE_PATH.read_text())
            _accounts = {name: _decrypt_fields(acc) for name, acc in raw.items()}
        except Exception as exc:
            logger.error("[kite_accounts] CRITICAL: failed to load accounts: {} — not overwriting file", exc)
            _accounts = {}
            return   # leave _loaded = False so next call retries; prevents silent credential wipe
    _loaded = True


def _save() -> None:
    try:
        stored = {name: _encrypt_fields(acc) for name, acc in _accounts.items()}
        tmp = _STORE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(stored, indent=2))
        tmp.replace(_STORE_PATH)
    except Exception as exc:
        logger.error("[kite_accounts] CRITICAL: failed to persist accounts: {}", exc)
        raise


def _mask(key: str) -> str:
    return f"{key[:4]}…{key[-4:]}" if len(key) >= 8 else ("*" * len(key))


# ── Public API ────────────────────────────────────────────────────────────────

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
