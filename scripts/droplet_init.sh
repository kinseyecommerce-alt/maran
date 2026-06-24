#!/bin/bash
# AlgoTrader Pro v4 — DigitalOcean Droplet Bootstrap
# Paste this into "User Data" when creating the Droplet.
# Runs once on first boot as root.
set -euo pipefail
LOG=/var/log/algotrader_init.log
exec > >(tee -a "$LOG") 2>&1
echo "=== AlgoTrader bootstrap started: $(date) ==="

# ── 1. System update ─────────────────────────────────────────────────────────
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y

# ── 2. Core dependencies ──────────────────────────────────────────────────────
apt-get install -y \
  python3.11 python3.11-venv python3-pip \
  git curl wget ufw unzip \
  build-essential libssl-dev libffi-dev

# ── 3. Playwright / Chromium system libs (for TOTP auto-login) ───────────────
apt-get install -y \
  libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
  libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 \
  libgbm1 libasound2 libpangocairo-1.0-0 libgtk-3-0 \
  fonts-liberation libappindicator3-1 xdg-utils

# ── 4. Clone repo ─────────────────────────────────────────────────────────────
cd /opt
git clone https://github.com/kinseyecommerce-alt/maran.git algotrader
cd /opt/algotrader/algotrader_v4

# ── 5. Python virtual environment + deps ─────────────────────────────────────
python3.11 -m venv /opt/algotrader/venv
source /opt/algotrader/venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# ── 6. Playwright browser install ────────────────────────────────────────────
playwright install chromium
playwright install-deps chromium

# ── 7. Create .env from example (fill credentials manually after boot) ────────
if [ ! -f /opt/algotrader/algotrader_v4/.env ]; then
  cp /opt/algotrader/algotrader_v4/.env.example \
     /opt/algotrader/algotrader_v4/.env
  echo ">>> .env created from .env.example — fill in credentials before starting"
fi

# ── 8. Logs directory ─────────────────────────────────────────────────────────
mkdir -p /opt/algotrader/algotrader_v4/logs

# ── 9. systemd service ────────────────────────────────────────────────────────
cat > /etc/systemd/system/algotrader.service << 'EOF'
[Unit]
Description=AlgoTrader Pro v4
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/algotrader/algotrader_v4
EnvironmentFile=/opt/algotrader/algotrader_v4/.env
ExecStart=/opt/algotrader/venv/bin/python main.py
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=algotrader

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable algotrader
# NOTE: service is enabled but NOT started yet — fill .env first

# ── 10. Firewall ──────────────────────────────────────────────────────────────
ufw allow OpenSSH
ufw allow 8000/tcp   # AlgoTrader API + dashboard
ufw --force enable

# ── 11. Log rotation ──────────────────────────────────────────────────────────
cat > /etc/logrotate.d/algotrader << 'EOF'
/opt/algotrader/algotrader_v4/logs/*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
    copytruncate
}
EOF

echo ""
echo "=== Bootstrap complete: $(date) ==="
echo ""
echo "NEXT STEPS:"
echo "  1. SSH in:   ssh root@$(curl -s ifconfig.me)"
echo "  2. Fill .env: nano /opt/algotrader/algotrader_v4/.env"
echo "  3. Start:    systemctl start algotrader"
echo "  4. Status:   systemctl status algotrader"
echo "  5. Logs:     journalctl -u algotrader -f"
echo "  6. Readiness: curl http://localhost:8000/readiness"
