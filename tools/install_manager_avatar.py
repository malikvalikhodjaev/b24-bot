"""Install a checked, versioned manager avatar of @fom_bitrix_bot."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import socket
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from datfo_crm_bot.api import Telegram, RemoteError
from datfo_crm_bot.config import load_env, connection_from_env


def latest_photo(telegram, bot_id):
    result = telegram.call('getUserProfilePhotos', {'user_id': bot_id, 'limit': 1})
    rows = result.get('photos', []) if isinstance(result, dict) else []
    if not rows:
        return None
    return max(rows[0], key=lambda photo: photo['width'] * photo['height'])


def upload(telegram, image, filename):
    boundary = 'FomAvatar' + uuid4().hex
    profile = json.dumps({'type': 'static', 'photo': 'attach://avatar'})
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="photo"\r\n\r\n{profile}\r\n'
        f'--{boundary}\r\nContent-Disposition: form-data; name="avatar"; '
        f'filename="{filename}"\r\nContent-Type: image/jpeg\r\n\r\n'
    ).encode('ascii') + image + f'\r\n--{boundary}--\r\n'.encode('ascii')
    request = Request(telegram.base + 'setMyProfilePhoto', data=body, method='POST',
                      headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
    try:
        with urlopen(request, timeout=40) as response:
            result = json.loads(response.read())
    except HTTPError as error:
        raise RemoteError('TELEGRAM_HTTP_' + str(error.code)) from None
    except (URLError, socket.timeout, TimeoutError, OSError):
        raise RemoteError('CONNECTION_ERROR', uncertain=True) from None
    except (ValueError, UnicodeError):
        raise RemoteError('INVALID_JSON', uncertain=True) from None
    if not isinstance(result, dict) or result.get('ok') is not True or result.get('result') is not True:
        code = result.get('error_code', 'ERROR') if isinstance(result, dict) else 'INVALID_RESPONSE'
        raise RemoteError('TELEGRAM_' + str(code))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', type=int, choices=[3, 4], default=3)
    version = parser.parse_args().version
    image_path = ROOT / f'assets/fom-bot-avatar-v{version}.jpg'
    result_path = ROOT / f'data/avatar-installation-v{version}.json'
    load_env(ROOT / '.env')
    token, _ = connection_from_env()
    if not token:
        raise SystemExit('Telegram token is missing')
    telegram = Telegram(token)
    identity = telegram.call('getMe')
    if identity.get('username') != 'fom_bitrix_bot' or identity.get('is_bot') is not True:
        raise SystemExit('Unexpected bot identity; no profile changed')
    image = image_path.read_bytes()
    if not image.startswith(b'\xff\xd8'):
        raise SystemExit('Expected JPEG avatar')
    digest = hashlib.sha256(image).hexdigest()
    previous = latest_photo(telegram, identity['id'])
    if result_path.exists():
        saved = json.loads(result_path.read_text(encoding='utf-8'))
        if saved.get('sha256') == digest and previous and saved.get('file_unique_id') == previous['file_unique_id']:
            print(json.dumps({'installed': True, 'already_current': True, 'bot': identity['username']}))
            return
        raise SystemExit('An installation record already exists for this version; no repeated upload')
    upload(telegram, image, image_path.name)
    current = latest_photo(telegram, identity['id'])
    if not current or previous and current['file_unique_id'] == previous['file_unique_id']:
        raise SystemExit('Upload accepted, but profile change is not confirmed; do not upload again')
    result = {
        'at': datetime.now(ZoneInfo('Asia/Tashkent')).isoformat(),
        'bot': identity['username'], 'installed': True, 'version': version, 'text': 'FOM',
        'symbol': 'manager in a suit inside a speech bubble' if version == 3 else 'manager inside a red speech bubble, red tie, three red FOM tiles',
        'file_unique_id': current['file_unique_id'],
        'previous_file_unique_id': previous['file_unique_id'] if previous else None,
        'width': current['width'], 'height': current['height'],
        'asset': str(image_path.relative_to(ROOT)).replace('\\', '/'), 'sha256': digest,
        'source': f'assets/fom-bot-avatar-v{version-1}.png', 'generator': 'built-in image_gen',
    }
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except RemoteError as error:
        raise SystemExit(error.code) from None
