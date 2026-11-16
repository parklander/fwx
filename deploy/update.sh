#!/usr/bin/env bash
# Pull the latest code and restart. Run as root on the droplet.
set -euo pipefail
APP=/opt/cwfis-forecast
sudo -u fwx git -C "$APP" pull --ff-only
sudo -u fwx "$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"
systemctl restart cwfis-forecast
