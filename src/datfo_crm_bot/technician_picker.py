"""Telegram list/search for technicians, independent of the pharmacy contact person."""
from collections import Counter
import html

from .api import RemoteError
from .communications import InboxError
from .i18n import tr
from .technicians import search


def directory(inbox):
    return inbox.sync.technicians if inbox.sync else None


def all_rows(inbox, user, *, force=False):
    employee = inbox.registration.user(user)
    return [row for row in directory(inbox).listing(force=force)
            if row['bitrix_id'] != employee.bitrix_id]


def open_picker(inbox, user, draft, *, force=False):
    draft.update(direct_recipient=True, technician_picker=True, recipient_page=0,
                 technician_query='', technician_region=None)
    draft.pop('technician_choices', None)
    draft.pop('technician', None)
    draft['technician_all'] = all_rows(inbox, user, force=force)
    draft['technician_choices'] = search(draft['technician_all'])


def prompt(inbox, user, draft):
    from .work_inbox import TO_POOL, CANCEL, ALL_TECHNICIANS, REFRESH_TECHNICIANS
    rows = draft.get('technician_choices', [])
    page = draft.get('recipient_page', 0)
    shown = list(enumerate(rows[page * 8:(page + 1) * 8], page * 8 + 1))
    text = tr('🛠 Выберите техника из структуры компании.') + '\n' + tr('Напишите имя или фамилию для поиска либо выберите номер из списка.')
    if draft.get('technician_query'):
        text += '\n\n🔎 ' + html.escape(draft['technician_query'])
    if draft.get('technician_region'):
        text += '\n🗺 R' + str(draft['technician_region'])
    duplicates = Counter(row['name'].casefold() for row in draft.get('technician_all', []))
    for number, row in shown:
        regions = ', '.join('R' + str(value) for value in row['regions'])
        text += '\n' + str(number) + '. ' + html.escape(row['name']) + ' · ' + regions
        if duplicates[row['name'].casefold()] > 1:
            text += ' · Б24 #' + str(row['bitrix_id'])
        if inbox.store.connected_technician(row['bitrix_id']):
            text += ' · Telegram ✅'
    if not rows:
        text += '\n\n' + tr('Техник не найден. Проверьте имя или фамилию либо посмотрите всех техников.')
    elif len(rows) > 8:
        text += '\n\n' + tr('Страница') + f' {page + 1}/{(len(rows) + 7) // 8}'
    keyboard = [[str(number) for number, _ in shown[start:start + 4]] for start in range(0, len(shown), 4)]
    navigation = []
    if page:
        navigation.append('/request_prev')
    if (page + 1) * 8 < len(rows):
        navigation.append('/request_next')
    if navigation:
        keyboard.append(navigation)
    keyboard += [[tr(ALL_TECHNICIANS)], [f'🗺 R{region}' for region in range(1, 5)],
                 [tr(REFRESH_TECHNICIANS)], [tr(TO_POOL)], [CANCEL]]
    return text, keyboard


def select(inbox, user, text, draft):
    """Return a selected fresh technician, or None after updating a search/filter."""
    from .work_inbox import ALL_TECHNICIANS, REFRESH_TECHNICIANS
    if text in {ALL_TECHNICIANS, tr(ALL_TECHNICIANS, 'uz'), REFRESH_TECHNICIANS,
                tr(REFRESH_TECHNICIANS, 'uz')}:
        open_picker(inbox, user, draft, force=text in {REFRESH_TECHNICIANS, tr(REFRESH_TECHNICIANS, 'uz')})
    elif text in {f'🗺 R{region}' for region in range(1, 5)}:
        draft.update(technician_region=int(text[-1]), technician_query='', recipient_page=0)
        draft['technician_choices'] = search(draft.get('technician_all', []), region=draft['technician_region'])
    elif text.isascii() and text.isdigit():
        selected = inbox.pick(text, draft.get('technician_choices', []))
        try:
            person = directory(inbox).employee(selected['bitrix_id'])
        except RemoteError as exc:
            message = 'Этот техник больше недоступен. Выберите другого техника или оставьте заявку в очереди.' if exc.code == 'FOM_TECHNICIAN_CHANGED' else 'Не удалось проверить техника в Б24. Обновите список и попробуйте ещё раз.'
            raise InboxError(tr(message)) from exc
        if person['bitrix_id'] == inbox.registration.user(user).bitrix_id:
            raise InboxError(tr('Выберите другого техника или оставьте заявку в очереди.'))
        return person
    elif text.startswith('/') or len(text.strip()) < 2 or len(text) > 100:
        raise InboxError(tr('Напишите имя или фамилию техника либо выберите номер из списка.'))
    else:
        draft.update(technician_query=text.strip(), recipient_page=0)
        draft['technician_choices'] = search(draft.get('technician_all', []), text,
                                             draft.get('technician_region'))
    return None


def unavailable():
    from .work_inbox import TO_POOL, REFRESH_TECHNICIANS, CANCEL
    return tr('Не удалось загрузить техников из Б24. Обновите список или оставьте заявку в очереди.'), [
        [tr(REFRESH_TECHNICIANS)], [tr(TO_POOL)], [CANCEL]]
