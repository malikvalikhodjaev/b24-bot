"""Publish the tested four static files on the existing named FOM dashboard host."""
import base64
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    'index.html':'6568a700e6e8f42ebabeae11597943f1d45803c271b769f8631f87203aef49f8',
    'app.js':'af561fae8f12549004339e69ae7304e51b3aa7964dcfccec20fb69154f2778b6',
    'style.css':'7246b61e20b73a82ad61f63323bf5c7f0ca92677ffd4d8355b384773c1cb4b42',
    'reference.json':'5be49568eabb787962803410fff3d2e2650767709f180995e81f4206e4a2fc1e',
}


def main():
    installer = (ROOT/'tools/deploy_miniapp_assets.py').read_text(encoding='utf-8')
    assets = {name:base64.b64encode((ROOT/'webapp'/name).read_bytes()).decode('ascii') for name in EXPECTED}
    installer = installer.replace('ASSETS = {}', 'ASSETS = '+repr(assets), 1).replace('EXPECTED = {}', 'EXPECTED = '+repr(EXPECTED), 1)
    (ROOT/'data/deploy-office-types-20261008.py').write_text(installer,encoding='utf-8')
    result = subprocess.run(['ssh.exe','-i',str(Path.home()/'.ssh/fom_dashboard_codex_v3'),
                             '-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=15',
                             'malik@130.49.168.29','python3','-'],input=installer.encode('utf-8'),
                            stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60)
    if result.returncode:
        raise SystemExit(result.stderr.decode('utf-8',errors='replace'))
    record = json.loads(result.stdout)
    (ROOT/'data/office-types-deployment-20261008.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(record))


if __name__=='__main__':
    main()
