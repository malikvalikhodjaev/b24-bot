"""One-message and Mini App inputs use the same validated pharmacy draft."""
from datetime import datetime
import json
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from .config import ROOT
from .i18n import tr, stored_language
from .intake_ui import coordinates
from .service import normalize_inn, normalize_phone

TEXT_FORM = '📝 Заполнить одним сообщением'
STEP_FORM = 'Пошаговое заполнение'
WEB_FORM = '📋 Открыть форму'

ALIASES = {'инн': 'inn', 'фирма': 'company_name', 'компания': 'company_name', 'аптека': 'title',
           'телефон': 'phone', 'имя': 'contact_name', 'должность': 'position', 'регион': 'business_region',
           'город/район': 'city', 'город': 'city', 'адрес': 'address', 'программа': 'program',
           'напоминание': 'next_step', 'срок': 'deadline'}
ALIASES.update({'дорихона': 'title', 'компания номи': 'company_name', 'исм': 'contact_name',
                'лавозим': 'position', 'ҳудуд': 'business_region', 'шаҳар/туман': 'city',
                'манзил': 'address', 'дастур': 'program', 'эслатма': 'next_step', 'муддат': 'deadline'})


def text_template():
    return tr('Заполните поля и отправьте всё одним сообщением. Телефон и контакт можно оставить пустыми; напоминание необязательно.') + '\n\n' + (
        tr('ИНН: \nФирма: \nАптека: \nТелефон: \nИмя: \nДолжность: \nРегион: \nГород/район: \nАдрес: \nПрограмма: \nНапоминание: \nСрок: '))


def parse_text(text):
    values = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        label, separator, value = line.partition(':')
        key = ALIASES.get(label.strip().casefold())
        if not separator or not key:
            raise ValueError(tr('Не понял название поля. Используйте строки из шаблона.'))
        if key in values:
            raise ValueError(tr('Одно поле указано дважды. Оставьте одно значение: ') + label.strip())
        values[key] = value.strip()
    values['create_contact'] = bool(values.get('contact_name'))
    values['reminder'] = bool(values.get('next_step'))
    return values


def form_keyboard(state, store=None, user=None):
    from .service import CANCEL
    rows = []
    publication = ROOT / 'data/miniapp-publication.json'
    try:
        record = json.loads(publication.read_text(encoding='utf-8'))
        url = record['url'] if record.get('verified') is True else ''
    except (OSError, KeyError, ValueError):
        url = ''
    parsed = urlsplit(url)
    if parsed.scheme == 'https' and parsed.netloc == 'fom-analytics.uz' and parsed.path == '/bot-form/':
        params = {'token': state['request_id'], 'mode': state.get('form_mode') or ('deal' if state.get('sales_intake') else 'pharmacy'),
                  'lang': (stored_language(store, user) or 'ru') if store and user else 'ru'}
        rows.append([{'text': WEB_FORM, 'web_app': {'url': url + '?' + urlencode(params)}}])
    return rows + [[CANCEL]]


def launch_button(store, user, mode, label):
    """Open a form directly with a token bound to this registered user."""
    key = 'webapp-launch:'+mode+':'+str(user)
    row = store.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    record = json.loads(row['value']) if row else {}
    if not record or record.get('used'):
        record = {'token':'datfo-form-'+uuid4().hex, 'used':False}
        with store.db:
            store.db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                             (key,json.dumps(record)))
    keyboard = form_keyboard({'request_id':record['token'],'form_mode':mode},store,user)
    button = keyboard[0][0] if keyboard and isinstance(keyboard[0][0],dict) else None
    return {**button,'text':label} if button else label


def launch_matches(store, user, mode, token):
    row = store.db.execute('SELECT value FROM settings WHERE key=?',('webapp-launch:'+mode+':'+str(user),)).fetchone()
    record = json.loads(row['value']) if row else {}
    return bool(isinstance(token,str) and record.get('token')==token and record.get('used') is False)


