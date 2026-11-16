#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu 24.04 droplet (the smallest size is plenty). Run as root:
#   bash setup.sh <git-repo-url> [domain]
# With no domain, the droplet's IP is used as <ip>.sslip.io, which still gets a real HTTPS certificate.
set -euo pipefail
REPO=${1:?usage: bash setup.sh <git-repo-url> [domain]}
DOMAIN=${2:-}
APP=/opt/cwfis-forecast
if [ -z "$DOMAIN" ]; then
  IP=$(curl -fsS https://ipv4.icanhazip.com)
  DOMAIN="${IP//./-}.sslip.io"
fi

apt-get update
apt-get install -y python3-venv git ufw debian-keyring debian-archive-keyring apt-transport-https curl gnupg
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update && apt-get install -y caddy
fi
ufw allow OpenSSH && ufw allow 80,443/tcp && ufw --force enable

id -u fwx >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/fwx --shell /usr/sbin/nologin fwx
[ -d "$APP/.git" ] || git clone "$REPO" "$APP"
chown -R fwx:fwx "$APP"
sudo -u fwx python3 -m venv "$APP/.venv"
sudo -u fwx "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

install -m 644 "$APP/deploy/cwfis-forecast.service" /etc/systemd/system/
sed "s/{DOMAIN}/$DOMAIN/" "$APP/deploy/Caddyfile" > /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable --now cwfis-forecast
systemctl restart caddy
echo "Done: https://$DOMAIN   (logs: journalctl -u cwfis-forecast -f)"
