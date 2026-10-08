"""Initialize reminders and optionally send Malik one reformatted confirmation."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from datfo_crm_bot.api import Bitrix, Telegram
from datfo_crm_bot.config import Config, load_env, connection_from_env
from datfo_crm_bot.i18n import LocalizedTelegram, language_context, stored_language
from datfo_crm_bot.live_deals import LiveDeals
from datfo_crm_bot.registration import Directory, Registration, RegistrationSettings
from datfo_crm_bot.reminders import Reminders, phrase
from datfo_crm_bot.storage import Store, process_lock

USER = 855438267
REQUEST = 'datfo-live-20f7c6a956ea43ac9ee3cb8fd8bb770f'


class ReadOnlyBitrix(Bitrix):
    def call(self, method, payload=None):
        if method not in {'crm.activity.get', 'crm.item.get'}:
            raise RuntimeError('This launch check permits CRM reads only')
        return super().call(method, payload)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--send-preview', action='store_true')
    args = parser.parse_args()
    load_env(ROOT / '.env')
    token, hook = connection_from_env(live=True)
    telegram = Telegram(token)
    if telegram.call('getMe').get('username') != 'fom_bitrix_bot':
        raise RuntimeError('Unexpected bot identity')
    database = ROOT / 'data/crm_bot.sqlite3'
    with process_lock(database):
        store = Store(database)
        try:
            backup = ROOT / 'data/backups/before-telegram-reminders-20261007.sqlite3'
            if not backup.exists():
                with sqlite3.connect(backup) as destination:
                    store.db.backup(destination)
            localized = LocalizedTelegram(telegram, store)
            api = ReadOnlyBitrix(hook)
            registration = Registration(RegistrationSettings.load(), store, Directory(api), localized)
            member = registration.user(USER)
            if not member or member.bitrix_id != 132:
                raise RuntimeError('Expected the approved Malik account')
            config = Config(token, hook, database, ZoneInfo('Asia/Tashkent'), {USER:member}, {})
            live = LiveDeals(registration, config, api, localized)
            reminders = Reminders(registration, live, localized)
            reminders.discover()
            row, activity, deal, due = reminders.own(USER, 351449)
            if row['request_id'] != REQUEST or row['deal_id'] != 140546:
                raise RuntimeError('Unexpected reminder binding')
            operation = store.operation(REQUEST)
            preview_state = {**operation['value'], 'deadline':due.isoformat(), 'next_step':str(activity['SUBJECT'])}
            preview_result = {**operation['result'], 'activity_id':351449 if activity['COMPLETED']=='N' else None}
            with language_context(stored_language(store, USER)):
                text, _, _ = live.bot.sales.success(preview_state, preview_result)
                markup = [[{'text':phrase('heading'), 'callback_data':'rm:card:351449'},
                           {'text':'Стадия сделки' if stored_language(store,USER)!='uz' else 'Сделка босқичи', 'callback_data':'ld:stages:140546'}]]
            report_path = ROOT / 'data/reminder-launch-check-20261007.json'
            previous = json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else {}
            report = {'ok':True, 'crm_read_only':True, 'deal_id':140546, 'activity_id':351449,
                'responsible_id':132, 'deadline':due.isoformat(), 'completed':activity['COMPLETED'],
                'confirmation_html':text, 'telegram_recipient':USER, 'preview_status':previous.get('preview_status','not_sent'),
                'checked_at':datetime.now(ZoneInfo('Asia/Tashkent')).isoformat()}
            if args.send_preview and report['preview_status']=='not_sent':
                report['preview_status']='sending'
                report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
                sent = localized.call('sendMessage', {'chat_id':USER,'text':text,'parse_mode':'HTML',
                    'reply_markup':{'inline_keyboard':markup},'link_preview_options':{'is_disabled':True}})
                report['preview_status']='sent'
                report['message_id']=sent['message_id']
            elif report['preview_status']=='sending':
                raise RuntimeError('Preview result is unconfirmed; no repeated transmission')
            elif args.send_preview and previous.get('confirmation_html') != text:
                message_id = previous.get('message_id')
                if not isinstance(message_id, int) or message_id < 1:
                    raise RuntimeError('Expected the existing preview message')
                localized.call('editMessageText', {'chat_id':USER, 'message_id':message_id,
                    'text':text, 'parse_mode':'HTML', 'reply_markup':{'inline_keyboard':markup},
                    'link_preview_options':{'is_disabled':True}})
                report['message_id']=message_id
                report['preview_updated']=True
            else:
                report['message_id']=previous.get('message_id')
            report['sqlite_integrity']=store.db.execute('PRAGMA integrity_check').fetchone()[0]
            report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({key:value for key,value in report.items() if key!='confirmation_html'},ensure_ascii=False))
        finally:
            store.close()


if __name__=='__main__':
    main()
