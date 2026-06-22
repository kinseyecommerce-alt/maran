"""
notifier.py
Unified alert dispatcher: SMTP email + Telegram.
Called on kill-switch trigger, daily-loss-limit breach, morning startup, restart.

Config (from settings):
  alert_email: str = ""           # recipient; empty = email disabled
  smtp_host: str = "smtp.gmail.com"
  smtp_port: int = 587
  smtp_user: str = ""             # sender email
  smtp_pass: str = ""             # app password
  telegram_bot_token / telegram_chat_id — already exist

Usage:
  from notifier import notifier
  notifier.send("Kill switch triggered", level="CRITICAL")
  notifier.send_daily_summary(pnl=1234, trades=8, win_rate=62.5)
"""
from __future__ import annotations

import smtplib
import urllib.request
import urllib.parse
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from loguru import logger

from config import settings


class Notifier:

    def send(self, subject: str, body: str = "", level: str = "INFO") -> None:
        """
        Try email (if configured) AND telegram (if configured).
        Swallows all errors; logs outcome at DEBUG.
        Escalates to CRITICAL if both configured channels fail.
        """
        full_subject = f"[AlgoTrader][{level}] {subject}"
        full_body = body or subject

        email_configured = bool(getattr(settings, "alert_email", "") and getattr(settings, "smtp_user", ""))
        telegram_configured = bool(getattr(settings, "telegram_bot_token", "")
                                   and getattr(settings, "telegram_chat_id", ""))
        email_ok = False
        telegram_ok = False

        # Email
        if email_configured:
            try:
                self.send_email(full_subject, full_body)
                logger.debug("Notifier: email sent — {}", full_subject)
                email_ok = True
            except Exception as exc:
                logger.warning("Notifier: email failed — {}: {}", full_subject, exc)

        # Telegram
        if telegram_configured:
            try:
                msg = f"{full_subject}\n\n{full_body}" if full_body != full_subject else full_subject
                self.send_telegram(msg)
                logger.debug("Notifier: telegram sent — {}", full_subject)
                telegram_ok = True
            except Exception as exc:
                logger.warning("Notifier: telegram failed — {}: {}", full_subject, exc)

        # Both channels were configured but both failed — alert may be lost
        if (email_configured or telegram_configured) and not email_ok and not telegram_ok:
            logger.critical(
                "Notifier: ALL channels failed for [{}] — alert may be lost", full_subject
            )

    def send_email(self, subject: str, body: str) -> None:
        """SMTP/TLS using smtplib; smtp_user → alert_email."""
        smtp_host = getattr(settings, "smtp_host", "smtp.gmail.com")
        smtp_port = getattr(settings, "smtp_port", 587)
        smtp_user = getattr(settings, "smtp_user", "")
        smtp_pass = getattr(settings, "smtp_pass", "")
        alert_email = getattr(settings, "alert_email", "")

        if not smtp_user or not smtp_pass or not alert_email:
            raise ValueError("SMTP credentials or alert_email not configured")

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = smtp_user
        msg["To"] = alert_email
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()  # required after TLS — server re-advertises AUTH capability
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, alert_email, msg.as_string())

    def send_telegram(self, msg: str) -> None:
        """POST to Telegram Bot API."""
        token = getattr(settings, "telegram_bot_token", "")
        chat_id = getattr(settings, "telegram_chat_id", "")
        if not token or not chat_id:
            raise ValueError("Telegram token or chat_id not configured")

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({"chat_id": chat_id, "text": msg}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data.get('description', 'unknown')}")

    def send_daily_summary(
        self,
        pnl: float,
        trades: int,
        win_rate: float,
        drawdown_pct: float = 0.0,
    ) -> None:
        """Format and send daily P&L summary."""
        sign = "+" if pnl >= 0 else ""
        body = (
            f"Daily Summary\n"
            f"P&L:       {sign}₹{pnl:,.0f}\n"
            f"Trades:    {trades}\n"
            f"Win rate:  {win_rate:.1f}%\n"
            f"Max DD:    {drawdown_pct:.1f}%\n"
            f"Mode:      {settings.trading_mode}"
        )
        level = "INFO" if pnl >= 0 else "WARNING"
        self.send(subject="Daily P&L Summary", body=body, level=level)

    def send_kill_switch_alert(self, reason: str) -> None:
        """Critical alert with [KILL SWITCH] prefix."""
        self.send(
            subject=f"[KILL SWITCH] {reason}",
            body=(
                f"The AlgoTrader kill switch has been triggered.\n\n"
                f"Reason: {reason}\n"
                f"Mode:   {settings.trading_mode}\n\n"
                f"All new order placement is halted until the kill switch is reset."
            ),
            level="CRITICAL",
        )

    def send_startup_alert(self, mode: str, agents: list[str]) -> None:
        """Morning startup notification."""
        agent_list = ", ".join(agents) if agents else "none"
        self.send(
            subject=f"AlgoTrader started ({mode})",
            body=(
                f"AlgoTrader Pro has started.\n\n"
                f"Mode:    {mode}\n"
                f"Agents:  {agent_list}\n"
            ),
            level="INFO",
        )


# Module-level singleton
notifier = Notifier()
