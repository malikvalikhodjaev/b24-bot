"""Navigation never silently abandons a form or an uncertain CRM write."""
from __future__ import annotations

import html
import re

from .i18n import tr
from .navigation_content import TEXTS

HOME = '🏠 Главное меню'
MORE = '☰ Ещё'


def phrase(key):
    return tr(TEXTS[key]['ru'])


def form_state(state):
    return state.get('pharmacy_form', {}) if state and state.get('step') == 'sales_okb' else state


def step_text(state, *, request=False):
    state = form_state(state)
    key = 'step_' + ('request_' if request else '') + str((state or {}).get('step', ''))
    return phrase('step') + phrase(key) if key in TEXTS else ''


def is_phone(text):
    return bool(re.fullmatch(r'\+?[0-9()\s-]+', text)) and 7 <= len(re.sub(r'\D', '', text)) <= 15


def check_phone_step(text, state, *, request=False):
    """Reject only a standalone phone in a step that clearly requires other data.

    INN and actual phone fields accept numeric input; dates, names containing a
    number, street addresses and descriptions containing a phone remain intact.
    """
    state = form_state(state)
    if not state or not is_phone(text):
        return
    step = state.get('step')
    names = {'title', 'description', 'okb_company_name', 'okb_title', 'okb_contact_name',
             'okb_city_search', 'sales_pharmacy', 'sales_description', 'sales_next_step'}
    choices = {'company', 'okb_company', 'okb_contact', 'okb_contact_create', 'okb_contact_position',
               'okb_region', 'okb_city_choice', 'okb_program', 'live_category', 'live_initial_stage',
               'sales_match', 'sales_pharmacy_confirm', 'confirm', 'stats_scope', 'stats_period'}
    if request:
        names, choices = {'title', 'description'}, {'deal', 'recipient', 'confirm'}
        if step == 'recipient' and not state.get('direct_recipient'):
            raise ValueError(tr('Это номер телефона. Сейчас выберите «На распределение» или «Назначить технику». Контактное лицо можно указать в форме заявки.'))
    if step in names | choices:
        raise ValueError(phrase('phone_text' if step in names else 'phone_choice'))


def input_error(state, reason, *, request=False):
    current = step_text(state, request=request)
    return '\n\n'.join(part for part in (current, html.escape(str(reason)), phrase('retry')) if part)


def retry_keyboard(state, store=None, user=None):
    from .service import CANCEL, CHECK, CONFIRM, SKIP_INN, SKIP_PHONE
    from .okb_service import CREATE_CONTACT, NO_CONTACT, USE_CONTACT_PHONE, NO_LANDMARK
    from .sales_intake import NEW_PHARMACY, SEARCH_AGAIN, USE_PHARMACY, USE_ADDRESS, NEXT_DAY
    if state and state.get('step') in {'okb_inn', 'okb_bulk_input', 'sales_pharmacy', 'sales_bulk_input'}:
        from .input_forms import form_keyboard
        return form_keyboard(state, store, user) + [[HOME]]
    state = form_state(state) or {}
    step = state.get('step')
    fields = {'okb_company': 'candidates', 'okb_contact': 'contact_candidates',
              'okb_contact_position': 'contact_position_candidates', 'okb_region': 'region_candidates',
              'okb_city_choice': 'city_candidates', 'okb_program': 'program_candidates'}
    if step in fields:
        rows = [[f"{i}. {row['title'][:100]}"] for i, row in enumerate(state.get(fields[step], []), 1)]
    elif step in {'live_category', 'live_initial_stage', 'sales_match', 'company'}:
        field = {'live_category': 'category_candidates', 'live_initial_stage': 'stage_candidates',
                 'sales_match': 'pharmacy_candidates', 'company': 'candidates'}[step]
        rows = [[str(i)] for i in range(1, len(state.get(field, [])) + 1)]
        if step == 'sales_match':
            rows += [[NEW_PHARMACY], [SEARCH_AGAIN]]
        if step == 'company':
            rows += [['Изменить ИНН']]
            if not state.get('candidates'):
                from .service import UNBOUND
                rows += [[UNBOUND]]
    else:
        from .intake_ui import ADD_LOCATION, SKIP_LOCATION, ADD_REMINDER, NO_REMINDER, location_keyboard
        rows = {'okb_contact_create': [[CREATE_CONTACT], [NO_CONTACT]],
                'okb_contact_phone': [[USE_CONTACT_PHONE]], 'okb_phone': [[SKIP_PHONE]],
                'phone': [[SKIP_PHONE]], 'sales_phone': [[SKIP_PHONE]], 'inn': [[SKIP_INN]],
                'okb_landmark': [[NO_LANDMARK]], 'sales_deadline': [[NEXT_DAY]],
                'sales_pharmacy_confirm': [[USE_PHARMACY], [SEARCH_AGAIN, NEW_PHARMACY]],
                'stats_scope': [['Мои', 'По команде']],
                'stats_period': [['Сегодня', 'Эта неделя', 'Этот месяц']],
                'confirm': [[CONFIRM]]}.get(step, [])
        if step == 'sales_address' and state.get('pharmacy', {}).get('address'):
            rows = [[USE_ADDRESS]]
        if step == 'okb_location_choice':
            rows = [[ADD_LOCATION, SKIP_LOCATION]]
        if step == 'okb_location':
            rows = location_keyboard()[:-1]
        if step == 'sales_reminder':
            rows = [[ADD_REMINDER, NO_REMINDER]]
        if step == 'sales_time':
            times = state.get('reminder_times', [])
            rows = [times[i:i+4] for i in range(0, len(times), 4)]
        if step == 'sales_deadline' and state.get('calendar_options'):
            from .reminder_calendar import PREVIOUS_MONTH, NEXT_MONTH
            days = list(state['calendar_options'])
            rows = [days[i:i+7] for i in range(0, len(days), 7)] + [[PREVIOUS_MONTH, NEXT_MONTH], [NEXT_DAY]]
    return rows + [[CANCEL, HOME]]


