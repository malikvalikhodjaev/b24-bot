"""Loopback read-only lookup service. Launch tokens authorize approved sales users."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import closing
import hmac
import json
from pathlib import Path
import sqlite3
import threading
import time

from .api import RemoteError
from .config import ROOT, connection_from_env, load_env
from .inn_lookup import InnCatalogue, ReadOnlyBitrix, inn_value
from .okb_crm import OkbSettings

PORT = 18772


class AccessDenied(ValueError):
    pass


def authorize(database, token, mode):
    if mode not in {'deal','support','pharmacy'} or not isinstance(token,str) or not 20 <= len(token) <= 100:
        raise AccessDenied('Откройте новую форму через меню бота.')
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro', uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        # Sender identity comes from the bot-issued token, never from a client-supplied user ID.
        for row in db.execute("SELECT key,value FROM settings WHERE key LIKE ?", ('webapp-launch:'+mode+':%',)):
            value = json.loads(row['value'])
            if value.get('used') is not False or not hmac.compare_digest(str(value.get('token','')), token):
                continue
            if value.get('issued_at') and time.time()-float(value['issued_at']) > 86400:
                raise AccessDenied('Форма устарела. Откройте новую через меню бота.')
            user = int(row['key'].rsplit(':',1)[1])
            break
        else:
            # A draft reopened with /deal or /request uses its own request token.
            table = 'fom_request_sessions' if mode == 'support' else 'sessions'
            user = None
            for row in db.execute('SELECT user_id,value FROM '+table):
                value = json.loads(row['value'])
                expected_mode = value.get('form_mode') or ('deal' if value.get('sales_intake') else 'pharmacy')
                if (value.get('step') in {'web_form','sales_bulk_input','okb_bulk_input'} and expected_mode == mode
                        and hmac.compare_digest(str(value.get('request_id','')), token)):
                    user = row['user_id']
                    break
            if user is None:
                raise AccessDenied('Эта форма закрыта. Откройте новую через меню бота.')
        member = db.execute('''SELECT 1 FROM registrations r JOIN fom_request_members m
            ON m.telegram_id=r.telegram_id WHERE r.telegram_id=? AND r.status='approved' AND m.role='fom_sales' ''', (user,)).fetchone()
        if not member:
            raise AccessDenied('Поиск доступен подтверждённым менеджерам продаж.')
        return user


class LookupService:
    def __init__(self, database, catalogue):
        self.database, self.catalogue = database, catalogue
        self.lock = threading.Lock()
        self.cache, self.last = {}, {}
        self.slots = threading.BoundedSemaphore(4)

    def lookup(self, data):
        if not isinstance(data,dict):
            raise ValueError('Форма не распознана.')
        user = authorize(self.database,data.get('token'),data.get('mode'))
        inn = inn_value(data.get('inn'))
        with self.lock:
            now = time.monotonic()
            if now - self.last.get(user, -10) < 1:
                raise ValueError('Подождите секунду и повторите поиск.')
            self.last[user] = now
            self.cache = {key:value for key,value in self.cache.items() if now-value[0] <= 60}
            cached = self.cache.get(inn)
            if cached:
                return {'companies':cached[1]}
        if not self.slots.acquire(blocking=False):
            raise ValueError('Сейчас идут другие запросы. Повторите поиск чуть позже.')
        try:
            companies = self.catalogue.lookup(inn)
        finally:
            self.slots.release()
        # Authorization is checked again if access was revoked while Bitrix was responding.
        authorize(self.database,data.get('token'),data.get('mode'))
        with self.lock:
            self.cache[inn] = (time.monotonic(),companies)
        return {'companies':companies}


def handler(service):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # No bearer tokens, INNs, raw exceptions or CRM URLs in access logs.

        def reply(self, status, value):
            data = json.dumps(value,ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.reply(200 if self.path == '/health' else 404, {'ok':self.path == '/health'})

        def do_POST(self):
            if self.path != '/lookup':
                self.reply(404, {'error':'Маршрут недоступен.'})
                return
            try:
                length = int(self.headers.get('Content-Length','0'))
                if not 0 < length <= 2048:
                    self.reply(413, {'error':'Запрос слишком большой.'})
                    return
                self.connection.settimeout(5)
                data = json.loads(self.rfile.read(length))
                self.reply(200, service.lookup(data))
            except AccessDenied as exc:
                self.reply(401, {'error':str(exc)})
            except (ValueError, json.JSONDecodeError) as exc:
                self.reply(400, {'error':str(exc) if isinstance(exc,ValueError) and not isinstance(exc,json.JSONDecodeError) else 'Запрос не распознан.'})
            except (RemoteError, OSError, sqlite3.Error):
                self.reply(503, {'error':'Б24 пока не ответил. Повторите поиск чуть позже.'})
    return Handler


def main():
    load_env(ROOT/'.env')
    _, webhook = connection_from_env(live=True)
    import os
    database = Path(os.getenv('BOT_DB_PATH','data/crm_bot.sqlite3'))
    if not database.is_absolute():
        database = ROOT/database
    catalogue = InnCatalogue(ReadOnlyBitrix(webhook,read_timeout=12),OkbSettings.load())
    server = ThreadingHTTPServer(('127.0.0.1',PORT),handler(LookupService(database,catalogue)))
    server.daemon_threads = True
    print('Mini App lookup ready on loopback.',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
