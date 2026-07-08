"""
kite_auto_login.py
Headless Playwright automation for the Zerodha Kite Connect OAuth flow.
Called by the platform scheduler at 08:50 AM IST every trading day.

Requires in .env:
  KITE_USER_ID       — Zerodha client ID (e.g. ZA1234)
  KITE_PASSWORD      — Zerodha login password
  KITE_TOTP_SECRET   — TOTP secret from Zerodha 2FA setup (Google Authenticator seed)
  KITE_REDIRECT_URL  — full callback URL registered in your Kite app
                       e.g. https://yourdomain.com/auth/kite/callback
"""
from __future__ import annotations

import re
import asyncio
from loguru import logger

import pyotp
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

from config import settings
from kite_client import kite_client


_CHROMIUM = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
_login_lock = asyncio.Lock()


def _extract_request_token(url: str) -> str | None:
    m = re.search(r"[?&]request_token=([^&]+)", url)
    return m.group(1) if m else None


def run_kite_auto_login() -> str:
    """
    Drives the full Zerodha Kite Connect OAuth flow in a headless browser.

    Returns the new access token string on success.
    Raises RuntimeError on any failure (caller sends Telegram alert).
    """
    missing = [k for k in ("kite_user_id", "kite_password", "kite_totp_secret",
                            "kite_api_key", "kite_redirect_url")
               if not getattr(settings, k, "")]
    if missing:
        raise RuntimeError(f"Missing config: {', '.join(k.upper() for k in missing)}")

    totp = pyotp.TOTP(settings.kite_totp_secret)
    login_url = kite_client.login_url()
    redirect_prefix = settings.kite_redirect_url.split("?")[0]
    # The callback endpoint runs IN THIS PROCESS and stores the exchanged
    # token — if it changes during the flow, the login succeeded no matter
    # what the browser-side URL race says (proven live 2026-07-08: the
    # callback page bounces to /dashboard before wait_for_url starts).
    _token_before = settings.kite_access_token
    _xchg_before  = getattr(kite_client, "last_token_exchange_ts", 0.0)

    import os
    chromium = _CHROMIUM if os.path.exists(_CHROMIUM) else None  # falls back to installed

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=chromium,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        ctx  = browser.new_context()
        page = ctx.new_page()
        page.set_default_timeout(30_000)

        try:
            # ── 1. Open Kite login ────────────────────────────────────────
            logger.info("[kite_login] navigating to Kite OAuth…")
            page.goto(login_url, wait_until="domcontentloaded")

            # ── 2. User ID + password ─────────────────────────────────────
            page.fill('input[id="userid"], input[placeholder*="User ID"]', settings.kite_user_id)
            page.fill('input[id="password"], input[type="password"]',      settings.kite_password)
            page.click('button[type="submit"]')

            # ── 3. TOTP 2FA ───────────────────────────────────────────────
            # Zerodha has changed this page over time: the classic input#pin
            # became a numeric input (id reused as "userid") inside
            # form.twofa-form. Match the form first, then fall back through
            # every shape seen in the wild.
            twofa_sel = (
                'form.twofa-form input:not([type="hidden"]), '
                'input[type="number"], '
                'input[autocomplete="one-time-code"], '
                'input[id="pin"], input[placeholder*="TOTP"], '
                'input[placeholder*="code"]'
            )
            try:
                page.wait_for_selector(twofa_sel, timeout=15_000)
            except PwTimeout:
                # Distinguish "bad password" from "Zerodha changed the DOM":
                # surface any visible error banner + where we actually landed.
                err_txt = ""
                try:
                    el = page.query_selector('.su-error, .error, [class*="error"]')
                    if el:
                        err_txt = (el.inner_text() or "").strip()[:120]
                except Exception:
                    pass
                raise RuntimeError(
                    f"2FA input never appeared. url={page.url} "
                    f"login-error={err_txt or 'none visible'} "
                    f"(an error banner here usually means wrong password; "
                    f"no banner means the 2FA page DOM changed again)"
                )
            page.fill(twofa_sel, totp.now())
            # Kite AUTO-SUBMITS the 2FA form once 6 digits land — by the time
            # we'd click, the page is usually already navigating to the
            # callback (proven live 2026-07-08: click timed out AFTER the
            # redirect carried request_token & status=success). The click is
            # only a fallback for the older non-auto-submit page; never let
            # it block the redirect wait below.
            try:
                page.click('button[type="submit"]', timeout=4_000)
            except Exception:
                pass  # already navigated / button gone — wait_for_url decides

            # ── 4. Wait for redirect to our callback URL ──────────────────
            try:
                page.wait_for_url(f"{redirect_prefix}**", timeout=20_000)
            except PwTimeout:
                # Repeat logins sometimes land on Kite's app-authorization
                # (consent) page instead of redirecting. Click through it
                # once, then wait again. Otherwise surface WHERE we are.
                # Success race: the callback page redirects onward to
                # /dashboard within ~1s — landing ANYWHERE on our own app
                # host means the callback already ran server-side.
                _own_host = redirect_prefix.split("/auth/")[0]
                _exchanged = getattr(kite_client, "last_token_exchange_ts", 0.0) > _xchg_before
                if page.url.startswith(_own_host) and (_exchanged or (
                        settings.kite_access_token
                        and settings.kite_access_token != _token_before)):
                    logger.info("[kite_login] callback completed server-side "
                                "(landed on {})", page.url)
                else:
                    try:
                        btn = page.query_selector(
                            'button:has-text("Authorize"), button:has-text("Continue"), '
                            'button[type="submit"]')
                        if btn:
                            btn.click()
                            page.wait_for_url(f"{redirect_prefix}**", timeout=15_000)
                        else:
                            raise PwTimeout("no consent button")
                    except Exception:
                        raise RuntimeError(
                            f"Kite did not redirect to callback URL in time "
                            f"(stuck at {page.url})")

            request_token = _extract_request_token(page.url)
            if not request_token and (
                    getattr(kite_client, "last_token_exchange_ts", 0.0) > _xchg_before
                    or (settings.kite_access_token
                        and settings.kite_access_token != _token_before)):
                # Server-side callback already exchanged the token — done.
                logger.info("[kite_login] token refreshed via in-process callback")
                return settings.kite_access_token
            if not request_token:
                raise RuntimeError("Kite did not include request_token in redirect URL")

        finally:
            browser.close()

    # ── 5. Exchange request_token for access_token ────────────────────────
    token = kite_client.set_access_token(request_token=request_token)
    logger.info("[kite_login] access token refreshed: {}…", token[:8])
    return token


async def refresh_kite_token_async() -> str:
    """Async wrapper — runs blocking Playwright in a thread pool."""
    async with _login_lock:
        return await asyncio.to_thread(run_kite_auto_login)
