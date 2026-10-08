"""Ownership-checked calendar and journalled rescheduling of the same native CRM todo."""
import calendar
from datetime import date, datetime, timedelta, timezone
import json
import re
from uuid import uuid4

from .api import RemoteError
from .config import ConfigError
from .i18n import tr
from .service import esc


class ReminderSnooze:
    def __init__(self, reminders):
        self.reminders, self.db = reminders, reminders.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS crm_reminder_snooze_inputs(
            user_id INTEGER PRIMARY KEY,token TEXT NOT NULL UNIQUE,value TEXT NOT NULL)''')
        self.db.commit()

    def phrase(self, key):
        from .reminders import phrase
        return phrase(key)

    def now(self):
        return self.reminders.clock().astimezone(self.reminders.live.bot.config.timezone)

    def save_input(self, user, token, state):
        with self.db:
            self.db.execute('INSERT INTO crm_reminder_snooze_inputs VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET token=excluded.token,value=excluded.value',
                            (user, token, json.dumps(state)))

    def input(self, user, token):
        row = self.db.execute('SELECT value FROM crm_reminder_snooze_inputs WHERE user_id=? AND token=?', (user, token)).fetchone()
        if not row:
            raise ConfigError(self.phrase('unavailable'))
        return json.loads(row[0])

    def begin(self, user, ident):
        row, activity, _, due = self.reminders.own(user, ident)
        if activity['COMPLETED'] == 'Y':
            raise ConfigError(self.phrase('already_completed'))
        pending = self.db.execute("SELECT token FROM crm_reminder_actions WHERE user_id=? AND kind='snooze' AND status IN ('sending','uncertain')", (user,)).fetchone()
        if pending:
            self.reminders.send(user, self.phrase('uncertain'), [[{'text': self.phrase('check'), 'callback_data': 'rm:sconfirm:' + pending[0]}]])
            return
        token = uuid4().hex[:20]
        state = {'activity_id': ident, 'expected_deadline': due.astimezone(timezone.utc).isoformat(),
                 'expected_owner': int(activity['RESPONSIBLE_ID']), 'month': self.now().strftime('%Y-%m')}
        self.save_input(user, token, state)
        self.calendar(user, token)

    def calendar(self, user, token):
        state = self.input(user, token)
        _, activity, _, _ = self.reminders.own(user, state['activity_id'])
        if activity['COMPLETED'] == 'Y':
            raise ConfigError(self.phrase('already_completed'))
        first = date.fromisoformat(state['month'] + '-01')
        today = self.now().date()
        rows = [[{'text': tr('Сегодня'), 'callback_data': f'rm:sday:{token}:{today:%Y%m%d}'},
                 {'text': tr('Завтра'), 'callback_data': f'rm:sday:{token}:{today + timedelta(days=1):%Y%m%d}'}]]
        for week in calendar.monthcalendar(first.year, first.month):
            buttons = []
            for day in week:
                chosen = date(first.year, first.month, day) if day else None
                valid = chosen is not None and chosen >= today
                buttons.append({'text': str(day) if valid else '·', 'callback_data': f'rm:sday:{token}:{chosen:%Y%m%d}' if valid else 'rm:noop'})
            rows.append(buttons)
        rows += [[{'text': '‹', 'callback_data': 'rm:smonth:' + token + ':p'},
                  {'text': '›', 'callback_data': 'rm:smonth:' + token + ':n'}],
                 [{'text': self.phrase('snooze_cancel'), 'callback_data': 'rm:scancel:' + token}]]
        self.reminders.send(user, self.phrase('snooze_date') + '\n\n<b>' + first.strftime('%m.%Y') + '</b>\n' + tr('Пн · Вт · Ср · Чт · Пт · Сб · Вс') + '\n🕒 ' + tr('Время Ташкента'), rows)

    def hours(self, user, token, chosen):
        state = self.input(user, token)
        now = self.now()
        day = date.fromisoformat(chosen)
        months = (day.year - now.year) * 12 + day.month - now.month
        if day < now.date() or months > 12:
            raise ConfigError(self.phrase('snooze_future'))
        times = [f'{hour:02}{minute:02}' for hour in range(24) for minute in (0, 30)
                 if day > now.date() or (hour, minute) > (now.hour, now.minute)]
        if not times:
            raise ConfigError(self.phrase('snooze_future'))
        state['chosen_date'] = chosen
        self.save_input(user, token, state)
        buttons = [{'text': value[:2] + ':' + value[2:], 'callback_data': 'rm:stime:' + token + ':' + value} for value in times]
        rows = [buttons[index:index + 4] for index in range(0, len(buttons), 4)]
        rows += [[{'text': self.phrase('snooze_back'), 'callback_data': 'rm:scalendar:' + token},
                  {'text': self.phrase('snooze_cancel'), 'callback_data': 'rm:scancel:' + token}]]
        self.reminders.send(user, self.phrase('snooze_hour') + '\n📅 ' + day.strftime('%d.%m.%Y') + '\n🕒 ' + tr('Время Ташкента'), rows)

    def choose(self, user, token, value):
        # Old hour-only buttons remain valid; new buttons include 00/30 minutes.
        if not re.fullmatch(r'(?:[01]\d|2[0-3])(?:00|30)?', value):
            raise ConfigError(self.phrase('unavailable'))
        clock = value + ':00' if len(value) == 2 else value[:2] + ':' + value[2:]
        existing = self.db.execute('SELECT * FROM crm_reminder_actions WHERE token=? AND user_id=?', (token, user)).fetchone()
        if existing and existing['status'] != 'prepared':
            # Repeated presses reconcile the same selection; never replace an in-flight write.
            chosen = json.loads(existing['body'])
            if existing['kind']!='snooze' or datetime.fromisoformat(chosen['deadline']).strftime('%H:%M')!=clock:
                raise ConfigError(self.phrase('unavailable'))
            return self.confirm(user,token)
        state = self.input(user, token)
        if 'chosen_date' not in state:
            raise ConfigError(self.phrase('unavailable'))
        due = datetime.fromisoformat(state['chosen_date'] + 'T' + clock).replace(tzinfo=self.now().tzinfo)
        if due <= self.now():
            raise ConfigError(self.phrase('snooze_future'))
        body = json.dumps({**state, 'deadline': due.isoformat()})
        with self.db:
            existing = self.db.execute('SELECT status FROM crm_reminder_actions WHERE token=? AND user_id=?', (token, user)).fetchone()
            if existing and existing[0] != 'prepared':
                raise ConfigError(self.phrase('unavailable'))
            self.db.execute('''INSERT INTO crm_reminder_actions(token,user_id,activity_id,kind,body,status)
                VALUES(?,?,?,'snooze',?,'prepared') ON CONFLICT(token,user_id) DO UPDATE SET body=excluded.body''',
                (token, user, state['activity_id'], body))
        self.reminders.telegram.send(user, self.phrase('snooze_saving'))
        self.confirm(user,token)

    def confirm(self, user, token):
        action = self.reminders.action(token, user)
        if action['kind'] != 'snooze':
            raise ConfigError(self.phrase('unavailable'))
        state = json.loads(action['body'])
        row, activity, _, current = self.reminders.own(user, action['activity_id'])
        wanted = datetime.fromisoformat(state['deadline'])
        if activity['COMPLETED'] == 'Y':
            raise ConfigError(self.phrase('already_completed'))
        if current == wanted:
            self.reminders.action_status(token, user, 'succeeded')
        elif action['status'] in {'sending', 'uncertain'}:
            # Recovery is read-only: a lost response never causes a second native update.
            self.reminders.send(user, self.phrase('uncertain'), [[{'text': self.phrase('check'), 'callback_data': 'rm:sconfirm:' + token}]])
            return
        elif action['status'] == 'succeeded':
            raise ConfigError(self.phrase('snooze_changed'))
        else:
            self.input(user, token)  # Cancelled selections cannot mutate the native activity.
            if wanted <= self.now():
                raise ConfigError(self.phrase('snooze_future'))
            if (current.astimezone(timezone.utc).isoformat() != state['expected_deadline']
                    or int(activity['RESPONSIBLE_ID']) != state['expected_owner']):
                raise ConfigError(self.phrase('snooze_changed'))
            self.reminders.action_status(token, user, 'sending')
            try:
                result = self.reminders.api.call('crm.activity.todo.updateDeadline',
                    {'id': row['activity_id'], 'ownerTypeId': 2, 'ownerId': row['deal_id'], 'value': wanted.isoformat()})['result']
                if not isinstance(result, dict) or str(result.get('id')) != str(row['activity_id']):
                    raise RemoteError('INVALID_SNOOZE_RESPONSE', uncertain=True)
                _, latest, _, deadline = self.reminders.own(user, row['activity_id'])
                if deadline != wanted or latest['COMPLETED'] != 'N':
                    raise RemoteError('SNOOZE_NOT_CONFIRMED', uncertain=True)
            except RemoteError as exc:
                self.reminders.action_status(token, user, 'uncertain' if exc.uncertain else 'rejected')
                self.reminders.send(user, self.phrase('uncertain' if exc.uncertain else 'snooze_denied'),
                    [[{'text': self.phrase('check' if exc.uncertain else 'retry'), 'callback_data': 'rm:sconfirm:' + token}]])
                return
            self.reminders.action_status(token, user, 'succeeded')
        with self.db:
            self.db.execute('DELETE FROM crm_reminder_snooze_inputs WHERE user_id=? AND token=?', (user, token))
        self.reminders.send(user, self.phrase('snooze_saved') + '\n<b>' + wanted.strftime('%d.%m.%Y · %H:%M') + '</b> · ' + tr('Ташкент'), self.reminders.buttons(row))

    def handle(self, user, data):
        if data == 'rm:noop':
            return True
        if match := re.fullmatch(r'rm:snooze:([1-9]\d*)', data):
            self.begin(user, int(match[1]))
        elif match := re.fullmatch(r'rm:s(day|time|month):([a-f0-9]{20}):([0-9]{8}|[0-9]{4}|[0-9]{2}|[pn])', data):
            action, token, value = match.groups()
            if action == 'time' and len(value) in {2,4}:
                self.choose(user,token,value)
                return True
            state = self.input(user, token)
            if action == 'day' and len(value) == 8:
                try:
                    chosen = datetime.strptime(value, '%Y%m%d').date().isoformat()
                except ValueError:
                    raise ConfigError(self.phrase('unavailable')) from None
                self.hours(user, token, chosen)
            elif action == 'month' and value in {'p', 'n'}:
                first = date.fromisoformat(state['month'] + '-01')
                index = first.year * 12 + first.month - 1 + (1 if value == 'n' else -1)
                minimum = self.now().date().replace(day=1)
                target = date(index // 12, index % 12 + 1, 1)
                if not 0 <= (target.year - minimum.year) * 12 + target.month - minimum.month <= 12:
                    raise ConfigError(self.phrase('snooze_future'))
                state['month'] = target.strftime('%Y-%m')
                self.save_input(user, token, state)
                self.calendar(user, token)
            else:
                raise ConfigError(self.phrase('unavailable'))
        elif match := re.fullmatch(r'rm:s(confirm|cancel|calendar):([a-f0-9]{20})', data):
            action, token = match.groups()
            if action == 'confirm':
                self.confirm(user, token)
            elif action == 'calendar':
                self.calendar(user, token)
            else:
                self.input(user, token)
                with self.db:
                    self.db.execute('DELETE FROM crm_reminder_snooze_inputs WHERE user_id=? AND token=?', (user, token))
                self.reminders.telegram.send(user, self.phrase('snooze_cancelled'))
        else:
            return False
        return True
