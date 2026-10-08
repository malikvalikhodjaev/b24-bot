"""Read-only CRM and SQLite verification of the deployed Mini App workflow."""
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from datfo_crm_bot.api import Bitrix
from datfo_crm_bot.config import Config,load_env,connection_from_env
from datfo_crm_bot.live_deals import LiveCrm
from datfo_crm_bot.sales_intake import SalesIntake,FOM_ID_FIELD


class ReadOnlyBitrix(Bitrix):
    def call(self,method,payload=None):
        if method not in {'crm.item.list','crm.item.get','crm.item.fields','crm.company.get','crm.activity.get'}:
            raise RuntimeError('CRM mutation prohibited in this check')
        return super().call(method,payload)


def main():
    load_env(ROOT/'.env');token,hook=connection_from_env(live=True)
    database=ROOT/'data/crm_bot.sqlite3'
    config=Config(token,hook,database,ZoneInfo('Asia/Tashkent'),{}, {})
    api=ReadOnlyBitrix(hook)
    crm=LiveCrm(config,api)
    sales=SalesIntake(SimpleNamespace(config=config,store=None,crm=crm,telegram=None,clock=lambda:datetime.now(config.timezone)))
    fields=api.call('crm.item.fields',{'entityTypeId':1034,'useOriginalUfNames':'Y'})['result']['fields']
    if fields[FOM_ID_FIELD]['title'].strip()!='FOM ID':raise RuntimeError('Wrong FOM ID field')
    if sales.pharmacy_crm.settings.pharmacy.fields['phone'] not in fields:raise RuntimeError('Missing pharmacy phone field')
    rows=api.call('crm.item.list',{'entityTypeId':1034,'useOriginalUfNames':'Y','filter':{'categoryId':17},
        'select':['*'],'order':{'id':'ASC'}})['result']['items']
    known=next((row for row in rows if str(row.get(FOM_ID_FIELD) or '').strip()),None)
    found=False
    if known:
        pharmacy=sales.lookup_fom(str(known[FOM_ID_FIELD]))
        if pharmacy['id']!=int(known.get('id',known.get('ID'))):raise RuntimeError('FOM ID lookup selected the wrong pharmacy')
        if pharmacy['company_id']:sales.company(pharmacy['company_id'])
        found=True
    activity=api.call('crm.activity.get',{'id':351449})['result']
    if 'Запрос бота:' in str(activity.get('DESCRIPTION') or ''):raise RuntimeError('Visible technical marker returned')
    before=json.loads((ROOT/'data/miniapp-workflows-before-20261007.json').read_text(encoding='utf-8'))
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
        operation=db.execute('SELECT value,result,status FROM operations WHERE request_id=?',('datfo-live-20f7c6a956ea43ac9ee3cb8fd8bb770f',)).fetchone()
        if list(operation)!=before['confirmed_operation']:raise RuntimeError('Completed operation changed')
        integrity=db.execute('PRAGMA integrity_check').fetchone()[0]
        columns={row[1] for row in db.execute('PRAGMA table_info(fom_requests)')}
        if not {'link_kind','pharmacy_id','pharmacy_company_id'}.issubset(columns):raise RuntimeError('Support link migration missing')
    record=json.loads((ROOT/'data/registration-process.json').read_text(encoding='utf-8-sig'))
    report={'ok':True,'checked_at':datetime.now(config.timezone).isoformat(),'crm_read_only':True,'fom_id_lookup_confirmed':found,
        'fom_field_verified':True,'pharmacy_phone_field_verified':True,'confirmed_operation_unchanged':True,
        'activity_marker_hidden':True,'sqlite_integrity':integrity,'support_pharmacy_columns':True,'pid':record['pid'],
        'stderr_bytes':Path(record['stderr']).stat().st_size}
    (ROOT/'data/miniapp-launch-check-20261007.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report))


if __name__=='__main__':main()