def consume_launch(store, user, mode, token):
    if not launch_matches(store,user,mode,token):
        raise ValueError(tr('Эта форма уже закрыта. Откройте новую форму через меню.'))
    with store.db:
        store.db.execute('UPDATE settings SET value=? WHERE key=?',
            (json.dumps({'token':token,'used':True}),'webapp-launch:'+mode+':'+str(user)))


def pick(value, rows, label):
    value = str(value or '').strip()
    matches = [row for row in rows if value == str(row['id']) or value.casefold() == row['title'].strip().casefold()]
    if len(matches) != 1:
        raise ValueError(tr('Выберите точное значение поля «') + label + tr('» из списка.'))
    return matches[0]


def collect(data, crm, config):
    if not isinstance(data, dict):
        raise ValueError(tr('Форма не распознана. Откройте её заново.'))
    limits = {'company_name': 120, 'title': 120, 'contact_name': 100, 'address': 500}
    result = {'inn': normalize_inn(str(data.get('inn', '')), config.inn_lengths),
              'landmark': '', 'bulk_ready': True}
    for key, limit in limits.items():
        value = data.get(key, '')
        if not isinstance(value, str) or len(value.strip()) > limit:
            raise ValueError(tr('Слишком длинное или неверное значение поля: ') + key)
        result[key] = value.strip()
    for key, label in (('title', 'Аптека'), ('address', 'Адрес')):
        if not result[key]:
            raise ValueError(tr('Заполните поле: ') + tr(label))
    phone = data.get('phone', '')
    result['phone'] = normalize_phone(str(phone)) if phone else ''
    if data.get('contact_policy')=='optional' and data.get('create_contact') is True and not result['phone']:
        raise ValueError(tr('Чтобы создать контакт, укажите его телефон.'))
    region = pick(data.get('business_region'), crm.choices('business_region'), tr('Бизнес-регион'))
    result.update(business_region=region, city=pick(data.get('city'), crm.cities_for_region(region['id']), tr('Город/район')))
    if 'current_program' in config.targets['pharmacy'].fields:
        result['current_program'] = pick(data.get('program'), crm.choices('current_program'), tr('Текущая программа'))
    if data.get('location'):
        result['location'] = coordinates(data['location'])
    result['create_contact'] = data.get('create_contact') is True and bool(result['phone'])
    result['phone_only'] = data.get('contact_policy') == 'optional' and not result['create_contact']
    result['bulk_position'] = data.get('position')
    return result


def reminder(data, timezone, now):
    enabled = data.get('reminder') is True
    if not enabled:
        return {'reminder': False, 'next_step': '', 'deadline': '', 'description': ''}
    action, deadline = data.get('next_step'), data.get('deadline')
    if 'reminder_action' in data:
        from .sales_options import REMINDER_ACTIONS
        key = data['reminder_action']
        if not isinstance(key, str) or key not in REMINDER_ACTIONS:
            raise ValueError(tr('Выберите действие для напоминания.'))
        if key != 'other':
            action = tr(REMINDER_ACTIONS[key][0])
    if not isinstance(action, str) or not action.strip() or len(action) > 1000 or not isinstance(deadline, str):
        raise ValueError(tr('Для напоминания заполните действие и дату со временем.'))
    try:
        if 'T' in deadline:
            due = datetime.fromisoformat(deadline)
            if due.tzinfo:
                raise ValueError
        else:
            due = datetime.strptime(deadline, '%d.%m.%Y %H:%M')
        due = due.replace(tzinfo=timezone)
    except ValueError:
        raise ValueError(tr('Для напоминания нужна дата и время: ДД.ММ.ГГГГ ЧЧ:ММ.')) from None
    if due <= now:
        raise ValueError(tr('Срок дела должен быть в будущем.'))
    return {'reminder': True, 'next_step': action.strip(), 'deadline': due.isoformat(), 'description': ''}