def menu_text(registration=None, user=None, *, cancelled=False, text=''):
    sales = not registration or registration.sales_allowed(user)
    intro = phrase('cancelled' if cancelled else 'phone_idle' if is_phone(text) and sales else 'phone_idle_tech' if is_phone(text) else 'unknown' if text else 'where')
    lines = []
    if sales and (not registration or registration.live_deals_enabled):
        lines += [phrase('sales'), phrase('support')]
    if sales and (not registration or registration.okb_enabled):
        lines += [phrase('okb')]
    if registration and registration.support_inbox_allowed(user):
        lines += [phrase('inbox')]
    if not registration or registration.live_deals_enabled:
        lines += [phrase('crm')]
    return intro + ('\n\n' + '\n'.join(lines) if lines else '') + ('\n\n' + phrase('where') if text or cancelled else '')


class Navigation:
    def __init__(self, registration, telegram, simulation=None, inbox=None):
        self.registration, self.telegram = registration, telegram
        self.simulation, self.inbox = simulation, inbox

    def handle(self, update):
        from .service import CANCEL, CHECK, DEAL, PHARMACY, SUPPORT, STATS
        from .okb_service import OKB
        from .live_deals import MY_CRM
        from .work_inbox import CANCEL as REQUEST_CANCEL, NEW, INBOX, MINE, SENT, STATS as REQUEST_STATS
        message = update.get('message', {})
        sender, chat = message.get('from', {}), message.get('chat', {})
        if chat.get('type') != 'private' or sender.get('is_bot') or sender.get('id') != chat.get('id') or not sender.get('id'):
            return False
        user = int(sender['id'])
        text = message.get('text', '')
        text = text.strip() if isinstance(text, str) else ''
        command = text.split(maxsplit=1)[0].split('@', 1)[0].lower() if text.startswith('/') else text
        cancelled = command in {'/cancel', '/request_cancel', CANCEL, REQUEST_CANCEL}
        sections = {DEAL, PHARMACY, SUPPORT, OKB, STATS, MY_CRM, NEW, INBOX, MINE, SENT, REQUEST_STATS,
                    '/deal', '/support', '/request', '/pharmacy', '/okb', '/stats', '/my_crm',
                    '/requests', '/requests_mine', '/requests_sent', '/requests_stats'}
        if command not in {HOME, MORE, '/menu', '/start'} | sections and not cancelled:
            return False
        if not self.registration.user(user) or self.registration.store.registration_session(user):
            return False
        if ((self.simulation and self.simulation.active(user)) or (self.inbox and self.inbox.store.mode(user))):
            return False
        if command == MORE:
            self.telegram.send(user, phrase('more_heading'), self.registration.more_keyboard(user))
            return True
        store = self.registration.store
        # Keep this guard even if a crash lost the active session.
        if store.unfinished(user):
            self.telegram.send(user, phrase('pending'), [[CHECK]])
            return True
        state = store.session(user)
        work = self.registration.work_inbox
        draft = work.store.session(user) if work else None
        active = bool(draft or (state and state.get('step') not in {'done', 'stats_scope', 'stats_period'}))
        if (command in sections or command == '/start') and not active:
            return False
        saved = store.delivery(int(update['update_id']))
        if saved:
            self.telegram.send(user, saved['text'], saved['keyboard'])
            return True
        response = menu_text(self.registration, user, cancelled=cancelled)
        keyboard = self.registration.keyboard(user)
        if cancelled:
            if state and state.get('request_id') and store.operation_wrote(state['request_id']):
                response += '\n\n' + phrase('saved')
            if draft:
                work.store.set_session(user, None)
            state = None
        elif command in sections:
            response = step_text(draft or state, request=bool(draft)) + '\n\n' + phrase('active')
            if draft:
                work.prompt(user, draft, error=phrase('active'))
                return True
            keyboard = retry_keyboard(state)
        elif active:
            response = step_text(draft or state, request=bool(draft)) + '\n\n' + phrase('active') + '\n\n' + response
            keyboard = [[REQUEST_CANCEL if draft else CANCEL]] + keyboard
        store.commit_reply(int(update['update_id']), user, user, state, response, keyboard)
        self.telegram.send(user, response, keyboard)
        return True
