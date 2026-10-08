"""Native support enums: public picker labels and fresh, server-side ID validation."""
import html
import re

from .api import RemoteError
from .i18n import tr

TYPE_FIELD = 'UF_CRM_67FC897AA4805'
PROGRAM_FIELD = 'UF_CRM_67FC897B7657A'
DESCRIPTION_FIELD = 'UF_CRM_67FC897A6424E'
UNKNOWN_TITLE = 'Неопознанная ошибка: нужно уточнить'


def _choices(fields, name):
    field = fields.get(name, {})
    if (field.get('type') != 'enumeration' or field.get('isMultiple') is not False
            or field.get('isReadOnly') or not isinstance(field.get('items'), list)):
        raise RemoteError('SUPPORT_FIELD_CHANGED')
    rows = []
    for item in field['items']:
        ident, title = str(item.get('ID', '')), item.get('VALUE')
        if not re.fullmatch(r'[1-9][0-9]*', ident) or not isinstance(title, str) or not title.strip():
            raise RemoteError('INVALID_SUPPORT_CHOICES')
        rows.append({'id': ident, 'title': title.strip()})
    if not rows or len({row['id'] for row in rows}) != len(rows):
        raise RemoteError('INVALID_SUPPORT_CHOICES')
    return rows


def catalogue(api):
    fields = api.call('crm.item.fields', {'entityTypeId': 2, 'useOriginalUfNames': 'Y'})['result'].get('fields', {})
    types, programs = _choices(fields, TYPE_FIELD), _choices(fields, PROGRAM_FIELD)
    description = fields.get(DESCRIPTION_FIELD, {})
    if description.get('type') != 'string' or description.get('isReadOnly') or description.get('isMultiple'):
        raise RemoteError('SUPPORT_FIELD_CHANGED')
    # The generic unknown option is first; it is never selected automatically.
    types.sort(key=lambda row: row['title'] != UNKNOWN_TITLE)
    return {'types': types, 'programs': programs}


def selected(data, choices):
    ident, program = data.get('support_type'), data.get('support_program', '')
    if not isinstance(ident, str) or not ident:
        raise ValueError(tr('Выберите тип обращения из списка. Если причина неизвестна, выберите «Неопознанная ошибка: нужно уточнить».'))
    if not isinstance(program, str):
        raise ValueError(tr('Выберите программу из списка или оставьте «Все программы».'))
    if ident.startswith('office:'):
        from .support_office import resolve
        return resolve(ident, program, choices)
    row = next((row for row in choices['types'] if row['id'] == ident), None)
    product = next((row for row in choices['programs'] if row['id'] == program), None)
    if row is None or (program and product is None):
        raise ValueError(tr('Список типов обращения изменился. Откройте форму заново и выберите тип.'))
    return {**row, 'program': product}


def revalidate(details, api):
    if details is None:
        return  # Historical requests and already open legacy drafts keep their original workflow.
    current = selected({'support_type': 'office:'+details['office']['id'] if details.get('office') else details['id'],
                        'support_program': (details.get('program') or {}).get('id', '')}, catalogue(api))
    if current != details:
        raise ValueError(tr('Список типов обращения изменился. Откройте форму заново и выберите тип.'))


def native_fields(details, description):
    if not details:
        return {}
    if details.get('fallback'):
        description = 'Тип в новой платформе: '+details['office']['title']+'\n\n'+description
    fields = {TYPE_FIELD: int(details['id']), DESCRIPTION_FIELD: description}
    if details.get('program'):
        fields[PROGRAM_FIELD] = int(details['program']['id'])
    return fields


def matches(remote, details):
    return (not details or (str(remote.get(TYPE_FIELD)) == details['id']
            and (not details.get('program') or str(remote.get(PROGRAM_FIELD)) == details['program']['id'])))


def preview(details):
    if not details:
        return ''
    title = (details.get('office') or {}).get('title', details['title'])
    lines = ['🏷 ' + tr('Тип обращения: ') + html.escape(title)]
    if details.get('program'):
        lines.insert(0, '💻 ' + tr('Программа: ') + html.escape(details['program']['title']))
    if details.get('fallback'):
        lines.append('ℹ️ '+tr('Точного типа в Б24 нет: сохраним выбранный тип в описании, а в поле Б24 отметим «Неопознанная ошибка: нужно уточнить».'))
    return '\n'.join(lines)


def program_ids(title, programs):
    patterns = {
        'F-Apteka': r'\[FA\]|\bFA\b|F[- ]APTEKA',
        'F-Kassa': r'\[FK\]|\bFK\b|F[- ]KASSA',
        'F-Summary': r'\[FS\]|\bFS\b|F[- ]SUMMARY',
        'F-Global': r'\[FGlobal\]|F[- ]GLOBAL',
        'F-Inventory': r'\[FI\]|F[- ]INVENTORY',
        'ArzonApteka': r'\[AA\]|ARZONAPTEKA',
    }
    groups = [name for name, pattern in patterns.items() if re.search(pattern, title, re.I)]
    return [product['id'] for product in programs if any(product['title'].casefold().startswith(name.casefold()) for name in groups)]


def picker_reference(api):
    from .support_office import reference
    return reference(catalogue(api))
