"""Verify the public static release with curl; do not read authenticated data."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import re

ROOT = Path(__file__).resolve().parents[1]
TAG = sys.argv[1] if len(sys.argv)>1 else 'support-types'
assert re.fullmatch(r'[a-z]+(?:-[a-z]+)*',TAG)
OUT = ROOT/f'data/{TAG}-public-20261008'


def fetch(name):
    target = OUT/name
    url = 'https://fom-analytics.uz/bot-form/'+('' if name=='index.html' else name)+'?v=20261008-'+TAG
    subprocess.run(['curl.exe','--compressed','--fail','--silent','--show-error','--location',
                    '--max-time','20','--output',str(target),url],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    actual = target.read_bytes()
    expected = (ROOT/'webapp'/name).read_bytes()
    return name, {'matches': actual==expected, 'sha256':hashlib.sha256(actual).hexdigest()}


def main():
    OUT.mkdir(exist_ok=True)
    with ThreadPoolExecutor(max_workers=4) as executor:
        assets = dict(executor.map(fetch, ['index.html','app.js','style.css','reference.json']))
    private = subprocess.run(['curl.exe','--silent','--show-error','--max-time','15','--output','NUL',
                              '--write-out','%{http_code}','https://fom-analytics.uz/api/admin/overview'],
                             check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE).stdout.decode('ascii')
    result = {'assets':assets,'private_api':int(private),'ok':all(row['matches'] for row in assets.values()) and private=='401'}
    (ROOT/f'data/{TAG}-public-check-20261008.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result))
    if not result['ok']:
        raise SystemExit('Public asset check differs from the published sources')


if __name__=='__main__':
    main()
