"""Language selection precedes registration and never consumes a draft field."""
from __future__ import annotations

from copy import deepcopy

from .i18n import stored_language, tr

SETTINGS = '⚙️ Настройки'
LANGUAGE = '🌐 Язык'
RU = '🇷🇺 Русский'
UZ = '🇺🇿 Ўзбекча'
CHOICES = [[RU, UZ]]


class LanguageUI:
    def __init__(self, registration, telegram):
        self.registration, self.store, self.telegram = registration, registration.store, telegram
        bind = getattr(telegram, 'bind_registration', None)
        if callable(bind):
            bind(registration)
        # Only actual reply buttons are aliases. CRM names and user text are untouched.
        from .registration import REGISTER, PROFILE, PROFILE_LABELS, REQUESTS, MEMBERS, CANCEL as REG_CANCEL
        from .service import DEAL, PHARMACY, SUPPORT, STATS, CANCEL, CONFIRM, CHECK, SKIP_INN, SKIP_PHONE, UNBOUND
        from .okb_service import OKB, OKB_LABELS, NO_LANDMARK, CONTINUE, CREATE_CONTACT, NO_CONTACT, USE_CONTACT_PHONE
        from .live_deals import MY_CRM, MY_CRM_LABELS, RESTART_DEAL
        from .sales_intake import USE_PHARMACY, NEW_PHARMACY, SEARCH_AGAIN, USE_ADDRESS, NEXT_DAY, CONTINUE_DEAL
        from .guidance import GUIDE
        from .navigation import HOME, MORE
        from .intake_ui import ADD_LOCATION, SKIP_LOCATION, SEND_LOCATION, ADD_REMINDER, NO_REMINDER
        from .reminder_calendar import PREVIOUS_MONTH, NEXT_MONTH
        from .input_forms import TEXT_FORM, STEP_FORM
        from .personal_statistics import MY_STATS
        from .work_inbox import INBOX, NEW, MINE, SENT, STATS as REQUEST_STATS, SEND, CANCEL as REQUEST_CANCEL, DIAGNOSTICS
        labels = [REGISTER, PROFILE, REQUESTS, MEMBERS, REG_CANCEL, DEAL, PHARMACY, SUPPORT, STATS,
                  CANCEL, CONFIRM, CHECK, SKIP_INN, SKIP_PHONE, 'Изменить ИНН', UNBOUND,
                  OKB, NO_LANDMARK, CONTINUE, CREATE_CONTACT, NO_CONTACT, USE_CONTACT_PHONE,
                  MY_CRM, GUIDE, HOME, MORE, ADD_LOCATION, SKIP_LOCATION, SEND_LOCATION, ADD_REMINDER, NO_REMINDER,
                  PREVIOUS_MONTH, NEXT_MONTH,
                  TEXT_FORM, STEP_FORM, MY_STATS,
                  INBOX, NEW, MINE, SENT,
                  REQUEST_STATS, SEND, REQUEST_CANCEL, DIAGNOSTICS,
                  'Мои', 'По команде', 'Сегодня', 'Эта неделя', 'Этот месяц',
                  USE_PHARMACY, NEW_PHARMACY, SEARCH_AGAIN, USE_ADDRESS, NEXT_DAY, CONTINUE_DEAL, RESTART_DEAL,
                  '🗺 R1 ҳудуди', '🗺 R2–R3–R4 ҳудудлари', '🧭 Тақсимловчи']
        self.aliases = {tr(label, language): label for label in labels for language in ('ru', 'uz')}
        self.aliases.update({tr(label, language): OKB for label in OKB_LABELS for language in ('ru', 'uz')})
        self.aliases.update({tr(label, language): PROFILE for label in PROFILE_LABELS for language in ('ru','uz')})
        self.aliases.update({tr(label, language): MY_CRM for label in MY_CRM_LABELS for language in ('ru','uz')})

    def normalize(self, update):
        message = update.get('message')
        if not isinstance(message, dict) or not isinstance(message.get('text'), str):
            return update
        text = message['text'].strip()
        canonical = self.aliases.get(text)
        if canonical is None or canonical == text:
            return update
        result = deepcopy(update)
        result['message']['text'] = canonical
        return result

    def handle(self, update):
        callback = update.get('callback_query')
        message = callback.get('message', {}) if isinstance(callback, dict) else update.get('message', {})
        sender = callback.get('from', {}) if isinstance(callback, dict) else message.get('from', {})
        chat = message.get('chat', {})
        if chat.get('type') != 'private' or sender.get('is_bot') or not sender.get('id') or sender['id'] != chat.get('id'):
            return False
        user = int(sender['id'])
        text = str(message.get('text', '')).strip() if not callback else ''
        command = text.split(maxsplit=1)[0].split('@', 1)[0].lower() if text.startswith('/') else text
        language = stored_language(self.store, user)
        # Old inline cards remain usable while first /start always asks for a language.
        from .registration import PROFILE_LABELS
        from .i18n import language_context
        profile_labels = {tr(label, lang) for label in PROFILE_LABELS for lang in (None, 'ru', 'uz')}
        if command in {'/profile', '/settings', '/language', SETTINGS, tr(SETTINGS, 'uz'), LANGUAGE, tr(LANGUAGE, 'uz')} | profile_labels:
            with language_context(language):
                self.telegram.send(user, self.registration.profile(user), self.registration.profile_keyboard(user))
            return True
        if text in {RU, UZ}:
            had_language = bool(language)
            language = 'ru' if text == RU else 'uz'
            with self.store.db:
                self.store.db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                      ('language:'+str(user), language))
            from .i18n import language_context
            with language_context(language):
                from .app import COMMANDS
                if self.registration.commands is None:
                    self.registration.commands = COMMANDS
                self.registration.refresh_commands(user)
                from .role_content import TEXTS
                text = tr(TEXTS['language_saved']['ru'])
                if had_language:
                    text += '\n\n' + self.registration.profile(user)
                self.telegram.send(user, text, self.registration.profile_keyboard(user) if had_language else self.registration.keyboard(user))
            return True
        if not language and not callback:
            self.telegram.send(user, '👋 Выберите язык / Тилни танланг:', CHOICES)
            return True
        return False
