"""One explicitly requested real test deal. Does not send Telegram messages."""
import argparse
import json
from pathlib import Path
import sys
from uuid import uuid4
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding='utf-8')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from datfo_crm_bot.api import Bitrix, Telegram
from datfo_crm_bot.config import Config, connection_from_env, load_env
from datfo_crm_bot.live_deals import LiveDeals, SOURCE, TEST_PREFIX
from datfo_crm_bot.registration import Registration, RegistrationSettings, Directory
from datfo_crm_bot.service import utc_text
from datfo_crm_bot.storage import Store, process_lock


class ReviewTelegram:
    def send(self,chat_id,text,keyboard=None):
        pass

    def call(self,method,payload):
        if method not in {'sendMessage','answerCallbackQuery'}:
            raise RuntimeError('Unexpected Telegram operation')
        return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--create-and-check',action='store_true',required=True)
    parser.parse_args()
    load_env(ROOT/'.env')
    token,webhook = connection_from_env(live=True)
    if Telegram(token).call('getMe').get('username')!='fom_bitrix_bot':
        raise RuntimeError('Unexpected bot')
    database = ROOT/'data'/'crm_bot.sqlite3'
    state_path = ROOT/'data'/'live-deal-qa-state.json'
    with process_lock(database):
        store = Store(database)
        try:
            config = Config(token,webhook,database,ZoneInfo('Asia/Tashkent'),{}, {})
            api = Bitrix(webhook)
            registration = Registration(RegistrationSettings.load(),store,Directory(api),ReviewTelegram())
            user = 855438267
            member = registration.user(user)
            if not member or not member.admin or member.bitrix_id!=132:
                raise RuntimeError('Expected confirmed Malik account')
            live = LiveDeals(registration,config,api,ReviewTelegram())
            live.bot.config.users[user] = member
            if state_path.exists():
                state = json.loads(state_path.read_text(encoding='utf-8'))
            else:
                categories = live.crm.categories()
                category = next(row for row in categories if row['id']==43 and row['title']=='DATFO Новые продажи')
                stages = [row for row in live.crm.stages(category['id']) if row['semantics'] not in {'S','F'}]
                state = {'workflow':'live_deal','kind':'deal','request_id':'datfo-live-qa-'+uuid4().hex,
                         'category_id':category['id'],'category_name':category['title'],
                         'initial_stage':stages[0]['id'],'initial_stage_name':stages[0]['title'],
                         'step':'confirm','live_test':True,'title':TEST_PREFIX+'Проверка бота, связи и стадий · Малик',
                         'description':'Реальная тестовая запись по запросу Малика от 4 октября 2026. Создано Codex для проверки сохранения ID и переключения стадий. Не клиентская продажа.',
                         'inn':'','phone':'','company':None}
                state_path.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
            if state.get('workflow')!='live_deal' or state.get('category_id')!=43 or not state.get('live_test') or not state['title'].startswith(TEST_PREFIX):
                raise RuntimeError('Unexpected verification state')
            store.prepare(user,state,utc_text(live.bot.clock()))
            live.bot._configure(state)
            response,_,_ = live.bot.submit(user,state)
            operation = store.operation(state['request_id'])
            if operation['status']!='succeeded' or not operation['result']:
                print(response)
                raise RuntimeError('Real test creation is not confirmed: '+operation['status'])
            row = live.item_for(operation,member,user)
            if int(row['assignedById'])!=132 or row['originatorId']!=SOURCE:
                raise RuntimeError('Responsible or source was not preserved')
            original = row['stageId']
            stages = live.crm.stages(int(row['categoryId']))
            destination = next(item for item in stages if item['id']!=original and item['semantics'] not in {'S','F'})

            def move(target):
                current = live.item_for(operation,member,user)
                options = live.crm.stages(int(current['categoryId']))
                choice = live.stages.choice(user,operation,current,options)
                index = next(index for index,item in enumerate(options) if item['id']==target)
                intent = live.stages.intent(live.stages.row('crm_stage_choices',choice,user),index)
                live.apply_stage(user,intent['token'],member)
                if live.stages.row('crm_stage_intents',intent['token'],user)['status']!='succeeded':
                    raise RuntimeError('Real stage transition is not confirmed')
                after = live.item_for(operation,member,user)
                if after['stageId']!=target:
                    raise RuntimeError('Stage verification failed')
                return after['stageId']

            middle = move(destination['id'])
            final = move(original)
            result = {'id':row['id'],'url':operation['result']['url'],'responsible_id':int(row['assignedById']),
                      'category_id':row['categoryId'],'request_id':state['request_id'],
                      'stages':[original,middle,final],'source_verified':True,'telegram_messages_sent':0}
            (ROOT/'qa'/'live-deal-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(result,ensure_ascii=False))
        finally:
            store.close()


if __name__=='__main__':
    main()
