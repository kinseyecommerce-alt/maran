"""Kite access-token resolution (no network, no browser)."""

from app.brokers.kite_auth import can_auto_login, resolve_access_token
from app.core.config import Settings

_FULL = dict(
    zerodha_user_id="ZA0000",
    zerodha_password="pw",
    zerodha_totp_secret="JBSWY3DPEHPK3PXP",
    zerodha_api_key="k",
    zerodha_api_secret="s",
    zerodha_redirect_url="https://example.com/cb",
)


def test_env_access_token_is_preferred():
    s = Settings(zerodha_access_token="tok123", **_FULL)
    assert resolve_access_token(s) == "tok123"


def test_none_when_no_credentials():
    assert resolve_access_token(Settings()) is None


def test_can_auto_login_requires_full_set():
    assert can_auto_login(Settings()) is False
    assert can_auto_login(Settings(**_FULL)) is True
    partial = dict(_FULL)
    partial.pop("zerodha_totp_secret")
    assert can_auto_login(Settings(**partial)) is False
