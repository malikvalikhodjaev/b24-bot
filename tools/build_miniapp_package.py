"""Build a self-contained install command without credentials or CRM records."""
import base64
import json
from pathlib import Path
import zlib

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / 'tools/install_miniapp.py').read_text(encoding='utf-8')
names = ('index.html', 'app.js', 'style.css', 'reference.json')
assets = {name: base64.b64encode((ROOT / 'webapp' / name).read_bytes()).decode('ascii') for name in names}
source = source.replace('ASSETS = {}', 'ASSETS = ' + repr(assets), 1)
compile(source, 'install_miniapp.py', 'exec')
packed = base64.b64encode(zlib.compress(source.encode('utf-8'), 9)).decode('ascii')
command = 'python3 -c \'import base64,zlib;exec(zlib.decompress(base64.b64decode("' + packed + '")))\''
path = ROOT / 'data/miniapp-publish-command.txt'
path.write_text(command + '\n', encoding='utf-8')
(ROOT / 'data/miniapp-install.py').write_text(source, encoding='utf-8')
print(json.dumps({'command_file': str(path), 'command_length': len(command), 'assets': len(assets)}))
