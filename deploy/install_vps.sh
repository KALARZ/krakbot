#!/usr/bin/env bash
# Idempotent VPS installer: the README "Installazione sul VPS" steps in one command.
# Run as root FROM the checkout at /opt/krakbot (clone it there first, see README):
#
#   sudo CAPITAL=5000 /opt/krakbot/deploy/install_vps.sh --with-shadow
#
# * the 7-day DRY_RUN runs WITHOUT Kraken keys: the script refuses to start if .env contains any;
# * .env is created once from .env.example (chmod 600, owner krakbot) with only CAPITAL set; never overwritten;
# * installs and starts krakbot.service (DRY_RUN) + the daily backup timer; --with-shadow adds the hourly shadow;
# * never installs or starts krakbot-live.service.
set -euo pipefail

APP=/opt/krakbot
WITH_SHADOW=0
for arg in "$@"; do
  case "$arg" in
    --with-shadow) WITH_SHADOW=1 ;;
    *) echo "unknown argument: $arg" >&2; exit 1 ;;
  esac
done

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
[[ -f $APP/pyproject.toml && -d $APP/.git ]] || { echo "clone the repository to $APP first (README)" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
  || { echo "Python >= 3.12 required (python3 --version)" >&2; exit 1; }
if command -v timedatectl >/dev/null && [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" != "yes" ]]; then
  echo "WARNING: the clock is not NTP-synchronized (install chrony or enable systemd-timesyncd)" >&2
fi

id krakbot >/dev/null 2>&1 || useradd --system --home-dir "$APP" --shell /usr/sbin/nologin krakbot
chown -R krakbot:krakbot "$APP"

sudo -u krakbot python3 -m venv "$APP/.venv"
sudo -u krakbot "$APP/.venv/bin/pip" install --quiet --upgrade pip
sudo -u krakbot "$APP/.venv/bin/pip" install --quiet "$APP"
sudo -u krakbot mkdir -p "$APP/data" "$APP/data-shadow"

if [[ ! -f $APP/.env ]]; then
  : "${CAPITAL:?first install: set CAPITAL=<EUR>, e.g. sudo CAPITAL=5000 $0}"
  [[ $CAPITAL =~ ^[0-9]+(\.[0-9]{1,2})?$ ]] || { echo "CAPITAL must be a number of EUR, e.g. 5000" >&2; exit 1; }
  sudo -u krakbot install -m 600 "$APP/.env.example" "$APP/.env"
  sudo -u krakbot sed -i "s/^CAPITAL=.*/CAPITAL=${CAPITAL}/" "$APP/.env"
  echo "created $APP/.env (CAPITAL=$CAPITAL, no Kraken keys). Optional: TELEGRAM_*, HEALTHCHECK_URL."
fi
chown krakbot:krakbot "$APP/.env"
chmod 600 "$APP/.env"
if grep -Eq '^KRAKEN_API_(KEY|SECRET)=.+' "$APP/.env"; then
  echo "REFUSED: $APP/.env contains Kraken API keys. The 7-day DRY_RUN runs without keys: empty them first." >&2
  exit 2
fi

# validates config.toml + .env (permissions, ranges, pre-registered values) and applies DB migrations
sudo -u krakbot "$APP/.venv/bin/krakbot" --config "$APP/config.toml" --env "$APP/.env" status >/dev/null

units=(krakbot.service krakbot-backup.service krakbot-backup.timer)
[[ $WITH_SHADOW -eq 1 ]] && units+=(krakbot-shadow.service)
for u in "${units[@]}"; do
  install -m 644 "$APP/deploy/systemd/$u" "/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl enable --now krakbot.service krakbot-backup.timer
[[ $WITH_SHADOW -eq 1 ]] && systemctl enable --now krakbot-shadow.service

sleep 15
systemctl --no-pager --lines=0 status krakbot.service || true
[[ $WITH_SHADOW -eq 1 ]] && { systemctl --no-pager --lines=0 status krakbot-shadow.service || true; }
echo
echo "DRY_RUN started. The 7 days count from the first COMPLETE UTC day. Daily check: docs/runbook.md section 1."
