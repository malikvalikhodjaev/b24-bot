"""Escaped, grouped previews shared by the pharmacy and new-pharmacy sale."""
from datetime import datetime
import html
from .i18n import tr
from .intake_ui import phrase


def esc(value):
    return html.escape(str(value))


def pharmacy_blocks(state, manager):
    contact = state.get('contact') or {}
    company_name = state.get('company_name') or (state.get('company') or {}).get('title') or phrase('not_set')
    lines = [phrase('pharmacy_section'), '<b>' + esc(state['title']) + '</b>']
    for key, label in (('business_region', 'Бизнес-регион: '), ('city', 'Город/район: '), ('current_program', 'Текущая программа: ')):
        if state.get(key):
            lines.append(tr(label) + esc(state[key]['title']))
    lines.append(tr('Адрес: ') + esc(state.get('address') or phrase('not_set')))
    if state.get('phone_only'):
        lines.append(tr('Телефон аптеки: ')+esc(state.get('phone') or phrase('not_set')))
    if state.get('location'):
        lines.append('📍 ' + esc(state['location']['latitude']) + ', ' + esc(state['location']['longitude']))
    lines += [tr('Статус: ') + tr('Потенциальная'), tr('Стадия: ') + tr('Потенциальная аптека'),
              tr('Менеджер продаж: ') + esc(manager), '', phrase('company_section'),
              esc(company_name), tr('ИНН: ') + esc(state.get('inn') or phrase('not_set'))]
    if state.get('new_company'):
        lines.append(phrase('new_company'))
    if state.get('phone_only'):
        return '\n'.join(lines)
    lines += ['', phrase('contact_section')]
    name = state.get('contact_name') if state.get('create_contact') else contact.get('title')
    lines += [esc(name or phrase('not_set')), tr('Телефон: ') + esc(state.get('phone') or phrase('not_set'))]
    if state.get('contact_position'):
        lines.append(tr('Должность контакта: ') + esc(state['contact_position']['title']))
    if state.get('create_contact'):
        lines.append(phrase('new_contact'))
    return '\n'.join(lines)


def sale_blocks(state, manager):
    if state.get('new_pharmacy'):
        text = pharmacy_blocks(state['pharmacy_form'], manager)
    else:
        row = state['pharmacy']
        company = state.get('company') or {}
        text = '\n'.join([phrase('pharmacy_section'), '<b>' + esc(row['title']) + '</b>',
            'FOM ID: '+esc(row.get('fom_id') or row['id']),tr('Адрес: ') + esc(state['address']), '', phrase('company_section'),
            esc(company.get('title') or phrase('not_set')), '', phrase('contact_section'),
            tr('Телефон: ') + esc(state.get('phone') or phrase('not_set'))])
        if row.get('status'):
            text = text.replace('\nFOM ID:', '\n' + tr('Статус аптеки: ') + esc(row['status']['title']) + '\nFOM ID:', 1)
    text += '\n\n' + phrase('deal_section') + '\n<b>' + esc(state['title']) + '</b>'
    text += '\n' + tr('Воронка: ') + esc(state['category_name']) + '\n' + tr('Стадия: ') + esc(state['initial_stage_name'])
    text += '\n' + tr('Ответственный: ') + esc(manager)
    if state.get('discussion_programs'):
        text += '\n💬 ' + tr('Обсудить программы: ') + ', '.join(esc(row['title'].removeprefix('◉ ')) for row in state['discussion_programs'])
    if state.get('description'):
        text += '\n' + tr('Описание: ') + esc(state['description'])
    if state.get('reminder', bool(state.get('next_step'))):
        text += '\n\n' + phrase('reminder_section') + '\n' + esc(state['next_step'])
        text += '\n🗓 ' + datetime.fromisoformat(state['deadline']).strftime('%d.%m.%Y %H:%M') + ' · ' + tr('Ташкент')
    else:
        text += '\n\n' + phrase('reminder_missing')
    return text
