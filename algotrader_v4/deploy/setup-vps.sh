#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# AlgoTrader Pro v4 — one-shot bootstrap for a fresh Ubuntu 22.04/24.04 VPS
#
# Usage (SSH into your VPS as root or sudo user):
#
#   # Option A — fresh server, clone from GitHub:
#   bash <(curl -sL https://raw.githubusercontent.com/kinseyecommerce-alt/maran/main/algotrader_v4/deploy/setup-vps.sh) yourdomain.com
#
#   # Option B — already cloned the repo:
#   sudo bash algotrader_v4/deploy/setup-vps.sh yourdomain.com
#
#   # Option C — use server IP directly (no domain, no TLS):
#   sudo bash algotrader_v4/deploy/setup-vps.sh 1.2.3.4
#
# Args:
#   $1 — domain name OR public IP of the VPS
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

DOMAIN="${1:-}"
APP_USER="algotrader"
APP_DIR="/opt/algotrader"
BRANCH="main"
PYTHON="python3.11"
REPO_URL="https://github.com/kinseyecommerce-alt/maran.git"

# ── 0. Sanity ─────────────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || { echo "Run as root: sudo bash $0 <domain-or-ip>"; exit 1; }
[[ -n "$DOMAIN" ]] || { echo "Usage: $0 <domain-or-ip>"; exit 1; }

IS_DOMAIN=false
[[ "$DOMAIN" =~ ^[a-zA-Z].*\.[a-zA-Z]{2,}$ ]] && IS_DOMAIN=true

# Detect the VPS public IP (for SEBI whitelist)
VPS_PUBLIC_IP=$(curl -sf https://api.ipify.org 2>/dev/null || curl -sf https://ifconfig.me 2>/dev/null || echo "")

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  AlgoTrader Pro v4 — VPS Setup"
echo "  Domain/IP  : $DOMAIN"
echo "  TLS        : $IS_DOMAIN"
echo "  Public IP  : ${VPS_PUBLIC_IP:-could not detect}"
echo "  App dir    : $APP_DIR"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ── 1. System packages ────────────────────────────────────────────────────────
echo "▶ Installing system packages…"
apt-get update -qq
apt-get install -y --no-install-recommends \
    git python3.11 python3.11-venv python3-pip \
    nginx ufw logrotate curl wget ca-certificates \
    build-essential libssl-dev libffi-dev python3.11-dev

$IS_DOMAIN && apt-get install -y --no-install-recommends certbot python3-certbot-nginx

# ── 2. App user ───────────────────────────────────────────────────────────────
id -u "$APP_USER" &>/dev/null || useradd -m -s /bin/bash "$APP_USER"

# ── 3. Clone / update repo ────────────────────────────────────────────────────
if [[ -d "$APP_DIR/.git" ]]; then
    echo "▶ Pulling latest from $BRANCH…"
    sudo -u "$APP_USER" git -C "$APP_DIR" fetch origin
    sudo -u "$APP_USER" git -C "$APP_DIR" checkout "$BRANCH"
    sudo -u "$APP_USER" git -C "$APP_DIR" pull origin "$BRANCH"
else
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    SRC_DIR="$(dirname "$SCRIPT_DIR")"   # algotrader_v4/
    if [[ -f "$SRC_DIR/main.py" ]]; then
        echo "▶ Linking existing repo → $APP_DIR"
        ln -sfn "$SRC_DIR" "$APP_DIR"
    else
        echo "▶ Cloning from GitHub…"
        git clone --depth=1 -b "$BRANCH" "$REPO_URL" /tmp/maran_clone
        # The app lives in the algotrader_v4/ subfolder
        mv /tmp/maran_clone/algotrader_v4 "$APP_DIR"
        rm -rf /tmp/maran_clone
    fi
    chown -R "$APP_USER":"$APP_USER" "$(readlink -f "$APP_DIR")"
fi

# ── 4. Python venv + deps ─────────────────────────────────────────────────────
echo "▶ Creating Python venv and installing dependencies…"
VENV="$APP_DIR/venv"
sudo -u "$APP_USER" $PYTHON -m venv "$VENV"
sudo -u "$APP_USER" "$VENV/bin/pip" install -q --upgrade pip wheel
# Pin setuptools<60 first — required to build ta==0.11.0
sudo -u "$APP_USER" "$VENV/bin/pip" install -q "setuptools<60"
sudo -u "$APP_USER" "$VENV/bin/pip" install -q -r "$APP_DIR/requirements.txt"
# Performance: faster uvloop event loop
sudo -u "$APP_USER" "$VENV/bin/pip" install -q uvloop httptools 2>/dev/null || true

# ── 5. .env ───────────────────────────────────────────────────────────────────
ENV_FILE="$APP_DIR/.env"
if [[ ! -f "$ENV_FILE" ]]; then
    cp "$APP_DIR/.env.example" "$ENV_FILE"

    # Auto-fill generated secrets
    JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    API_KEY=$(python3 -c "import secrets; print(secrets.token_hex(24))")
    KS_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(16))")

    # bcrypt hash for default password admin@786
    ADMIN_HASH=$(python3 -c "import bcrypt; print(bcrypt.hashpw(b'admin@786', bcrypt.gensalt(12)).decode())" 2>/dev/null \
        || "$VENV/bin/python" -c "import bcrypt; print(bcrypt.hashpw(b'admin@786', bcrypt.gensalt(12)).decode())")

    sed -i "s|^JWT_SECRET_KEY=.*|JWT_SECRET_KEY=$JWT_SECRET|" "$ENV_FILE"
    sed -i "s|^API_KEY=.*|API_KEY=$API_KEY|" "$ENV_FILE"
    sed -i "s|^KILL_SWITCH_RESET_SECRET=.*|KILL_SWITCH_RESET_SECRET=$KS_SECRET|" "$ENV_FILE"
    sed -i "s|^ADMIN_PASSWORD_HASH=.*|ADMIN_PASSWORD_HASH=$ADMIN_HASH|" "$ENV_FILE"

    # Auto-set KITE_REDIRECT_URL
    if $IS_DOMAIN; then
        sed -i "s|^KITE_REDIRECT_URL=.*|KITE_REDIRECT_URL=https://$DOMAIN/auth/kite/callback|" "$ENV_FILE"
    else
        sed -i "s|^KITE_REDIRECT_URL=.*|KITE_REDIRECT_URL=http://$DOMAIN/auth/kite/callback|" "$ENV_FILE"
    fi

    # Auto-set SEBI_WHITELISTED_IPS to the VPS public IP
    if [[ -n "$VPS_PUBLIC_IP" ]]; then
        if grep -q "^SEBI_WHITELISTED_IPS=" "$ENV_FILE"; then
            sed -i "s|^SEBI_WHITELISTED_IPS=.*|SEBI_WHITELISTED_IPS=$VPS_PUBLIC_IP|" "$ENV_FILE"
        else
            echo "SEBI_WHITELISTED_IPS=$VPS_PUBLIC_IP" >> "$ENV_FILE"
        fi
    fi

    chmod 600 "$ENV_FILE"
    chown "$APP_USER":"$APP_USER" "$ENV_FILE"

    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ACTION REQUIRED — edit secrets in .env:"
    echo "  sudo nano $ENV_FILE"
    echo ""
    echo "  Required fields:"
    echo "    KITE_API_KEY      your Zerodha API key"
    echo "    KITE_API_SECRET   your Zerodha API secret"
    echo "    ANTHROPIC_API_KEY your Anthropic key"
    echo "    TRADING_MODE      PAPER or LIVE"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
