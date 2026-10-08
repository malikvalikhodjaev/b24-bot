"""Publish only the verified contact/route form assets on the existing FOM host."""
import base64
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]
EXPECTED={
    'index.html':'8458466483b452da95e045185cd79405b0ca7a71386dcd54922aeab832396084',
    'app.js':'c8de5c3699a9637bb776ef950eece02e71e463892b78a138ef8bee685e793e8b',
}


def main():
    installer=(ROOT/'tools/deploy_miniapp_assets.py').read_text(encoding='utf-8')
    assets={name:base64.b64encode((ROOT/'webapp'/name).read_bytes()).decode('ascii') for name in EXPECTED}
    installer=installer.replace('ASSETS = {}','ASSETS = '+repr(assets),1).replace('EXPECTED = {}','EXPECTED = '+repr(EXPECTED),1)
    (ROOT/'data/deploy-support-contact-20261008.py').write_text(installer,encoding='utf-8')
    result=subprocess.run(['ssh.exe','-i',str(Path.home()/'.ssh/fom_dashboard_codex_v3'),
        '-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=15',
        'malik@130.49.168.29','python3','-'],input=installer.encode('utf-8'),
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=60)
    if result.returncode:
        raise SystemExit(result.stderr.decode('utf-8',errors='replace'))
    record=json.loads(result.stdout)
    (ROOT/'data/support-contact-deployment-20261008.json').write_text(json.dumps(record,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(record))


if __name__=='__main__':
    main()
