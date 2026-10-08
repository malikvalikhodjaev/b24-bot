"""Move Malik's known reminder marker into native CRM origin fields."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from datfo_crm_bot import activity_identity
from datfo_crm_bot.api import Bitrix, Telegram
from datfo_crm_bot.config import Config, load_env, connection_from_env
from datfo_crm_bot.live_deals import LiveDeals
from datfo_crm_bot.registration import Directory, Registration, RegistrationSettings
from datfo_crm_bot.reminders import Reminders
from datfo_crm_bot.storage import Store, process_lock

USER = 855438267
REQUEST = 'datfo-live-20f7c6a956ea43ac9ee3cb8fd8bb770f'
DEAL = 140546
ACTIVITY = 351449


class ScopedBitrix(Bitrix):
    def call(self, method, payload=None):
        if method == 'crm.activity.get' and payload == {'id': ACTIVITY}:
            return super().call(method, payload)
        if method == 'crm.item.get' and payload == {'entityTypeId': 2, 'id': DEAL, 'useOriginalUfNames': 'Y'}:
            return super().call(method, payload)
        if method == 'crm.activity.update':
            current = super().call('crm.activity.get', {'id': ACTIVITY})['result']
            expected = {'id': ACTIVITY, 'fields': {'ORIGINATOR_ID': activity_identity.SOURCE,
                'ORIGIN_ID': activity_identity.origin_id(REQUEST),
                'DESCRIPTION': activity_identity.cleaned_description(current, REQUEST)}}
            if payload == expected and activity_identity.matches(current, REQUEST):
                return super().call(method, payload)
        raise RuntimeError('Only the known reminder description and origin metadata may change')


class SilentTelegram:
    def send(self, *args, **kwargs):
        raise RuntimeError('This maintenance check sends no Telegram messages')

    def call(self, *args, **kwargs):
        raise RuntimeError('This maintenance check sends no Telegram messages')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true', required=True)
    parser.parse_args()
    load_env(ROOT / '.env')
    token, hook = connection_from_env(live=True)
    if Telegram(token).call('getMe').get('username') != 'fom_bitrix_bot':
        raise RuntimeError('Unexpected bot identity')
    database = ROOT / 'data/crm_bot.sqlite3'
    with process_lock(database):
        store = Store(database)
        try:
            raw = dict(store.db.execute('SELECT * FROM operations WHERE request_id=?', (REQUEST,)).fetchone())
            operation = store.operation(REQUEST)
            if (operation['status'] != 'succeeded' or operation['user_id'] != USER
                    or operation['result']['id'] != DEAL or operation['result']['activity_id'] != ACTIVITY):
                raise RuntimeError('Expected the exact completed sale and reminder')
            api = ScopedBitrix(hook)
            before = api.call('crm.activity.get', {'id': ACTIVITY})['result']
            backup_root = ROOT / 'data/backups'
            backup_root.mkdir(exist_ok=True)
            backup = backup_root / 'before-hide-reminder-code-20261007.sqlite3'
            if not backup.exists():
                with sqlite3.connect(backup) as destination:
                    store.db.backup(destination)
            crm_backup = backup_root / 'activity-351449-before-hide-code-20261007.json'
            if not crm_backup.exists():
                crm_backup.write_text(json.dumps(before, ensure_ascii=False, indent=2), encoding='utf-8')
            registration = Registration(RegistrationSettings.load(), store, Directory(api), SilentTelegram())
            member = registration.user(USER)
            if not member or member.bitrix_id != 132:
                raise RuntimeError('Expected the approved Malik account')
            config = Config(token, hook, database, ZoneInfo('Asia/Tashkent'), {USER: member}, {})
            live = LiveDeals(registration, config, api, SilentTelegram())
            reminders = Reminders(registration, live, SilentTelegram())
            reminders.discover()
            reminders.own(USER, ACTIVITY)
            try:
                live.bot.sales.activity_metadata(operation['value'], operation['result'], {'id': ACTIVITY})
            finally:
                # Maintenance of an already completed sale must keep its result,
                # completion time and original frozen input exactly as stored.
                with store.db:
                    store.db.execute('UPDATE operations SET status=?,result=?,completed_at=? WHERE request_id=?',
                        (raw['status'], raw['result'], raw['completed_at'], REQUEST))
            after = api.call('crm.activity.get', {'id': ACTIVITY})['result']
            preserved = ('ID', 'OWNER_ID', 'OWNER_TYPE_ID', 'TYPE_ID', 'PROVIDER_ID', 'PROVIDER_TYPE_ID',
                'SUBJECT', 'DEADLINE', 'START_TIME', 'END_TIME', 'RESPONSIBLE_ID', 'COMPLETED', 'SETTINGS')
            if any(before.get(key) != after.get(key) for key in preserved):
                raise RuntimeError('A business field changed unexpectedly')
            if (activity_identity.has_legacy_tag(after, REQUEST)
                    or after.get('ORIGINATOR_ID') != activity_identity.SOURCE
                    or after.get('ORIGIN_ID') != activity_identity.origin_id(REQUEST)):
                raise RuntimeError('Hidden reminder binding was not confirmed')
            reminders.own(USER, ACTIVITY)
            if dict(store.db.execute('SELECT * FROM operations WHERE request_id=?', (REQUEST,)).fetchone()) != raw:
                raise RuntimeError('The completed sale changed')
            report = {'ok': True, 'activity_id': ACTIVITY, 'deal_id': DEAL,
                'description': after['DESCRIPTION'], 'hidden_binding_confirmed': True,
                'business_fields_preserved': True, 'completed_sale_preserved': True,
                'reminder_access_confirmed': True, 'deadline': after['DEADLINE'],
                'sqlite_integrity': store.db.execute('PRAGMA integrity_check').fetchone()[0],
                'checked_at': datetime.now(config.timezone).isoformat()}
            (ROOT / 'data/reminder-code-cleanup-20261007.json').write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(report, ensure_ascii=False))
        finally:
            store.close()


if __name__ == '__main__':
    main()
