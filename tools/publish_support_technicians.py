"""Publish two route-hint assets with exact previous-release guards and rollback."""
import base64
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
EXPECTED={
    'index.html':'cbc1d0b6fb8c01d9edee18bb820a6d79cee7443af69557b02b26ae8085786e07',
    'app.js':'35cd61b623e420f43c09b3f1184269c5a099fa7b0381a4401b5e4b1b7512c9e8',
}


def main():
    installer=(ROOT/'tools/deploy_miniapp_assets.py').read_text(encoding='utf-8')
    assets={name:base64.b64encode((ROOT/'webapp'/name).read_bytes()).decode('ascii') for name in EXPECTED}
    installer=installer.replace('ASSETS = {}','ASSETS = '+repr(assets),1).replace('EXPECTED = {}','EXPECTED = '+repr(EXPECTED),1)
    (ROOT/'data/deploy-support-technicians-20261008.py').write_text(installer,encoding='utf-8')
    result=subprocess.run(['ssh.exe','-i',str(Path.home()/'.ssh/fom_dashboard_codex_v3'),
        '-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=15',
        'malik@130.49.168.29','python3','-'],input=installer.encode('utf-8'),
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60)
    if result.returncode:
        raise SystemExit(result.stderr.decode('utf-8',errors='replace'))
    record=json.loads(result.stdout)
    (ROOT/'data/technician-deployment-20261008.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(record))


if __name__=='__main__':
    main()
