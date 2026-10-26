#!/usr/bin/env bash
# Deploy Suryadeals Power Engine on the Ubuntu server (nginx + certbot already installed).
# Usage on the server:  ENGINE_PASSWORD=... ENGINE_SIGNATURE="..." bash deploy.sh [domain]
# ENGINE_PASSWORD / ENGINE_SIGNATURE are only needed on the first run; later runs keep them.
set -euo pipefail

DOMAIN="${1:-searchengine.suryadeals.com}"
APP_DIR=/opt/power-engine
DATA_DIR=/opt/power-engine-data
ENV_FILE=/etc/power-engine.env
HTPASSWD=/etc/nginx/.htpasswd-power-engine
REPO=https://github.com/somanisuryateja/Suryadeals-Power-Engine.git
PORT=8010

echo "[1/5] Code"
if [ -d "$APP_DIR/.git" ]; then
  sudo git -C "$APP_DIR" fetch -q origin main && sudo git -C "$APP_DIR" reset -q --hard origin/main
else
  sudo git clone -q "$REPO" "$APP_DIR"
fi
sudo chown -R ubuntu:ubuntu "$APP_DIR"
sudo mkdir -p "$DATA_DIR" && sudo chown ubuntu:ubuntu "$DATA_DIR"

echo "[2/5] Python env"
[ -d "$APP_DIR/venv" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
(cd "$APP_DIR" && venv/bin/python -m app.main --selfcheck && venv/bin/python engine/daily_jobs.py --selfcheck)

echo "[3/5] Settings and password"
if [ ! -f "$ENV_FILE" ]; then
  printf 'ENGINE_DB=%s/engine.db\nENGINE_SIGNATURE=%s\n' "$DATA_DIR" "${ENGINE_SIGNATURE:-}" | sudo tee "$ENV_FILE" >/dev/null
  sudo chmod 600 "$ENV_FILE"
fi
if [ ! -f "$HTPASSWD" ]; then
  : "${ENGINE_PASSWORD:?set ENGINE_PASSWORD on the first deploy}"
  printf 'surya:%s\n' "$(openssl passwd -apr1 "$ENGINE_PASSWORD")" | sudo tee "$HTPASSWD" >/dev/null
  sudo chown root:www-data "$HTPASSWD" && sudo chmod 640 "$HTPASSWD"
fi

echo "[4/5] Service"
sudo tee /etc/systemd/system/power-engine.service >/dev/null <<EOF
[Unit]
Description=Suryadeals Power Engine (job feed)
After=network.target

[Service]
User=ubuntu
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $PORT
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable -q power-engine
sudo systemctl restart power-engine

echo "[5/5] nginx + HTTPS"
if [ ! -f "/etc/nginx/sites-available/$DOMAIN" ]; then
  sudo tee "/etc/nginx/sites-available/$DOMAIN" >/dev/null <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    auth_basic "Power Engine";
    auth_basic_user_file $HTPASSWD;
    add_header X-Robots-Tag "noindex, nofollow" always;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
  sudo ln -sf "/etc/nginx/sites-available/$DOMAIN" /etc/nginx/sites-enabled/
  sudo nginx -t && sudo systemctl reload nginx
  sudo certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect -m surya76755416@gmail.com
fi
sudo nginx -t -q && sudo systemctl reload nginx

sleep 2
curl -fsS "http://127.0.0.1:$PORT/api/health" && echo && echo "Live: https://$DOMAIN/"
