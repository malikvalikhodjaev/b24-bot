"""Finish the exact user-confirmed sale whose requisite length was repaired."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from datfo_crm_bot.api import Bitrix, Telegram
from datfo_crm_bot.config import Config, load_env, connection_from_env
from datfo_crm_bot.live_deals import LiveDeals, SOURCE
from datfo_crm_bot.registration import Registration, RegistrationSettings, Directory
from datfo_crm_bot.storage import Store, process_lock
from repair_confirmed_requisite import DiagnosticBitrix

REQUEST = 'datfo-live-20f7c6a956ea43ac9ee3cb8fd8bb770f'
USER = 855438267


class SilentTelegram:
    def send(self, chat_id, text, keyboard=None): pass
    def call(self, method, payload):
        if method not in {'sendMessage', 'answerCallbackQuery'}: raise RuntimeError('Unexpected Telegram call')
        return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', choices=[REQUEST], required=True)
    parser.parse_args()
    load_env(ROOT / '.env')
    token, hook = connection_from_env(live=True)
    if Telegram(token).call('getMe').get('username') != 'fom_bitrix_bot': raise RuntimeError('Unexpected bot')
    database = ROOT / 'data/crm_bot.sqlite3'
    with process_lock(database):
        store = Store(database)
        try:
            operation = store.operation(REQUEST)
            if (not operation or operation['user_id'] != USER or operation['status'] not in {'created', 'succeeded'}
                    or not operation['value'].get('sales_intake') or operation['value'].get('step') != 'confirm'):
                raise RuntimeError('Expected the exact already confirmed partial sale')
            child_id = REQUEST + '-pharmacy'
            for name, expected_id in (('company', 86017), ('requisite', 43925)):
                step = store.operation_step(child_id, name)
                if not step or step['status'] != 'succeeded_write' or step['result']['id'] != expected_id:
                    raise RuntimeError('Expected the previously saved ' + name)
            active = store.session(USER)
            if active and active.get('request_id') != REQUEST:
                raise RuntimeError('The user has another active draft; do not replace it')
            backup = ROOT / 'data/backups/before-finish-confirmed-sale.sqlite3'
            if not backup.exists():
                with sqlite3.connect(backup) as destination: store.db.backup(destination)
            config = Config(token, hook, database, ZoneInfo('Asia/Tashkent'), {}, {})
            api = DiagnosticBitrix(hook)
            registration = Registration(RegistrationSettings.load(), store, Directory(api), SilentTelegram())
            member = registration.user(USER)
            if not member or member.bitrix_id != 132: raise RuntimeError('Expected the approved Malik account')
            live = LiveDeals(registration, config, api, SilentTelegram())
            live.bot.config.users[USER] = member
            response, _, after = live.bot.sales.submit(USER, operation['value'])
            current = store.operation(REQUEST)
            if current['status'] != 'succeeded':
                print(json.dumps({'ok': False, 'status': current['status'], 'message': response}, ensure_ascii=False))
                return 2
            item = live.item_for(current, member, USER)
            child = store.operation(child_id)
            if (int(item['assignedById']) != 132 or item['originatorId'] != SOURCE
                    or str(item['parentId1034']) != str(child['result']['id']) or int(item['companyId']) != 86017):
                raise RuntimeError('The saved sale binding differs from the confirmed draft')
            store.set_session(USER, after)
            result = {'ok': True, 'request': REQUEST, 'deal': current['result'],
                      'pharmacy': child['result'], 'company_id': 86017, 'requisite_id': 43925,
                      'responsible_id': 132, 'category_id': item['categoryId'], 'stage_id': item['stageId']}
            (ROOT / 'data/confirmed-sale-repair.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(result, ensure_ascii=False))
            return 0
        finally: store.close()


if __name__ == '__main__': raise SystemExit(main())
