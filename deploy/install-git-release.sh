#!/usr/bin/env bash
# Prepare a verified Git release. This script never starts a Telegram poller.
set -euo pipefail
umask 077
if [[ "$(id -un)" != 'malik' || $# -ne 1 ]]; then
  printf '%s\n' 'Run as malik with one absolute path to the bot Git bundle.' >&2
  exit 1
fi
bundle_path=$(realpath "$1")
bot_root=/home/malik/datfo-crm-bot
if [[ ! -f "$bundle_path" || "$bundle_path" != /home/malik/.local/share/fom-bot-deploy/incoming/*.bundle ]]; then
  printf '%s\n' 'Bundle must be in the dedicated bot incoming directory.' >&2
  exit 1
fi
if [[ -e "$bot_root" || -L "$bot_root" ]]; then
  printf '%s\n' 'Existing bot checkout preserved. Use the update procedure in SERVER_RUNTIME.md.' >&2
  exit 1
fi
git clone --branch codex/fom-bot-runtime "$bundle_path" "$bot_root"
chmod 700 "$bot_root"
git -C "$bot_root" remote set-url origin https://github.com/malikvalikhodjaev/datfo-strategy.git
python_path=$(/usr/bin/python3 -c 'import json,pathlib; print(json.loads((pathlib.Path.home()/"fom-bot-runtime-installed.json").read_text())["python"])')
case "$python_path" in /home/malik/.local/share/fom-bot-runtime/python/*/bin/python3.12) ;; *) exit 1 ;; esac
"$python_path" -c 'import sys; from zoneinfo import ZoneInfo; assert sys.version_info >= (3, 11); ZoneInfo("Asia/Tashkent")'
"$python_path" -m venv "$bot_root/.venv"
mkdir -m 700 "$bot_root/data"
cd "$bot_root"
PYTHONPATH=src:tests .venv/bin/python -m unittest discover -s tests -q >data/server-tests.log 2>&1
unit_path=/home/malik/.config/systemd/user/fom-bitrix-bot.service
if [[ -e "$unit_path" ]]; then
  printf '%s\n' 'Existing service unit preserved.' >&2
  exit 1
fi
install -m 600 deploy/fom-bitrix-bot.service "$unit_path"
systemd-analyze --user verify "$unit_path"
systemctl --user daemon-reload
printf '%s\n' 'Git release and Python prepared; tests passed. Service has NOT been started.'
