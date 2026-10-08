"""Read-only office catalogue snapshot, resolved to native Bitrix IDs without guessing."""
import json
import re
from uuid import UUID

from .api import RemoteError
from .config import ROOT
from .i18n import tr
from .support_types import UNKNOWN_TITLE, program_ids

# Explicit equivalences for labels that the office UI shortened. No UUID/enum-ID equivalence is assumed.
ALIASES = {
    'Не могут войти в ПО': ['[FA] Не могут войти в F-Apteka', '[FK] Не могут войти в F-Kassa', '[FS] Не могут войти в FS'],
    'Не обновляется': ['[FGlobal] F-Global не обновляется'],
    'Отсоединить филиал': ['[FGlobal] Отсоединить филиал от F-Global'],
    'Сбой работы модуля': ['[F-Employee] Сбой работы модуля F-Employee'],
    'Данные не приходят': ['[AA] Данные не приходят в АА'],
    'Переустановка ВК': ['[FK] Переустановка'],
}


def snapshot():
    value = json.loads((ROOT/'support-office-types.json').read_text(encoding='utf-8'))
    rows = value['rows']
    if not rows or len({row[0] for row in rows}) != len(rows):
        raise RemoteError('INVALID_OFFICE_CATALOGUE')
    for ident, title in rows:
        if str(UUID(ident)) != ident or not isinstance(title,str) or not title.strip():
            raise RemoteError('INVALID_OFFICE_CATALOGUE')
    return value


def normalized(title, *, prefix=True):
    if prefix:
        title = re.sub(r'^\[[^\]]+\]\s*', '', title)
    title = title.casefold().replace('ё','е').replace('(нет галочки)','')
    return re.sub(r'[^\w]+','',title)


def candidates(title, choices):
    if title == 'Неопознанная ошибка':
        return [row for row in choices['types'] if row['title'] == UNKNOWN_TITLE]
    if title in ALIASES:
        aliases = {normalized(value,prefix=False) for value in ALIASES[title]}
        return [row for row in choices['types'] if normalized(row['title'],prefix=False) in aliases]
    return [row for row in choices['types'] if normalized(row['title']) == normalized(title)]


def resolve(ident, product_id, choices):
    record = next((row for row in snapshot()['rows'] if 'office:'+row[0] == ident), None)
    product = next((row for row in choices['programs'] if row['id'] == product_id), None)
    if not record or (product_id and not product):
        raise ValueError(tr('Список типов обращения изменился. Откройте форму заново и выберите тип.'))
    matches = candidates(record[1],choices)
    grouped = sorted({value for row in matches for value in program_ids(row['title'],choices['programs'])})
    if grouped and not product_id:
        raise ValueError(tr('Для этого типа обращения выберите программу, по которой нужна помощь.'))
    if grouped and product_id not in grouped:
        raise ValueError(tr('Этот тип обращения относится к другой программе. Проверьте программу и тип в форме.'))
    if product_id:
        matches = [row for row in matches if not program_ids(row['title'],choices['programs'])
                   or product_id in program_ids(row['title'],choices['programs'])]
    fallback = len(matches) != 1
    native = next((row for row in choices['types'] if row['title'] == UNKNOWN_TITLE), None) if fallback else matches[0]
    if native is None:
        raise RemoteError('SUPPORT_UNKNOWN_TYPE_MISSING')
    return {**native, 'program': product, 'office': {'id':record[0],'title':record[1]}, 'fallback':fallback}


def reference(choices):
    value = snapshot()
    unknown = next((row for row in choices['types'] if row['title'] == UNKNOWN_TITLE), None)
    if unknown is None:
        raise RemoteError('SUPPORT_UNKNOWN_TYPE_MISSING')
    rows = [{**unknown,'program_ids':[],'requires_program':False,'native_matches':[]}]
    for ident,title in value['rows']:
        if title == 'Неопознанная ошибка':
            continue
        matches = candidates(title,choices)
        grouped = sorted({value for row in matches for value in program_ids(row['title'],choices['programs'])})
        rows.append({'id':'office:'+ident,'title':title,'program_ids':grouped,'requires_program':bool(grouped),
                     'aliases':[row['title'] for row in matches],
                     'native_matches':[{'id':row['id'],'program_ids':program_ids(row['title'],choices['programs'])} for row in matches]})
    return {'types':rows,'programs':choices['programs'],'unknown_id':unknown['id'],
            'source':'office_snapshot','source_url':value['source'],'captured_on':value['captured_on']}
