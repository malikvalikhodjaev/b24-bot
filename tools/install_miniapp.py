"""Narrow installer for four static assets and their public GET/HEAD routes.

Run on the named FOM dashboard host; build_miniapp_package.py embeds ASSETS.
No CRM credentials, authentication settings or other services are changed.
"""
import base64
from datetime import datetime
import json
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import urlopen

ASSETS = {}
PUBLIC = ('index.html', 'app.js', 'style.css', 'reference.json')

METHOD = '''    def serve_bot_form(self, request_path: str) -> bool:
        # Four public presentation assets only. Drafts are submitted to Telegram,
        # whose bot checks the sender and active draft before any CRM write.
        assets = {'/bot-form/': ('index.html', 'text/html; charset=utf-8'),
                  '/bot-form/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                  '/bot-form/style.css': ('style.css', 'text/css; charset=utf-8'),
                  '/bot-form/reference.json': ('reference.json', 'application/json; charset=utf-8')}
        asset = assets.get(request_path)
        if asset is None:
            return False
        try:
            data = (ROOT_DIR / 'apps/bot_form' / asset[0]).read_bytes()
        except OSError:
            self.send_error(404)
            return True
        self.send_response(200)
        self.send_header('Content-Type', asset[1])
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        if self.command == 'GET':
            self.wfile.write(data)
        return True

'''


def patch_source(source):
    if '    def serve_bot_form(' in source:
        if METHOD not in source:
            raise RuntimeError('Existing bot form route differs; inspect it before deploying')
        return source
    anchor = '    def do_HEAD(self) -> None:\n'
    if source.count(anchor) != 1:
        raise RuntimeError('Expected one dashboard HEAD handler')
    source = source.replace(anchor, METHOD + anchor, 1)
    for name in ('HEAD', 'GET'):
        before = f'    def do_{name}(self) -> None:\n        request_path = urlsplit(self.path).path\n'
        if source.count(before) != 1:
            raise RuntimeError('Dashboard handler changed: ' + name)
        source = source.replace(before, before + '\n        if self.serve_bot_form(request_path):\n            return\n', 1)
    compile(source, 'dashboard_server.py', 'exec')
    return source


def safe_path(root, relative):
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise RuntimeError('Target escapes the FOM dashboard repository')
    return path


def main():
    root = Path('/home/malik/-fom-dashboards').resolve()
    if root != Path('/home/malik/-fom-dashboards') or not root.is_dir():
        raise RuntimeError('Expected the named FOM dashboard repository')
    if set(ASSETS) != set(PUBLIC):
        raise RuntimeError('Build the installer package with all four assets first')
    server = safe_path(root, 'scripts/dashboard_server.py')
    original = server.read_bytes()
    updated = patch_source(original.decode('utf-8')).encode('utf-8')
    files = {name: base64.b64decode(ASSETS[name], validate=True) for name in PUBLIC}
    previous = {}
    for name in PUBLIC:
        path = safe_path(root, 'apps/bot_form/' + name)
        previous[name] = path.read_bytes() if path.exists() else None
    backup = safe_path(root, 'logs/miniapp-backups/' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    backup.mkdir(parents=True)
    (backup / 'dashboard_server.py').write_bytes(original)
    for name, data in previous.items():
        if data is not None: (backup / name).write_bytes(data)
    try:
        for name, data in files.items():
            path = safe_path(root, 'apps/bot_form/' + name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        server.write_bytes(updated)
        subprocess.run(['systemctl', '--user', 'restart', 'fom-dashboard-server.service'], check=True)
        last = None
        for attempt in range(5):
            try:
                for name, data in files.items():
                    url = 'http://127.0.0.1:8768/bot-form/' + ('' if name == 'index.html' else name)
                    with urlopen(url, timeout=4) as response:
                        if response.status != 200 or response.read() != data:
                            raise RuntimeError('Asset verification failed: ' + name)
                try:
                    with urlopen('http://127.0.0.1:8768/api/admin/overview', timeout=4):
                        raise RuntimeError('Protected dashboard endpoint unexpectedly became public')
                except HTTPError as exc:
                    if exc.code != 401: raise
                last = None
                break
            except OSError as exc:
                last = exc
                time.sleep(1)
        if last is not None: raise last
        print(json.dumps({'ok': True, 'assets': 4, 'private_api': 401,
                          'backup': str(backup), 'url': 'https://fom-analytics.uz/bot-form/'}))
    except Exception:
        server.write_bytes(original)
        for name, data in previous.items():
            path = safe_path(root, 'apps/bot_form/' + name)
            if data is None:
                if path.exists(): path.unlink()
            else: path.write_bytes(data)
        subprocess.run(['systemctl', '--user', 'restart', 'fom-dashboard-server.service'], check=False)
        raise


if __name__ == '__main__': main()