else
    echo "▶ .env already exists — skipping auto-fill"
fi

# ── 6. Logs directory ─────────────────────────────────────────────────────────
mkdir -p /var/log/algotrader "$APP_DIR/logs"
chown "$APP_USER":"$APP_USER" /var/log/algotrader "$APP_DIR/logs"

# ── 7. systemd service ────────────────────────────────────────────────────────
echo "▶ Installing systemd service…"
cat > /etc/systemd/system/algotrader.service <<EOF
[Unit]
Description=AlgoTrader Pro v4
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$VENV/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1 --log-level warning
Restart=always
RestartSec=5
StandardOutput=append:/var/log/algotrader/app.log
StandardError=append:/var/log/algotrader/app.log
TimeoutStartSec=60
TimeoutStopSec=30
KillSignal=SIGTERM

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable algotrader

# ── 8. nginx reverse proxy ────────────────────────────────────────────────────
echo "▶ Configuring nginx…"
cat > /etc/nginx/sites-available/algotrader <<NGINX
upstream algotrader_upstream {
    server 127.0.0.1:8000;
    keepalive 32;
}

server {
    listen 80;
    server_name $DOMAIN;

    # WebSocket + long-poll support
    location / {
        proxy_pass         http://algotrader_upstream;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade \$http_upgrade;
        proxy_set_header   Connection "upgrade";
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 300s;
        proxy_connect_timeout 10s;
    }
}
NGINX

ln -sfn /etc/nginx/sites-available/algotrader /etc/nginx/sites-enabled/algotrader
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# ── 9. TLS (Let's Encrypt) ────────────────────────────────────────────────────
if $IS_DOMAIN; then
    echo "▶ Requesting TLS certificate for $DOMAIN…"
    certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
        --email "admin@$DOMAIN" --redirect || \
        echo "⚠ TLS setup failed — check DNS is pointing to this server, then run: sudo certbot --nginx -d $DOMAIN"
    systemctl enable certbot.timer 2>/dev/null || true
fi

# ── 10. UFW firewall ──────────────────────────────────────────────────────────
echo "▶ Configuring firewall…"
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

# ── 11. Log rotation ──────────────────────────────────────────────────────────
cat > /etc/logrotate.d/algotrader <<LR
/var/log/algotrader/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
LR

# ── 12. Start the service ─────────────────────────────────────────────────────
echo "▶ Starting algotrader service…"
systemctl start algotrader
sleep 4

if systemctl is-active --quiet algotrader; then
    STATUS="RUNNING ✓"
else
    STATUS="FAILED ✗"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  AlgoTrader Pro v4 — Setup Complete"
echo "  Service   : $STATUS"
$IS_DOMAIN && echo "  URL       : https://$DOMAIN" || echo "  URL       : http://$DOMAIN"
echo "  Login     : admin / admin@786"
[[ -n "$VPS_PUBLIC_IP" ]] && echo "  Static IP : $VPS_PUBLIC_IP  ← register this with SEBI & Zerodha"
echo ""
echo "  Commands:"
echo "    sudo systemctl status algotrader"
echo "    sudo journalctl -fu algotrader"
echo "    sudo nano $ENV_FILE   (then: sudo systemctl restart algotrader)"
echo ""
echo "  Next steps:"
echo "  1. sudo nano $ENV_FILE — add KITE_API_KEY, KITE_API_SECRET, ANTHROPIC_API_KEY"
echo "  2. Set TRADING_MODE=LIVE when ready"
echo "  3. Register IP $VPS_PUBLIC_IP in Zerodha Console → API → Apps"
echo "  4. sudo systemctl restart algotrader"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

[[ "$STATUS" == *FAILED* ]] && { echo ""; journalctl -u algotrader -n 30 --no-pager; exit 1; }
