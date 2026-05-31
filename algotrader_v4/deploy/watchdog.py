#!/usr/bin/env python3
"""
deploy/watchdog.py
──────────────────────────────────────────────────────────
HA watchdog for AlgoTrader Pro. Run alongside the main service:
  python deploy/watchdog.py

Reads config from /opt/algotrader/.env (or local .env).
Monitors GET /health on localhost:8000 every 30 seconds.
Restarts via `systemctl restart algotrader` on 3 consecutive failures.
Sends Telegram alerts on restart and too-many-restarts events.

Run as a separate systemd service or screen session.

──────────────────────────────────────────────────────────
Systemd unit (save as /etc/systemd/system/algotrader-watchdog.service):

[Unit]
Description=AlgoTrader Watchdog
After=algotrader.service
Wants=algotrader.service

[Service]
Type=simple
User=algotrader
WorkingDirectory=/opt/algotrader
ExecStart=/opt/algotrader/venv/bin/python /opt/algotrader/deploy/watchdog.py
Restart=always
RestartSec=10
StandardOutput=append:/var/log/algotrader/watchdog.log
StandardError=append:/var/log/algotrader/watchdog.log

[Install]
WantedBy=multi-user.target
──────────────────────────────────────────────────────────
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchdog] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("watchdog")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class WatchdogConfig:
    health_url: str = "http://127.0.0.1:8000/health"
    check_interval_sec: int = 30
    fail_threshold: int = 3
    restart_cmd: str = "systemctl restart algotrader"
    max_restarts_per_hour: int = 5
    cooldown_sec: int = 1800          # 30 min pause after too-many-restarts
    telegram_token: str = ""
    telegram_chat_id: str = ""


# ---------------------------------------------------------------------------
# Telegram helper
# ---------------------------------------------------------------------------

def _send_telegram(token: str, chat_id: str, msg: str) -> None:
    """Fire-and-forget Telegram message. Swallows all errors."""
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({"chat_id": chat_id, "text": msg}).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
    except Exception as exc:  # noqa: BLE001
        log.warning("Telegram send failed: %s", exc)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

def _check_health(url: str) -> bool:
    """
    GET *url* with a 5-second timeout.
    Returns True only when HTTP 200 and response body contains '"status":"ok"'
    (with or without surrounding whitespace).
    """
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            if resp.status != 200:
                return False
            body = resp.read().decode(errors="replace")
            # Accept both {"status":"ok"} and {"status": "ok"}
            return '"status"' in body and '"ok"' in body
    except Exception as exc:  # noqa: BLE001
        log.debug("Health check error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Service restart
# ---------------------------------------------------------------------------

def _restart_service(cmd: str) -> bool:
    """
    Run *cmd* (split on spaces) with a 30-second timeout.
    Returns True on returncode 0, False otherwise.
    """
    try:
        result = subprocess.run(
            cmd.split(),
            timeout=30,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            log.info("Restart succeeded (cmd=%r)", cmd)
            return True
        log.error(
            "Restart failed (rc=%d): %s", result.returncode, result.stderr.strip()
        )
        return False
    except subprocess.TimeoutExpired:
        log.error("Restart command timed out after 30 s")
        return False
    except Exception as exc:  # noqa: BLE001
        log.error("Restart command raised: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------

class Watchdog:
    def __init__(self, cfg: WatchdogConfig) -> None:
        self.cfg = cfg
        self.consecutive_failures: int = 0
        self.restart_timestamps: List[float] = []   # epoch seconds

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _notify(self, msg: str) -> None:
        log.info(msg)
        _send_telegram(self.cfg.telegram_token, self.cfg.telegram_chat_id, msg)

    def _recent_restart_count(self) -> int:
        """Number of restarts in the last 3600 seconds."""
        cutoff = time.time() - 3600
        self.restart_timestamps = [t for t in self.restart_timestamps if t >= cutoff]
        return len(self.restart_timestamps)

    def _do_restart(self) -> None:
        """Attempt service restart, send Telegram alert, enforce cool-down if
        too many restarts have occurred within the last hour."""
        now = time.time()
        self.restart_timestamps.append(now)
        recent = self._recent_restart_count()

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        self._notify(
            f"[AlgoTrader Watchdog] {ts}\n"
            f"Health check failed {self.cfg.fail_threshold}x in a row.\n"
            f"Restarting service (restart #{recent} in last hour)."
        )

        success = _restart_service(self.cfg.restart_cmd)
        if not success:
            self._notify(
                "[AlgoTrader Watchdog] WARNING: restart command returned non-zero. "
                "Manual intervention may be required."
            )

        # Reset failure counter so we don't restart-loop immediately
        self.consecutive_failures = 0

        # Too-many-restarts guard
        if recent > self.cfg.max_restarts_per_hour:
            cooldown_min = self.cfg.cooldown_sec // 60
            self._notify(
                f"[AlgoTrader Watchdog] CRITICAL: {recent} restarts in the last hour "
                f"(threshold={self.cfg.max_restarts_per_hour}). "
                f"Pausing watchdog for {cooldown_min} minutes. "
                "Please investigate."
            )
            log.warning(
                "Too many restarts (%d). Sleeping %d s before resuming.",
                recent,
                self.cfg.cooldown_sec,
            )
            time.sleep(self.cfg.cooldown_sec)
            # Drain the restart list so the counter resets cleanly after cooldown
            self.restart_timestamps.clear()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        self._notify(
            f"[AlgoTrader Watchdog] Started at {ts}. "
            f"Monitoring {self.cfg.health_url} every {self.cfg.check_interval_sec}s."
        )

        while True:
            time.sleep(self.cfg.check_interval_sec)

            healthy = _check_health(self.cfg.health_url)

            if healthy:
                if self.consecutive_failures > 0:
                    log.info(
                        "Service recovered after %d failure(s).",
                        self.consecutive_failures,
                    )
                self.consecutive_failures = 0
            else:
                self.consecutive_failures += 1
                log.warning(
                    "Health check FAILED (%d/%d): %s",
                    self.consecutive_failures,
                    self.cfg.fail_threshold,
                    self.cfg.health_url,
                )
                if self.consecutive_failures >= self.cfg.fail_threshold:
                    self._do_restart()


# ---------------------------------------------------------------------------
# .env loader (python-dotenv optional, fallback to manual parse)
# ---------------------------------------------------------------------------

def _load_dotenv(path: str) -> None:
    """Load key=value pairs from *path* into os.environ (skip if missing)."""
    if not os.path.isfile(path):
        log.debug(".env file not found at %s — using existing env", path)
        return
    try:
        from dotenv import load_dotenv  # type: ignore[import]
        load_dotenv(path, override=False)
        log.info("Loaded .env from %s (via python-dotenv)", path)
        return
    except ImportError:
        pass
    # Manual fallback
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    log.info("Loaded .env from %s (manual parser)", path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="AlgoTrader HA watchdog — monitors /health and restarts on crash.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--url",
        default="http://127.0.0.1:8000/health",
        help="Health-check endpoint URL.",
    )
    p.add_argument(
        "--interval",
        type=int,
        default=30,
        metavar="SEC",
        help="Seconds between health checks.",
    )
    p.add_argument(
        "--fail-threshold",
        type=int,
        default=3,
        metavar="N",
        help="Consecutive failures before restart.",
    )
    p.add_argument(
        "--max-restarts",
        type=int,
        default=5,
        metavar="N",
        help="Max restarts per hour before cooldown.",
    )
    p.add_argument(
        "--cooldown",
        type=int,
        default=1800,
        metavar="SEC",
        help="Cooldown pause (seconds) after too many restarts.",
    )
    p.add_argument(
        "--restart-cmd",
        default="systemctl restart algotrader",
        help="Shell command used to restart the service.",
    )
    p.add_argument(
        "--env",
        default=None,
        metavar="PATH",
        help=(
            "Path to .env file. "
            "Defaults to /opt/algotrader/.env in production or ../.env locally."
        ),
    )
    return p


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()

    # Resolve .env path
    if args.env:
        env_path = args.env
    elif os.path.isfile("/opt/algotrader/.env"):
        env_path = "/opt/algotrader/.env"
    else:
        env_path = os.path.join(os.path.dirname(__file__), "..", ".env")

    _load_dotenv(env_path)

    cfg = WatchdogConfig(
        health_url=args.url,
        check_interval_sec=args.interval,
        fail_threshold=args.fail_threshold,
        restart_cmd=args.restart_cmd,
        max_restarts_per_hour=args.max_restarts,
        cooldown_sec=args.cooldown,
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
    )

    log.info(
        "Config: url=%s interval=%ds fail_threshold=%d max_restarts_per_hour=%d "
        "cooldown=%ds restart_cmd=%r telegram=%s",
        cfg.health_url,
        cfg.check_interval_sec,
        cfg.fail_threshold,
        cfg.max_restarts_per_hour,
        cfg.cooldown_sec,
        cfg.restart_cmd,
        "configured" if cfg.telegram_token else "not configured",
    )

    try:
        Watchdog(cfg).run()
    except KeyboardInterrupt:
        log.info("Watchdog stopped by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
