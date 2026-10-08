#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ "$(id -un)" != "malik" ]]; then
  printf '%s\n' 'Run as the existing malik user.' >&2
  exit 1
fi
bot_archive=/home/malik/fom-bitrix-bot-20261006-v7.tar.gz
bot_root=/home/malik/datfo-crm-bot
bot_unit=/home/malik/.config/systemd/user/fom-bitrix-bot.service
if [[ ! -f "$bot_archive" || -e "$bot_root" || -e "$bot_unit" ]]; then
  printf '%s\n' 'Archive missing or an existing bot checkout/unit needs review. Existing files preserved.' >&2
  exit 1
fi
python3 -c 'import sys; from zoneinfo import ZoneInfo; assert sys.version_info >= (3, 11); ZoneInfo("Asia/Tashkent")'
mkdir -m 700 "$bot_root"
tar --extract --gzip --file "$bot_archive" --directory "$bot_root" --no-same-owner
chmod 600 "$bot_root/.env" "$bot_root/registration.json" "$bot_root/data/crm_bot.sqlite3"
cd "$bot_root"
python3 -m unittest discover -s tests -q
python3 run.py --sales-bot --check-online
mkdir -p /home/malik/.config/systemd/user
install -m 600 "$bot_root/deploy/fom-bitrix-bot.service" "$bot_unit"
systemctl --user daemon-reload
printf '%s\n' 'Prepared and checked. Service has not been started.'
printf '%s\n' 'Stop the verified laptop poller before enabling fom-bitrix-bot.service.'
printf '%s\n' 'User linger status:'
loginctl show-user malik --property=Linger --value
