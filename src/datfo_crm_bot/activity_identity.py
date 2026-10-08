"""Technical reminder identity, separate from its visible description."""
import html
import re

SOURCE = 'datfo-sales-telegram'


def origin_id(request_id):
    return request_id + '-next-step'


def legacy_tag(request_id):
    return 'Запрос бота: ' + origin_id(request_id)


def has_legacy_tag(activity, request_id):
    description = html.unescape(re.sub(r'<[^>]*>', '', str(activity.get('DESCRIPTION') or ''))).rstrip()
    return description.endswith(legacy_tag(request_id))


def matches(activity, request_id):
    if not isinstance(activity, dict):
        return False
    source, ident = activity.get('ORIGINATOR_ID'), activity.get('ORIGIN_ID')
    if source or ident:
        return source == SOURCE and ident == origin_id(request_id)
    return has_legacy_tag(activity, request_id)


def cleaned_description(activity, request_id):
    description = str(activity.get('DESCRIPTION') or '')
    if not has_legacy_tag(activity, request_id):
        return description
    tag = legacy_tag(request_id)
    position = description.rfind(tag)
    if position < 0:
        raise ValueError('Legacy reminder marker has an unsupported encoding')
    # Keep user text and closing formatting tags; remove only the known suffix.
    return description[:position].rstrip('\r\n') + description[position + len(tag):]
