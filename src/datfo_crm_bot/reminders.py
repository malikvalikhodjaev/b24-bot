"""Bot-created CRM todos: persistent notices, completion and result comments."""
from datetime import datetime, timezone
import json
import re
from uuid import uuid4

from .api import RemoteError
from .activity_identity import matches as matches_activity
from .config import ConfigError
from .i18n import tr, user_language
from .live_deals import SOURCE
from .reminder_content import TEXTS
from .service import esc

REMINDERS = TEXTS['menu']['ru']


def phrase(key):
    return tr(TEXTS[key]['ru'])


class Reminders:
    def __init__(self, registration, live, telegram, *, clock=None):
        self.registration, self.live, self.telegram = registration, live, telegram
        self.store, self.api = registration.store, live.crm.api
        self.db = self.store.db
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.next_poll = None
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS crm_reminders(
                activity_id INTEGER PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
                deal_id INTEGER NOT NULL, responsible_id INTEGER,
                deadline TEXT, state TEXT NOT NULL DEFAULT 'open');
            CREATE TABLE IF NOT EXISTS crm_reminder_notices(
                activity_id INTEGER NOT NULL, deadline TEXT NOT NULL, user_id INTEGER NOT NULL,
                slot TEXT NOT NULL, status TEXT NOT NULL, message_id INTEGER,
                PRIMARY KEY(activity_id,deadline,user_id,slot));
            CREATE TABLE IF NOT EXISTS crm_reminder_actions(
                token TEXT NOT NULL, user_id INTEGER NOT NULL, activity_id INTEGER NOT NULL,
                kind TEXT NOT NULL, body TEXT, status TEXT NOT NULL, result_id INTEGER,
                PRIMARY KEY(token,user_id));
            CREATE TABLE IF NOT EXISTS crm_reminder_inputs(
                user_id INTEGER PRIMARY KEY, activity_id INTEGER NOT NULL, token TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS crm_reminder_updates(
                update_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL);
        ''')
        self.db.commit()
        live.reminders = self
        from .reminder_snooze import ReminderSnooze
        self.snooze = ReminderSnooze(self)

    def discover(self):
        rows = self.db.execute("""SELECT request_id, value, result FROM operations
            WHERE kind='deal' AND status IN ('succeeded','created')
            AND json_extract(result,'$.activity_id') IS NOT NULL""").fetchall()
        with self.db:
            for row in rows:
                state, result = json.loads(row['value']), json.loads(row['result'])
                if not state.get('sales_intake') or state.get('live_test'):
                    continue
                self.db.execute('INSERT OR IGNORE INTO crm_reminders(activity_id,request_id,deal_id) VALUES(?,?,?)',
                    (int(result['activity_id']), row['request_id'], int(result['id'])))

    def row(self, ident):
        row = self.db.execute('SELECT * FROM crm_reminders WHERE activity_id=?', (ident,)).fetchone()
        if not row:
            raise ConfigError(phrase('unavailable'))
        return dict(row)

    def read(self, row):
        activity = self.api.call('crm.activity.get', {'id': row['activity_id']})['result']
        deal = self.live.crm.item(row['deal_id'])
        if (not isinstance(activity, dict) or str(activity.get('ID')) != str(row['activity_id'])
                or str(activity.get('OWNER_TYPE_ID')) != '2' or str(activity.get('OWNER_ID')) != str(row['deal_id'])
                or activity.get('PROVIDER_ID') != 'CRM_TODO' or not matches_activity(activity, row['request_id'])
                or deal.get('originatorId') != SOURCE or deal.get('originId') != row['request_id']):
            raise ConfigError(phrase('changed'))
        try:
            due = datetime.fromisoformat(activity['DEADLINE'])
            responsible = int(activity['RESPONSIBLE_ID'])
            if due.tzinfo is None or responsible < 1 or activity.get('COMPLETED') not in {'Y', 'N'}:
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise ConfigError(phrase('changed')) from None
        with self.db:
            self.db.execute('UPDATE crm_reminders SET responsible_id=?,deadline=?,state=? WHERE activity_id=?',
                (responsible, due.astimezone(timezone.utc).isoformat(), 'completed' if activity['COMPLETED']=='Y' else 'open', row['activity_id']))
        return activity, deal, due

    def own(self, user, ident):
        member = self.registration.user(user)
        if not member:
            raise ConfigError(phrase('register'))
        row = self.row(ident)
        activity, deal, due = self.read(row)
        # Administrators also act only on their own reminders through these buttons.
        if int(activity['RESPONSIBLE_ID']) != member.bitrix_id:
            raise ConfigError(phrase('not_yours'))
        return row, activity, deal, due

    def buttons(self, row, *, completed=False):
        actions = [] if completed else [{'text': phrase('complete'), 'callback_data': 'rm:complete:'+str(row['activity_id'])}]
        if not completed:
            actions.append({'text': phrase('snooze'), 'callback_data': 'rm:snooze:' + str(row['activity_id'])})
        actions.append({'text': phrase('comment'), 'callback_data': 'rm:comment:'+str(row['activity_id'])})
        return [actions, [{'text': phrase('open'), 'url': self.live.crm.portal+'/crm/deal/details/'+str(row['deal_id'])+'/'}]]

    def send(self, user, text, buttons):
        return self.telegram.call('sendMessage', {'chat_id': user, 'text': text, 'parse_mode': 'HTML',
            'reply_markup': {'inline_keyboard': buttons}, 'link_preview_options': {'is_disabled': True}})

    @user_language
    def card(self, user, ident, slot=None, expected_deadline=None):
        row, activity, deal, due = self.own(user, ident)
        if slot:
            seconds = (due-self.clock()).total_seconds()
            if (activity['COMPLETED']=='Y' or due.astimezone(timezone.utc).isoformat() != expected_deadline
                    or (slot=='advance' and not 0 < seconds <= 900) or (slot=='due' and seconds > 0)):
                raise ConfigError(phrase('changed'))
        local = due.astimezone(self.live.bot.config.timezone).strftime('%d.%m.%Y · %H:%M')
        text = '<b>'+phrase('heading')+'</b>'
        if slot:
            text += '\n'+phrase('soon' if slot=='advance' else 'due')
        text += '\n\n<b>'+esc(activity.get('SUBJECT') or '')+'</b>\n'+phrase('deadline')+local
        text += '\n'+phrase('deal')+str(row['deal_id'])+' · '+esc(deal.get('title') or '')
        return self.send(user, text, self.buttons(row, completed=activity['COMPLETED']=='Y'))

    def flush(self):
        now = self.clock()
        if self.next_poll and now < self.next_poll:
            return
        from datetime import timedelta
        self.next_poll = now + timedelta(seconds=60)
        self.discover()
        rows = self.db.execute("SELECT * FROM crm_reminders WHERE state='open' ORDER BY activity_id").fetchall()
        for raw in rows:
            row = dict(raw)
            key = None
            try:
                activity, _, due = self.read(row)
                if activity['COMPLETED']=='Y':
                    continue
                seconds = (due-now).total_seconds()
                if seconds > 900:
                    continue
                recipient = self.db.execute("SELECT telegram_id FROM registrations WHERE status='approved' AND bitrix_id=?", (int(activity['RESPONSIBLE_ID']),)).fetchone()
                if not recipient:
                    continue
                slot = 'advance' if seconds > 0 else 'due'
                key = (row['activity_id'], due.astimezone(timezone.utc).isoformat(), recipient[0], slot)
                previous = self.db.execute('SELECT status FROM crm_reminder_notices WHERE activity_id=? AND deadline=? AND user_id=? AND slot=?', key).fetchone()
                if previous:
                    continue
                with self.db:
                    self.db.execute('INSERT INTO crm_reminder_notices(activity_id,deadline,user_id,slot,status) VALUES(?,?,?,?,?)', (*key, 'sending'))
                # Recheck ownership immediately before transmission, including changes in CRM.
                sent = self.card(recipient[0], row['activity_id'], slot, key[1])
                with self.db:
                    self.db.execute('UPDATE crm_reminder_notices SET status=?,message_id=? WHERE activity_id=? AND deadline=? AND user_id=? AND slot=?',
                        ('sent', sent.get('message_id') if isinstance(sent, dict) else None, *key))
            except (RemoteError, ConfigError) as exc:
                if key is not None:
                    with self.db:
                        self.db.execute("UPDATE crm_reminder_notices SET status='uncertain' WHERE activity_id=? AND deadline=? AND user_id=? AND slot=? AND status='sending'", key)
                if isinstance(exc, RemoteError) and exc.code == 'TELEGRAM_401':
                    raise
                # An unavailable CRM card or blocked Telegram recipient must not stop others.
                continue

    @user_language
    def listing(self, user):
        self.discover()
        member = self.registration.user(user)
        if not member:
            raise ConfigError(phrase('register'))
        found = False
        failed = False
        for raw in self.db.execute('SELECT * FROM crm_reminders ORDER BY deadline,activity_id').fetchall():
            try:
                activity, _, _ = self.read(dict(raw))
            except (ConfigError, RemoteError):
                failed = True
                continue
            if activity['COMPLETED']=='N' and int(activity['RESPONSIBLE_ID']) == member.bitrix_id:
                self.card(user, raw['activity_id'])
                found = True
        if not found:
            self.telegram.send(user, phrase('read_error' if failed else 'none'), self.registration.keyboard(user))

    def action(self, token, user):
        row = self.db.execute('SELECT * FROM crm_reminder_actions WHERE token=? AND user_id=?', (token, user)).fetchone()
        if not row:
            raise ConfigError(phrase('unavailable'))
        return dict(row)

    def action_status(self, token, user, status, result=None):
        with self.db:
            self.db.execute('UPDATE crm_reminder_actions SET status=?,result_id=? WHERE token=? AND user_id=?', (status, result, token, user))

    def complete(self, user, ident):
        row, activity, _, _ = self.own(user, ident)
        token = 'complete:'+str(ident)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO crm_reminder_actions(token,user_id,activity_id,kind,status) VALUES(?,?,?,?,?)', (token, user, ident, 'complete', 'prepared'))
        action = self.action(token, user)
        if activity['COMPLETED']=='Y':
            self.action_status(token, user, 'succeeded')
            self.send(user, phrase('already_completed'), self.buttons(row, completed=True))
            return
        if action['status']=='succeeded':
            raise ConfigError(phrase('old_completion'))
        if action['status'] in {'sending', 'uncertain'}:
            self.send(user, phrase('uncertain'), [[{'text':phrase('check'), 'callback_data':'rm:complete:'+str(ident)}]])
            return
        self.action_status(token, user, 'sending')
        try:
            response = self.api.call('crm.activity.update', {'id': ident, 'fields': {'COMPLETED': 'Y'}})
            if response.get('result') is not True:
                raise RemoteError('INVALID_COMPLETION_RESPONSE', uncertain=True)
            _, current, _, _ = self.own(user, ident)
            if current['COMPLETED'] != 'Y':
                raise RemoteError('COMPLETION_NOT_CONFIRMED', uncertain=True)
        except RemoteError as exc:
            self.action_status(token, user, 'uncertain' if exc.uncertain else 'rejected')
            self.send(user, phrase('uncertain' if exc.uncertain else 'denied'), [[{'text':phrase('check' if exc.uncertain else 'retry'), 'callback_data':'rm:complete:'+str(ident)}]])
            return
        self.action_status(token, user, 'succeeded')
        self.send(user, phrase('completed'), self.buttons(row, completed=True))

    def begin_comment(self, user, ident):
        self.own(user, ident)
        pending = self.db.execute('SELECT token FROM crm_reminder_inputs WHERE user_id=?', (user,)).fetchone()
        if pending and self.db.execute('SELECT 1 FROM crm_reminder_actions WHERE token=? AND user_id=?', (pending[0], user)).fetchone():
            raise ConfigError(phrase('pending_comment'))
        with self.db:
            self.db.execute('INSERT INTO crm_reminder_inputs VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET activity_id=excluded.activity_id,token=excluded.token', (user, ident, uuid4().hex[:20]))
        self.send(user, phrase('comment_prompt'), [[{'text':phrase('cancel'), 'callback_data':'rm:cancel'}]])

    def save_comment(self, user, token):
        action = self.action(token, user)
        row, activity, _, _ = self.own(user, action['activity_id'])
        if action['kind'] != 'comment':
            raise ConfigError(phrase('unavailable'))
        tag = 'Комментарий Telegram: '+token
        if action['status'] != 'succeeded':
            notes = self.api.list_all('crm.timeline.comment.list', {'filter': {'ENTITY_ID':row['deal_id'], 'ENTITY_TYPE':'deal'}, 'select':['ID','COMMENT']})
            matches = [note for note in notes if str(note.get('COMMENT') or '').rstrip().endswith(tag)]
            if len(matches)>1:
                raise ConfigError(phrase('changed'))
            if matches:
                self.action_status(token, user, 'succeeded', int(matches[0]['ID']))
            elif action['status'] in {'sending','uncertain'}:
                self.send(user, phrase('uncertain'), [[{'text':phrase('check'), 'callback_data':'rm:retry:'+token}]])
                return
            else:
                self.action_status(token, user, 'sending')
                member = self.registration.user(user)
                comment = ('Напоминание #'+str(row['activity_id'])+' — '+str(activity.get('SUBJECT') or '')
                    +'\n'+action['body']+'\nАвтор: '+member.name+' (Б24 #'+str(member.bitrix_id)+')\n'+tag)
                try:
                    response = self.api.call('crm.timeline.comment.add', {'fields': {'ENTITY_ID':row['deal_id'], 'ENTITY_TYPE':'deal', 'COMMENT':comment}})['result']
                    if isinstance(response, bool) or not str(response).isdigit() or int(response)<1:
                        raise RemoteError('INVALID_COMMENT_RESPONSE', uncertain=True)
                except RemoteError as exc:
                    self.action_status(token, user, 'uncertain' if exc.uncertain else 'rejected')
                    self.send(user, phrase('uncertain' if exc.uncertain else 'denied'), [[{'text':phrase('check' if exc.uncertain else 'retry'), 'callback_data':'rm:retry:'+token}, {'text':phrase('cancel'), 'callback_data':'rm:cancel'}]])
                    return
                self.action_status(token, user, 'succeeded', int(response))
        with self.db:
            self.db.execute('DELETE FROM crm_reminder_inputs WHERE user_id=? AND token=?', (user, token))
        self.send(user, phrase('comment_saved'), self.buttons(row, completed=activity['COMPLETED']=='Y'))

    def cancel_comment(self, user):
        with self.db:
            self.db.execute('DELETE FROM crm_reminder_inputs WHERE user_id=?', (user,))
        self.telegram.send(user, phrase('comment_cancelled'), self.registration.keyboard(user))

    def handle(self, update):
        callback = update.get('callback_query')
        message = callback.get('message', {}) if isinstance(callback, dict) else update.get('message', {})
        sender = callback.get('from', {}) if isinstance(callback, dict) else message.get('from', {})
        if message.get('chat', {}).get('type') != 'private' or sender.get('is_bot') or not sender.get('id') or sender['id'] != message['chat'].get('id'):
            return False
        user = int(sender['id'])
        if not callback and self.db.execute('SELECT 1 FROM crm_reminder_updates WHERE update_id=?', (update.get('update_id'),)).fetchone():
            return True
        text = message.get('text', '').strip() if isinstance(message.get('text', ''), str) else ''
        pending = self.db.execute('SELECT * FROM crm_reminder_inputs WHERE user_id=?', (user,)).fetchone()
        data = str(callback.get('data', '')) if callback else ''
        if callback and not data.startswith('rm:'):
            return False
        command = text.split(maxsplit=1)[0].split('@', 1)[0].lower() if text.startswith('/') else text
        if not callback and command not in {'/reminders', REMINDERS, TEXTS['menu']['uz']} and not pending:
            return False
        if not callback and pending and (command.startswith('/') and command not in {'/cancel'}):
            with self.db:
                self.db.execute('DELETE FROM crm_reminder_inputs WHERE user_id=?', (user,))
            if command != '/reminders':
                return False
        from .navigation import HOME
        menus = self.registration.keyboard(user) + self.registration.more_keyboard(user)
        menu_labels = {button for buttons in menus for button in buttons if isinstance(button, str)} | {HOME}
        if not callback and pending and text in menu_labels and text not in {REMINDERS, TEXTS['menu']['uz']}:
            with self.db:
                self.db.execute('DELETE FROM crm_reminder_inputs WHERE user_id=?', (user,))
            return False
        if not self.registration.user(user):
            self.telegram.send(user, phrase('register'), self.registration.keyboard(user))
            return True
        try:
            if callback:
                self.discover()
                try:
                    self.telegram.call('answerCallbackQuery', {'callback_query_id':callback.get('id','')})
                except RemoteError as exc:
                    if exc.code=='TELEGRAM_401':
                        raise
                if self.snooze.handle(user, data):
                    pass
                elif data == 'rm:list':
                    self.listing(user)
                elif data=='rm:cancel':
                    self.cancel_comment(user)
                elif match := re.fullmatch(r'rm:(card|complete|comment):(\d+)', data):
                    action, ident = match.groups()
                    {'card':self.card, 'complete':self.complete, 'comment':self.begin_comment}[action](user, int(ident))
                elif match := re.fullmatch(r'rm:retry:([a-f0-9]{20})', data):
                    self.save_comment(user, match[1])
                else:
                    raise ConfigError(phrase('unavailable'))
            elif command in {'/reminders', REMINDERS, TEXTS['menu']['uz']}:
                self.listing(user)
            elif pending and command in {'/cancel','Отмена',phrase('cancel')}:
                with self.db:
                    self.db.execute('INSERT OR IGNORE INTO crm_reminder_updates VALUES(?,?)', (update['update_id'], user))
                self.cancel_comment(user)
            elif pending:
                if not text or len(text)>2000:
                    raise ConfigError(phrase('comment_invalid'))
                with self.db:
                    self.db.execute('INSERT OR IGNORE INTO crm_reminder_actions(token,user_id,activity_id,kind,body,status) VALUES(?,?,?,?,?,?)', (pending['token'], user, pending['activity_id'], 'comment', text, 'prepared'))
                    self.db.execute('INSERT OR IGNORE INTO crm_reminder_updates VALUES(?,?)', (update['update_id'], user))
                self.save_comment(user, pending['token'])
        except ConfigError as exc:
            self.telegram.send(user, '⚠️ '+esc(exc))
        except RemoteError as exc:
            if exc.code == 'TELEGRAM_401':
                raise
            self.telegram.send(user, phrase('read_error'))
        return True
