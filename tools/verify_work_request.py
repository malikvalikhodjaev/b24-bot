"""One marked FOM request linked to an existing own CRM deal; no CRM writes."""
import argparse
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from datfo_crm_bot.api import Bitrix, Telegram
from datfo_crm_bot.config import Config, load_env, connection_from_env
from datfo_crm_bot.live_deals import LiveDeals
from datfo_crm_bot.registration import Registration, RegistrationSettings, Directory
from datfo_crm_bot.storage import Store, process_lock
from datfo_crm_bot.work_inbox import WorkInbox


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--create-and-check',action='store_true',required=True)
    parser.add_argument('--deal-id',type=int,required=True)
    parser.add_argument('--show-in-bot',action='store_true')
    args = parser.parse_args()
    if args.deal_id<1:
        raise RuntimeError('A positive deal ID is required')
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
            work = WorkInbox(registration,telegram,live)
            before = live.crm.item(args.deal_id)
            if int(before['assignedById'])!=132:
                raise RuntimeError('Choose an existing deal responsible to Malik')
            link = work.link(user,args.deal_id)
            row = work.store.create(user,user,link,'[ТЕСТ БОТА] Проверка заявки ФОМ',
                'Тест номера заявки, ссылки на существующую сделку и приёма в работу. Получатель — Малик; '
                'сотрудникам и клиентам эта проверка не рассылается. Карточка Б24 не изменяется.',
                f'fom-work-qa-20261006-{args.deal_id}')
            after = live.crm.item(args.deal_id)
            if any(before.get(key)!=after.get(key) for key in ('id','title','categoryId','stageId','assignedById','updatedTime')):
                raise RuntimeError('CRM state changed during verification; request remains saved for review')
            output = {'ticket_id':row['id'],'deal_id':row['deal_id'],'deal_url':row['deal_url'],
                'creator':row['creator'],'recipient':row['initial_recipient'],'status':row['status'],
                'test':bool(row['test']),'crm_unchanged':True,'card_sent_to_creator':False}
            if args.show_in_bot:
                work.card(user,row,cache=False,heading='🧪 Локал ботда ФОМ сўровини синаш')
                output['card_sent_to_creator'] = True
            (ROOT/'data'/'work-request-qa-result.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(output,ensure_ascii=False))
        finally:
            store.close()


if __name__=='__main__':
    main()
