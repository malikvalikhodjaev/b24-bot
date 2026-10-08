"""On the named FOM host: replace selected static assets, preserve server and services."""
import base64
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

ASSETS = {}
EXPECTED = {}
NAMES = ('index.html','app.js','style.css','reference.json')


def main():
    root=Path('/home/malik/-fom-dashboards').resolve(strict=True)
    names=tuple(ASSETS)
    if str(root)!='/home/malik/-fom-dashboards' or not names or not set(names).issubset(NAMES):
        raise RuntimeError('Wrong repository or unsupported static asset package')
    server=root/'scripts/dashboard_server.py'
    server_bytes=server.read_bytes()
    if b'def serve_bot_form(' not in server_bytes:
        raise RuntimeError('Existing static route required; server is not changed by this installer')
    paths={name:root/'apps/bot_form'/name for name in names}
    if any(not path.resolve(strict=True).is_relative_to(root) or path.is_symlink() for path in paths.values()):
        raise RuntimeError('An asset path escapes the named repository')
    previous={name:path.read_bytes() for name,path in paths.items()}
    if EXPECTED and (set(EXPECTED)!=set(names) or any(
            hashlib.sha256(value).hexdigest()!=EXPECTED[name] for name,value in previous.items())):
        raise RuntimeError('Selected assets changed since the reviewed version')
    data={name:base64.b64decode(ASSETS[name],validate=True) for name in names}
    backup=root/'logs/miniapp-backups'/datetime.now().strftime('%Y%m%d-%H%M%S-%f-assets')
    backup.mkdir(parents=True)
    for name,value in previous.items():(backup/name).write_bytes(value)
    def atomic(name,value):
        target=paths[name];temp=target.with_name(target.name+'.miniapp-tmp')
        if temp.exists() or temp.is_symlink():raise RuntimeError('Temporary path already exists')
        with temp.open('xb') as stream:stream.write(value)
        os.replace(temp,target)
    try:
        for name,value in data.items():atomic(name,value)
        for name,value in data.items():
            url='http://127.0.0.1:8768/bot-form/'+('' if name=='index.html' else name)
            with urlopen(url,timeout=10) as response:
                if response.status!=200 or response.read()!=value:raise RuntimeError('Static asset response mismatch: '+name)
        try:
            with urlopen('http://127.0.0.1:8768/api/admin/overview',timeout=10):
                raise RuntimeError('Protected API unexpectedly public')
        except HTTPError as exc:
            if exc.code!=401:raise
        if server.read_bytes()!=server_bytes:raise RuntimeError('Server source changed concurrently')
        result={'ok':True,'assets':len(names),'private_api':401,'backup':str(backup),'server_unchanged':True,
            'service_restarted':False,'sha256':{name:hashlib.sha256(value).hexdigest() for name,value in data.items()}}
        (backup/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result))
    except Exception:
        for name,value in previous.items():atomic(name,value)
        raise


if __name__=='__main__':main()
