"""
DigitalOcean OAuth admin client — lets an admin authorize AlgoTrader Pro to
read its own App Platform deployment status (name, phase, live URL).
Not a trading broker; unrelated to kite_client / upstox_broker / kotak.
"""
from __future__ import annotations

import httpx
from loguru import logger

from config import settings

_AUTHORIZE_URL = "https://cloud.digitalocean.com/v1/oauth/authorize"
_TOKEN_URL = "https://cloud.digitalocean.com/v1/oauth/token"
_API_BASE = "https://api.digitalocean.com/v2"


def login_url() -> str:
    return (
        f"{_AUTHORIZE_URL}?client_id={settings.digitalocean_client_id}"
        f"&redirect_uri={settings.digitalocean_redirect_url}"
        f"&response_type=code&scope=read"
    )


def exchange_code(code: str) -> str:
    payload = {
        "client_id": settings.digitalocean_client_id,
        "client_secret": settings.digitalocean_client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": settings.digitalocean_redirect_url,
    }
    resp = httpx.post(
        _TOKEN_URL, data=payload,
        headers={"Accept": "application/json"}, timeout=15.0,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token", "")
    if not token:
        raise RuntimeError("DigitalOcean token exchange returned no access_token")

    settings.digitalocean_access_token = token
    from state_store import set_kv
    set_kv("digitalocean_access_token", token)
    logger.info("[DigitalOcean] OAuth connected — token: {}…", token[:8])
    return token


def get_app_status() -> dict:
    token = settings.digitalocean_access_token
    if not token:
        raise RuntimeError("Not connected — no DigitalOcean access token")

    resp = httpx.get(
        f"{_API_BASE}/apps",
        headers={"Authorization": f"Bearer {token}"}, timeout=15.0,
    )
    resp.raise_for_status()
    apps = resp.json().get("apps", [])

    if settings.digitalocean_app_id:
        apps = [a for a in apps if a.get("id") == settings.digitalocean_app_id]

    summary = []
    for a in apps:
        dep = a.get("active_deployment") or {}
        summary.append({
            "id": a.get("id"),
            "name": a.get("spec", {}).get("name"),
            "phase": dep.get("phase"),
            "live_url": a.get("live_url"),
            "created_at": a.get("created_at"),
            "updated_at": a.get("updated_at"),
        })
    return {"connected": True, "apps": summary}
