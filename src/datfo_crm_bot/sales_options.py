"""Verified native sales field and small, canonical reminder action choices."""
from .api import RemoteError
from .i18n import tr

DISCUSSION_FIELD = 'UF_CRM_1778163500995'
INACTIVE_STATUSES = {'4794', '4795'}
REMINDER_ACTIONS = {
    'call': ('Позвонить', 'Қўнғироқ қилиш'),
    'visit': ('Приехать', 'Бориб кўриш'),
    'check_in': ('Уточнить, как идут дела', 'Ишлар қандай кетаётганини сўраш'),
    'other': ('Другое', 'Бошқа'),
}


def discussion_choices(api):
    fields = api.call('crm.item.fields', {'entityTypeId': 2, 'useOriginalUfNames': 'Y'})['result'].get('fields', {})
    field = fields.get(DISCUSSION_FIELD, {})
    if (field.get('type') != 'enumeration' or field.get('isMultiple') is not True
            or field.get('isReadOnly') or not isinstance(field.get('items'), list)):
        raise RemoteError('DISCUSSION_FIELD_CHANGED')
    rows = [{'id': str(row['ID']), 'title': str(row['VALUE'])} for row in field['items']]
    if not rows or len({row['id'] for row in rows}) != len(rows):
        raise RemoteError('INVALID_DISCUSSION_CHOICES')
    return rows


def selected_programs(values, choices):
    if (not isinstance(values, list) or not 1 <= len(values) <= 50
            or any(isinstance(value, bool) or not isinstance(value, (str, int)) for value in values)):
        raise ValueError(tr('Выберите хотя бы одну программу для обсуждения.'))
    ids = list(dict.fromkeys(str(value) for value in values))
    lookup = {row['id']: row for row in choices}
    if any(value not in lookup for value in ids):
        raise ValueError(tr('Список программ изменился. Откройте форму заново и выберите программы.'))
    return [lookup[value] for value in ids]
