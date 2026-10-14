"""Local INN flow preview. No Telegram polling, no CRM writes, no VPS deployment."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import argparse
import json
import sys

from catalog import LiveCatalogue,PreviewCatalogue,REFERENCE,ROOT,RemoteError


def make_server(port, catalogue):
    assets = Path(__file__).resolve().parent
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def reply(self,status,value):
            raw=json.dumps(value,ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store')
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError,ConnectionResetError):
                pass

        def do_GET(self):
            path=urlsplit(self.path).path
            if path=='/api/health':
                return self.reply(200,{'sources':list(catalogue.providers),'crm_writes':False,'version':'inn-preview-v0.1'})
            if path=='/reference.json':
                return self.reply(200,REFERENCE)
            names={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
            name=names.get(path)
            if not name:
                return self.reply(404,{'error':'Страница не найдена.'})
            raw=(assets/name).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type',{'html':'text/html','js':'text/javascript','css':'text/css'}[name.split('.')[-1]]+'; charset=utf-8')
            self.send_header('Content-Length',str(len(raw)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            origin=self.headers.get('Origin')
            expected='http://127.0.0.1:'+str(self.server.server_port)
            if origin and origin!=expected:
                return self.reply(403,{'error':'Откройте тестовую форму напрямую.'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=16384 or self.headers.get_content_type()!='application/json':
                    raise ValueError('Неверный запрос формы.')
                data=json.loads(self.rfile.read(length))
                if not isinstance(data,dict):
                    raise ValueError('Неверный запрос формы.')
                path=urlsplit(self.path).path
                if path=='/api/lookup':
                    companies=catalogue.lookup(data.get('source'),data.get('inn'))
                    value={'companies':[{key:value for key,value in row.items() if key!='pharmacies'} for row in companies]}
                elif path=='/api/pharmacies':
                    companies=catalogue.lookup(data.get('source'),data.get('inn'))
                    company=next((row for row in companies if str(row['id'])==str(data.get('company_id'))),None)
                    if not company:
                        raise ValueError('Фирма не найдена. Повторите поиск по ИНН.')
                    value={'pharmacies':company['pharmacies']}
                elif path=='/api/preview':
                    value=catalogue.preview(data)
                else:
                    return self.reply(404,{'error':'Этот сценарий недоступен.'})
                self.reply(200,value)
            except (ValueError,TypeError):
                self.reply(400,{'error':str(sys.exc_info()[1])[:250]})
            except RemoteError:
                self.reply(502,{'error':'Б24 не ответил на поиск. Повторите попытку. Данные формы сохранены.'})
            except Exception:
                self.reply(500,{'error':'Не удалось проверить данные. Повторите поиск.'})
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=18771)
    parser.add_argument('--private-root',type=Path)
    args=parser.parse_args()
    live=None
    if args.private_root:
        try:
            live=LiveCatalogue.local_connection(args.private_root)
        except Exception:
            print('CRM connection unavailable; demo scenarios remain available.',flush=True)
    server=make_server(args.port,PreviewCatalogue(live))
    print('INN flow preview: http://127.0.0.1:'+str(server.server_port)+'; CRM writes disabled.',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
