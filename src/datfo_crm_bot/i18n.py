"""Translate system phrases before inserting CRM names or user-entered values."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
from .help_content import HELP_TEXTS
from .navigation_content import TEXTS as NAVIGATION_TEXTS
from .intake_content import TEXTS as INTAKE_TEXTS
from .reminder_content import TEXTS as REMINDER_TEXTS
from .statistics_content import TEXTS as STATISTICS_TEXTS
from .role_content import TEXTS as ROLE_TEXTS
from .support_content import TEXTS as SUPPORT_TEXTS

_language = ContextVar('bot_language', default=None)
_catalog = json.loads(Path(__file__).with_name('translations.json').read_text(encoding='utf-8'))
for translations in HELP_TEXTS.values():
    for phrase in translations.values():
        _catalog[phrase] = translations
for translations in [*NAVIGATION_TEXTS.values(), *INTAKE_TEXTS.values(), *REMINDER_TEXTS.values(), *STATISTICS_TEXTS.values(), *ROLE_TEXTS.values(), *SUPPORT_TEXTS.values()]:
    for phrase in translations.values():
        _catalog[phrase] = translations


def tr(source: str, language: str | None = None) -> str:
    language = language or _language.get()
    if language is None:
        return source
    entry = _catalog.get(source)
    if entry:
        return entry.get(language, source)
    body = source.strip()
    entry = _catalog.get(body)
    if entry and body:
        begin = len(source)-len(source.lstrip())
        end = len(source.rstrip())
        return source[:begin]+entry.get(language, body)+source[end:]
    return source


@contextmanager
def language_context(language):
    token = _language.set(language)
    try:
        yield
    finally:
        _language.reset(token)


def stored_language(store, user):
    row = store.db.execute('SELECT value FROM settings WHERE key=?', ('language:'+str(user),)).fetchone()
    return row[0] if row and row[0] in {'ru', 'uz'} else None


def user_language(method):
    """Cards sent by the outbox use the recipient's language, not the actor's."""
    def wrapped(self, user, *args, **kwargs):
        with language_context(stored_language(self.registration.store, user)):
            return method(self, user, *args, **kwargs)
    return wrapped


class LocalizedTelegram:
    def __init__(self, telegram, store):
        self.telegram, self.store = telegram, store
        self.registration = None

    def bind_registration(self, registration):
        self.registration = registration

    def _markup(self, markup, language, chat_id=None):
        result = dict(markup)
        if 'keyboard' in result and self.registration and chat_id is not None:
            result['keyboard'] = self.registration.direct_form_keyboard(chat_id, result['keyboard'])
        for key in ('keyboard', 'inline_keyboard'):
            if key in result:
                result[key] = [[tr(button, language) if isinstance(button, str) else
                    {**button, 'text': tr(button['text'], language)} for button in row] for row in result[key]]
        return result

    def send(self, chat_id, text, keyboard=None):
        language = stored_language(self.store, chat_id)
        localized = self._markup({'keyboard': keyboard}, language, chat_id)['keyboard'] if keyboard is not None else None
        return self.telegram.send(chat_id, tr(text, language), localized)

    def call(self, method, params=None):
        params = dict(params or {})
        if method == 'sendMessage':
            language = stored_language(self.store, params['chat_id'])
            params['text'] = tr(params['text'], language)
            if 'reply_markup' in params:
                params['reply_markup'] = self._markup(params['reply_markup'], language, params['chat_id'])
        return self.telegram.call(method, params)

    def __getattr__(self, name):
        return getattr(self.telegram, name)
