"""Read-only restart proof, comparing existing columns across an additive migration."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import re

ROOT = Path(__file__).resolve().parents[1]
NAMES = ('registrations','fom_request_members','operations','operation_steps','sessions',
         'fom_request_sessions','fom_requests','crm_reminders','crm_reminder_notices',
         'crm_reminder_snooze_inputs','crm_reminder_actions')


def main():
    label = sys.argv[1]
    assert label in {'before','after'}
    tag = sys.argv[2] if len(sys.argv)>2 else 'support-types'
    assert re.fullmatch(r'[a-z]+(?:-[a-z]+)*',tag)
    before_path = ROOT/f'data/{tag}-state-before-20261008.json'
    before = json.loads(before_path.read_text(encoding='utf-8')) if label=='after' else None
    db = sqlite3.connect((ROOT/'data/crm_bot.sqlite3').as_uri()+'?mode=ro',uri=True)
    result = {}
    for name in NAMES:
        columns = before[name]['columns'] if before else [row[1] for row in db.execute('PRAGMA table_info('+name+')')]
        selection = ','.join('"'+column+'"' for column in columns)
        rows = list(db.execute('SELECT '+selection+' FROM '+name+' ORDER BY '+selection))
        result[name] = {'columns':columns,'rows':len(rows),'sha256':hashlib.sha256(
            json.dumps(rows,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()}
    result['sending_operations'] = db.execute("SELECT count(*) FROM operations WHERE status='sending'").fetchone()[0]
    result['sending_crm_jobs'] = db.execute("SELECT count(*) FROM fom_request_crm_jobs WHERE state='sending'").fetchone()[0]
    result['sending_reminder_actions'] = db.execute("SELECT count(*) FROM crm_reminder_actions WHERE status='sending'").fetchone()[0]
    result['quick_check'] = db.execute('PRAGMA quick_check').fetchone()[0]
    if before:
        result['previous_data_unchanged'] = all(before[name]==result[name] for name in NAMES)
        result['old_support_metadata_empty'] = db.execute('SELECT count(*) FROM fom_requests WHERE support_details IS NOT NULL').fetchone()[0]==0
    db.close()
    (ROOT/f'data/{tag}-state-{label}-20261008.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:value if key not in NAMES else {'rows':value['rows']} for key,value in result.items()}))
    if label=='before' and any(result[key] for key in ('sending_operations','sending_crm_jobs','sending_reminder_actions')):
        raise SystemExit('A write is in progress; restart postponed')


if __name__=='__main__':
    main()
