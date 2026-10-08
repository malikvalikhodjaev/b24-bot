"""Create one marked self-addressed technical request; verify the native FOM stages."""
import argparse
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from datfo_crm_bot.api import Bitrix, Telegram, RemoteError
from datfo_crm_bot.config import Config, load_env, connection_from_env
from datfo_crm_bot.live_deals import LiveDeals
from datfo_crm_bot.registration import Registration, RegistrationSettings, Directory
from datfo_crm_bot.storage import Store, process_lock
from datfo_crm_bot.work_inbox import WorkInbox, elapsed
from datfo_crm_bot.work_sync import SupportSettings


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--create-and-check',action='store_true',required=True)
    parser.add_argument('--deal-id',type=int,required=True)
    parser.add_argument('--show-in-bot',action='store_true')
    args = parser.parse_args()
    if args.deal_id<1:
        raise RuntimeError('Positive deal ID required')
    load_env(ROOT/'.env')
    token,webhook = connection_from_env(live=True)
    telegram = Telegram(token)
    if telegram.call('getMe').get('username')!='fom_bitrix_bot':
        raise RuntimeError('Unexpected bot')
    database = ROOT/'data'/'crm_bot.sqlite3'
    with process_lock(database):
        store = Store(database)
        try:
            api = Bitrix(webhook)
            registration = Registration(RegistrationSettings.load(),store,Directory(api),telegram)
            user = 855438267
            member = registration.user(user)
            if not member or not member.admin or member.bitrix_id!=132:
                raise RuntimeError('Expected confirmed Malik account')
            live = LiveDeals(registration,Config(token,webhook,database,ZoneInfo('Asia/Tashkent'),{},{}),api,telegram)
            work = WorkInbox(registration,telegram,live,support_settings=SupportSettings.load())
            before = live.crm.item(args.deal_id)
            if int(before['assignedById'])!=132:
                raise RuntimeError('Source must belong to Malik')
            link = work.link(user,args.deal_id)
            row = work.store.create(user,user,link,'[ТЕСТ БОТА] Стадии и время заявки ФОМ',
                'Проверка существующей воронки техобслуживания, регионов R1 и R2–R3–R4, '
                'ожидания и времени от создания до решения. Получатель — только Малик. '
                'Исходная служебная сделка сохраняется без изменений.',
                f'fom-stage-qa-20261006-{args.deal_id}')
            work.sync.retry(row['id'],user)
            row = work.store.ticket(row['id'])
            observations = []
            def observe():
                binding = work.sync.binding(row['id'])
                observations.append({'version':row['version'],'local_status':row['status'],'queue':row['queue'],
                    'sync':binding['state'],'error':binding['error'],'crm_id':binding['crm_id'],'stage':binding['stage_id']})
                return binding['state']=='confirmed'
            ready = observe()
            # Stable request origin means reruns recover the same request and never create a duplicate.
            if ready and row['status']=='assigned' and row['version']==0:
                for action in ('r1','r234','take','wait','resume','close'):
                    row = work.store.change(user,row['id'],row['version'],action)['ticket']
                    work.sync.flush(row['id'])
                    if not observe():
                        ready = False
                        break
            after = live.crm.item(args.deal_id)
            unchanged = all(before.get(key)==after.get(key) for key in ('id','title','categoryId','stageId','assignedById','updatedTime'))
            if not unchanged:
                raise RuntimeError('Source changed during verification; preserve records for review')
            row = work.store.ticket(row['id'])
            output = {'ticket_id':row['id'],'test':bool(row['test']),'creator':user,'responsible_bitrix_id':132,
                'source_deal_id':args.deal_id,'source_unchanged':unchanged,'observations':observations,
                'created_at':row['created_at'],'work_at':row['work_at'],'resolved_at':row['resolved_at'],
                'resolution_seconds':elapsed(row),'status':row['status'],'all_confirmed':ready,
                'card_sent_to_creator':False}
            if args.show_in_bot:
                work.card(user,row,cache=False,heading='🧪 Б24 босқичлари ва вақт текшируви')
                output['card_sent_to_creator'] = True
            (ROOT/'data'/'support-stages-qa-result.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(output,ensure_ascii=False))
        finally:
            store.close()


if __name__=='__main__':
    try:
        main()
    except RemoteError as exc:
        print(json.dumps({'error':exc.code}))
        sys.exit(1)
