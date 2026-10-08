"""Read-only personal CRM overview; never consumes a form or work-inbox draft."""
from __future__ import annotations

from datetime import datetime, timedelta
import html
import re
from zoneinfo import ZoneInfo

from .api import RemoteError
from .i18n import tr, user_language
from .statistics_content import TEXTS

MY_STATS = TEXTS['menu']['ru']
KINDS = ('deal', 'pharmacy', 'contact', 'support')
PERIODS = ('today', 'week', 'month', 'all')


def phrase(key):
    return tr(TEXTS[key]['ru'])


def statistics_interval(period, now):
    if period not in PERIODS:
        raise ValueError('Unknown statistics period')
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'week':
        start -= timedelta(days=start.weekday())
    elif period == 'month':
        start = start.replace(day=1)
    elif period == 'all':
        start = None
    return start, now


class PersonalStatistics:
    def __init__(self, registration, telegram, statistics, *, simulation=None, inbox=None, clock=None):
        self.registration, self.telegram, self.statistics = registration, telegram, statistics
        self.simulation, self.inbox = simulation, inbox
        self.timezone = ZoneInfo('Asia/Tashkent')
        self.clock = clock or (lambda: datetime.now(self.timezone))
        registration.personal_statistics_enabled = True

    def interval(self, period):
        return statistics_interval(period, self.clock().astimezone(self.timezone))

    @user_language
    def render(self, user, period='month'):
        member = self.registration.user(user)
        if not member:
            return phrase('denied'), None
        if not self.registration.sales_allowed(user):
            from .role_content import TEXTS
            return tr(TEXTS['sales_only']['ru']), None
        start, now = self.interval(period)
        report = self.statistics.personal_counts(start.isoformat() if start else None, now.isoformat(),
                                                member.bitrix_id, self.registration.store, user, kinds=KINDS)
        title = phrase(period)
        if start:
            title += f' · {start:%d.%m}–{now:%d.%m.%Y}'
        width = max(len(phrase(kind)) for kind in KINDS)
        cells = [[str(report[source][kind]['count']) if 'count' in report[source][kind] else '—'
                  for source in ('bot', 'all')] for kind in KINDS]
        bot_width = max(len(phrase('bot')), *(len(row[0]) for row in cells))
        crm_width = max(len(phrase('crm')), *(len(row[1]) for row in cells))
        table = [f"{'':<{width}}  {phrase('bot'):>{bot_width}}  {phrase('crm'):>{crm_width}}"]
        table += [f"{phrase(kind):<{width}}  {row[0]:>{bot_width}}  {row[1]:>{crm_width}}"
                  for kind, row in zip(KINDS, cells)]
        text = (phrase('heading') + '\n' + html.escape(title) + '\n\n' + phrase('created') +
                '\n<pre>' + html.escape('\n'.join(table)) + '</pre>\n' + phrase('explanation'))
        if any('—' in row for row in cells):
            text += '\n\n' + phrase('unavailable')
        text += '\n\n' + phrase('updated').format(time=now.strftime('%d.%m · %H:%M'), period=phrase('period_'+period))
        buttons = [{'text': ('✓ ' if value == period else '') + phrase(value),
                    'callback_data': 'personal_stats:' + value} for value in PERIODS]
        return text, {'inline_keyboard': [buttons[:2], buttons[2:],
                       [{'text': phrase('refresh'), 'callback_data': 'personal_stats:' + period}]]}

    def handle(self, update):
        callback = update.get('callback_query')
        message = callback.get('message', {}) if isinstance(callback, dict) else update.get('message', {})
        sender = callback.get('from', {}) if isinstance(callback, dict) else message.get('from', {})
        chat = message.get('chat', {})
        if chat.get('type') != 'private' or sender.get('is_bot') or not sender.get('id') or sender['id'] != chat.get('id'):
            return False
        user = sender['id']
        if callback:
            data = str(callback.get('data', ''))
            if not data.startswith('personal_stats:'):
                return False
            period = data.removeprefix('personal_stats:')
            if period not in PERIODS:
                self.telegram.call('answerCallbackQuery', {'callback_query_id': callback['id'], 'text': phrase('choose')})
                return True
        else:
            text = str(message.get('text', '')).strip()
            if text not in {'/my_stats', MY_STATS}:
                return False
            period = 'month'
        if not self.registration.user(user):
            if callback:
                self.telegram.call('answerCallbackQuery', {'callback_query_id': callback['id'], 'text': phrase('denied')})
            else:
                self.telegram.send(user, phrase('denied'))
            return True
        if not self.registration.sales_allowed(user):
            from .role_content import TEXTS
            reason=tr(TEXTS['sales_only']['ru'])
            if callback:
                self.telegram.call('answerCallbackQuery', {'callback_query_id':callback['id'],'text':reason[:180],'show_alert':True})
            else:
                self.telegram.send(user,reason,self.registration.keyboard(user))
            return True
        if (self.simulation and self.simulation.active(user)) or (self.inbox and self.inbox.store.mode(user)):
            return False
        message_id = message.get('message_id') if callback else None
        if callback:
            self.telegram.call('answerCallbackQuery', {'callback_query_id': callback['id']})
        else:
            loading = self.telegram.call('sendMessage', {'chat_id': user, 'text': phrase('loading')})
            message_id = loading.get('message_id') if isinstance(loading, dict) else None
        body, markup = self.render(user, period)
        payload = {'chat_id': user, 'text': body, 'parse_mode': 'HTML',
                   'link_preview_options': {'is_disabled': True}}
        if markup:
            payload['reply_markup'] = markup
        # Callback messages carry plain text plus entities, not the HTML sent.
        plain_body = html.unescape(re.sub(r'</?[^>]+>', '', body))
        if callback and message.get('text') in {body, plain_body} and message.get('reply_markup') == markup:
            return True
        if isinstance(message_id, int) and not isinstance(message_id, bool) and message_id > 0:
            try:
                self.telegram.call('editMessageText', {**payload, 'message_id': message_id})
            except RemoteError as exc:
                if exc.code != 'TELEGRAM_400':
                    raise
                # A removed/old message cannot be edited; keep the result accessible.
                self.telegram.call('sendMessage', payload)
        else:
            self.telegram.call('sendMessage', payload)
        return True
