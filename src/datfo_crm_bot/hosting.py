"""The migrated laptop keeps read-only checks, but cannot become a second poller."""
import json
import os

from .config import ConfigError, ROOT


def assert_polling_location():
    if os.environ.get('FOM_BOT_EXECUTION') == 'server':
        return
    marker = ROOT/'data/server-hosting.json'
    if not marker.exists():
        return
    try:
        value = json.loads(marker.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        raise ConfigError('Не удалось проверить место запуска бота. Проверьте data/server-hosting.json.') from None
    if value.get('active') is True:
        raise ConfigError('Бот работает на сервере. Локальный запуск отключён, чтобы не запустить второй обработчик Telegram.')
