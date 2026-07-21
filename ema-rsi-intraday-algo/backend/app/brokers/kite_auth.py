"""Kite access-token resolution for the live service.

Order of preference:
  1. `ZERODHA_ACCESS_TOKEN` (or `KITE_ACCESS_TOKEN`) from the environment — the simple,
     operator-refreshed daily token. Always tried first.
  2. Headless TOTP auto-login (Playwright + pyotp) when the full credential set is
     present. Drives the Kite OAuth form and exchanges the request token.

Everything here is lazy-imported (`kiteconnect`, `playwright`, `pyotp`) so importing this
module — and therefore booting the web process — never fails just because a browser or the
broker SDK is missing. `resolve_access_token` returns None when no path is available; the
service then simply stays "not ready" instead of crashing.
"""

from __future__ import annotations

import logging
import re

from app.core.config import Settings

logger = logging.getLogger(__name__)

_AUTO_LOGIN_FIELDS = (
    "zerodha_user_id",
    "zerodha_password",
    "zerodha_totp_secret",
    "zerodha_api_key",
    "zerodha_api_secret",
    "zerodha_redirect_url",
)


def can_auto_login(settings: Settings) -> bool:
    return all(getattr(settings, f, "") for f in _AUTO_LOGIN_FIELDS)


def resolve_access_token(settings: Settings, *, errors: list[str] | None = None) -> str | None:
    """Return a usable Kite access token, or None if none can be obtained.

    Any failure reason is appended to `errors` (when provided) so callers can surface it
    on the readiness endpoint without re-running the flow.
    """
    if settings.zerodha_access_token:
        return settings.zerodha_access_token
    if can_auto_login(settings):
        try:
            return auto_login(settings)
        except Exception as exc:  # never let a login failure crash the web process
            logger.warning("kite auto-login failed: %s", exc)
            if errors is not None:
                errors.append(str(exc))
            return None
    if errors is not None:
        missing = [f for f in _AUTO_LOGIN_FIELDS if not getattr(settings, f, "")]
        errors.append(f"no access token and auto-login incomplete (missing: {', '.join(missing)})")
    return None


def _extract_request_token(url: str) -> str | None:
    m = re.search(r"[?&]request_token=([^&]+)", url)
    return m.group(1) if m else None


def exchange_request_token(settings: Settings, request_token: str) -> str:
    """Exchange a Kite request_token for an access_token (blocking network call)."""
    from kiteconnect import KiteConnect  # lazy

    kite = KiteConnect(api_key=settings.zerodha_api_key)
    data = kite.generate_session(request_token, api_secret=settings.zerodha_api_secret)
    return data["access_token"]


def auto_login(settings: Settings) -> str:
    """Headless Zerodha OAuth via Playwright + TOTP → access token.

    Raises RuntimeError on any failure. Ported from the retired platform's proven flow;
    Kite auto-submits the 2FA form once six digits land, so the redirect wait is what
    actually confirms success.
    """
    import contextlib
    import os

    import pyotp
    from kiteconnect import KiteConnect
    from playwright.sync_api import sync_playwright

    if not can_auto_login(settings):
        missing = [f.upper() for f in _AUTO_LOGIN_FIELDS if not getattr(settings, f, "")]
        raise RuntimeError(f"auto-login missing config: {', '.join(missing)}")

    totp = pyotp.TOTP(settings.zerodha_totp_secret)
    login_url = KiteConnect(api_key=settings.zerodha_api_key).login_url()
    redirect_prefix = settings.zerodha_redirect_url.split("?")[0]

    chromium = "/opt/pw-browsers/chromium/chrome-linux/chrome"
    exe = chromium if os.path.exists(chromium) else None

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=exe,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        page = browser.new_context().new_page()
        page.set_default_timeout(30_000)
        try:
            page.goto(login_url, wait_until="domcontentloaded")
            page.fill('input[id="userid"], input[placeholder*="User ID"]', settings.zerodha_user_id)
            page.fill('input[id="password"], input[type="password"]', settings.zerodha_password)
            page.click('button[type="submit"]')

            twofa_sel = (
                'form.twofa-form input:not([type="hidden"]), '
                'input[type="number"], input[autocomplete="one-time-code"], '
                'input[id="pin"], input[placeholder*="TOTP"], input[placeholder*="code"]'
            )
            page.wait_for_selector(twofa_sel, timeout=15_000)
            page.fill(twofa_sel, totp.now())
            with contextlib.suppress(Exception):
                page.click('button[type="submit"]', timeout=4_000)  # Kite auto-submits

            page.wait_for_url(f"{redirect_prefix}**", timeout=20_000)
            request_token = _extract_request_token(page.url)
        finally:
            browser.close()

    if not request_token:
        raise RuntimeError(f"no request_token in redirect (stuck at {page.url})")
    return exchange_request_token(settings, request_token)
