"""Read-only proof of the active regional technician directory; no employee messaging."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from datfo_crm_bot.api import Bitrix
from datfo_crm_bot.config import connection_from_env,load_env
from datfo_crm_bot.technicians import TechnicianDirectory


def main():
    load_env(ROOT/'.env'); _,hook=connection_from_env(live=True)
    directory=TechnicianDirectory(Bitrix(hook))
    rows=directory.listing(force=True)
    if not rows:
        raise SystemExit('Regional technicians are not confirmed')
    checked=directory.employee(rows[0]['bitrix_id'])
    result={'ok':True,'source':'Bitrix company structure','support_department':17,
        'regional_departments':{'60':'R1','61':'R2','62':'R3','63':'R4'},
        'employee_count':len(rows),'region_counts':{str(region):sum(region in row['regions'] for row in rows) for region in range(1,5)},
        'employees':rows,'fresh_employee_check':checked['bitrix_id']==rows[0]['bitrix_id'],
        'writes':0,'telegram_messages':0}
    (ROOT/'data/technician-directory-live-20261008.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({key:value for key,value in result.items() if key!='employees'},ensure_ascii=False))


if __name__=='__main__':
    main()
