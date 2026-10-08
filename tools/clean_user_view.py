"""Archive only bot QA requests and delete their verified marked support cards on request."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from datfo_crm_bot.api import Bitrix, Telegram, RemoteError
from datfo_crm_bot.config import Config, load_env, connection_from_env
from datfo_crm_bot.live_deals import LiveDeals
from datfo_crm_bot.registration import Directory, Registration, RegistrationSettings
from datfo_crm_bot.storage import Store, process_lock
from datfo_crm_bot.work_inbox import WorkInbox
from datfo_crm_bot.work_sync import SupportSettings, SOURCE
from datfo_crm_bot.communications import inbox_test_for
from datfo_crm_bot.app import COMMANDS


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply',action='store_true',required=True)
    args=parser.parse_args()
    load_env(ROOT/'.env')
    token,webhook=connection_from_env(live=True)
    api,tg=Bitrix(webhook),Telegram(token)
    if tg.call('getMe').get('username')!='fom_bitrix_bot':
        raise RuntimeError('Unexpected bot')
    database=ROOT/'data/crm_bot.sqlite3'
    with process_lock(database):
        backup=ROOT/'data/backups'/('before-clean-user-view-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'.sqlite3')
        backup.parent.mkdir(exist_ok=True)
        original=sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)
        copy=sqlite3.connect(backup);original.backup(copy);copy.close();original.close()
        store=Store(database)
        try:
            reg=Registration(RegistrationSettings.load(),store,Directory(api),tg)
            user=855438267
            if not reg.user(user) or not reg.user(user).admin or reg.user(user).bitrix_id!=132:
                raise RuntimeError('Expected confirmed Malik')
            live=LiveDeals(reg,Config(token,webhook,database,ZoneInfo('Asia/Tashkent'),{},{}),api,tg)
            work=WorkInbox(reg,tg,live,support_settings=SupportSettings.load())
            rows=[dict(row) for row in store.db.execute('SELECT * FROM fom_requests WHERE creator=? AND test=1',(user,))]
            for row in rows:
                if not row['request_id'].startswith(('fom-work-qa-','fom-stage-qa-')) or not row['title'].startswith('[ТЕСТ БОТА]'):
                    raise RuntimeError('Preserve a test whose QA provenance is unknown')
            records=[]
            for row in rows:
                binding=work.sync.binding(row['id'])
                if not binding or not binding['crm_id']:
                    continue
                try:
                    remote=live.crm.item(binding['crm_id'])
                except RemoteError as exc:
                    if exc.code=='NOT_FOUND':
                        records.append({'ticket':row['id'],'id':binding['crm_id'],'already_absent':True})
                        continue
                    raise
                if (remote.get('originatorId')!=SOURCE or remote.get('originId')!=row['request_id']
                        or not str(remote.get('title','')).startswith('[ТЕСТ БОТА]') or int(remote.get('categoryId',-1))!=47
                        or int(remote.get('assignedById',-1))!=132 or int(remote['id'])==row['deal_id']):
                    raise RuntimeError('Preserve CRM card whose QA identity or owner changed')
                records.append({'ticket':row['id'],'id':int(remote['id']),'remote':remote})
            saved=ROOT/'data/backups'/('qa-records-before-clean-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'.json')
            saved.write_text(json.dumps({'tickets':rows,'crm':records},ensure_ascii=False,indent=2),encoding='utf-8')
            with store.db:
                for row in rows:
                    store.db.execute('UPDATE fom_requests SET archived=1 WHERE id=?',(row['id'],))
                for key in ['b24-test:'+str(user),'simulation-active:'+str(user)]:
                    store.db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,'0'))
                session=store.session(user)
                if session and (session.get('live_test') or str(session.get('title','')).startswith('[ТЕСТ БОТА]')):
                    store.set_session(user,None)
            with inbox_test_for(reg,tg) as inbox:
                inbox.store.exit(user)
            result={'archived_tickets':[row['id'] for row in rows],'crm_removed':[],'crm_errors':[],
                'original_source_preserved':136814,'test_modes_off':True}
            for record in records:
                if record.get('already_absent'):
                    result['crm_removed'].append(record['id']);continue
                try:
                    api.call('crm.item.delete',{'entityTypeId':2,'id':record['id']})
                    try:
                        live.crm.item(record['id'])
                    except RemoteError as exc:
                        if exc.code=='NOT_FOUND':
                            result['crm_removed'].append(record['id']);continue
                        raise
                    result['crm_errors'].append({'id':record['id'],'error':'DELETE_NOT_CONFIRMED'})
                except RemoteError as exc:
                    result['crm_errors'].append({'id':record['id'],'error':exc.code})
            tg.call('setMyCommands',{'commands':COMMANDS})
            reg.okb_enabled=True
            greeting,keyboard=reg.route(user,user,'/start','/start')
            tg.send(user,greeting,keyboard)
            result['clean_menu_sent']=True
            result['visible_tickets']=len(work.store.listing(user))
            (ROOT/'data/clean-user-view-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(result,ensure_ascii=False))
        finally:
            store.close()


if __name__=='__main__':
    try:
        main()
    except RemoteError as exc:
        print(json.dumps({'error':exc.code}));sys.exit(1)
