"""Narrow same-origin dashboard bridge to the bot's authenticated loopback lookup."""
from datetime import datetime
import json
from pathlib import Path
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

METHOD = '''    def serve_bot_lookup(self) -> None:
        # CRM credentials stay in the bot service; the body contains its launch token.
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        origin = self.headers.get('Origin', '')
        if origin and origin != 'https://fom-analytics.uz':
            self.send_json({'error': 'Откройте форму через меню бота.'}, status=403)
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            length = 0
        if not 0 < length <= 2048:
            self.send_json({'error': 'Запрос не распознан.'}, status=413)
            return
        if self.headers.get_content_type() != 'application/json':
            self.send_json({'error': 'Запрос не распознан.'}, status=415)
            return
        self.connection.settimeout(5)
        body = self.rfile.read(length)
        try:
            request = Request('http://127.0.0.1:18772/lookup', data=body,
                              headers={'Content-Type': 'application/json'}, method='POST')
            try:
                response = urlopen(request, timeout=42)
            except HTTPError as exc:
                response = exc
            with response:
                status = response.code
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError('lookup response too large')
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError('invalid lookup response')
        except (OSError, ValueError):
            self.send_json({'error': 'Б24 пока не ответил. Повторите поиск чуть позже.'}, status=503)
            return
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(raw)

'''


def patch_source(source):
    if '    def serve_bot_lookup(' in source:
        if METHOD not in source:
            raise RuntimeError('Existing lookup bridge differs; inspect before deploying')
        return source
    log_anchor = '    def log_message(self, format: str, *args) -> None:\n'
    if log_anchor in source:
        if source.count(log_anchor) != 1:
            raise RuntimeError('Dashboard logger changed')
        # Form URLs carry bearer tokens; do not put them in the dashboard journal.
        source = source.replace(log_anchor, log_anchor+"        if urlsplit(self.path).path.startswith('/bot-form/'):\n            return\n",1)
    anchor = '    def do_POST(self) -> None:\n        request_path = urlsplit(self.path).path\n'
    if source.count(anchor) != 1:
        raise RuntimeError('Dashboard POST handler changed')
    source = source.replace(anchor, METHOD+anchor+"        if request_path == '/bot-form/api/lookup':\n            self.serve_bot_lookup()\n            return\n", 1)
    compile(source,'dashboard_server.py','exec')
    return source


def main():
    root = Path('/home/malik/-fom-dashboards').resolve()
    if root != Path('/home/malik/-fom-dashboards') or not root.is_dir():
        raise RuntimeError('Expected named dashboard checkout')
    server = root/'scripts/dashboard_server.py'
    original = server.read_bytes()
    updated = patch_source(original.decode('utf-8')).encode('utf-8')
    backup = root/'logs/miniapp-backups'/datetime.now().strftime('%Y%m%d-%H%M%S-%f-lookup')
    backup.mkdir(parents=True)
    (backup/'dashboard_server.py').write_bytes(original)
    try:
        server.write_bytes(updated)
        subprocess.run(['systemctl','--user','restart','fom-dashboard-server.service'],check=True)
        for attempt in range(6):
            try:
                request = Request('http://127.0.0.1:8768/bot-form/api/lookup',data=b'{"token":"invalid","mode":"deal","inn":"300000101"}',
                    headers={'Content-Type':'application/json'},method='POST')
                try:
                    with urlopen(request,timeout=4):
                        raise RuntimeError('Unauthenticated lookup became public')
                except HTTPError as exc:
                    if exc.code != 401:
                        raise
                try:
                    with urlopen('http://127.0.0.1:8768/api/admin/overview',timeout=4):
                        raise RuntimeError('Protected admin API became public')
                except HTTPError as exc:
                    if exc.code != 401:
                        raise
                break
            except OSError:
                if attempt == 5:
                    raise
                time.sleep(1)
        print(json.dumps({'ok':True,'lookup_without_token':401,'admin_without_session':401,'backup':str(backup)}))
    except Exception:
        server.write_bytes(original)
        subprocess.run(['systemctl','--user','restart','fom-dashboard-server.service'],check=False)
        raise


if __name__ == '__main__':
    main()
