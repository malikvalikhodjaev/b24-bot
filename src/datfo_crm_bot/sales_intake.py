"""KG sales intake, reusing the existing OKB form and persisted write phases."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import re

from .api import RemoteError
from . import activity_identity
from .config import ConfigError, positive_id
from .i18n import tr
from .okb_crm import OkbCrm, OkbSettings
from .okb_service import OkbBot, PendingStep, addition_notices
from .service import CANCEL, CHECK, CONFIRM, SKIP_PHONE, esc, normalize_phone, utc_text
from .intake_ui import phrase as intake, ADD_REMINDER, NO_REMINDER
from .reminder_calendar import calendar_prompt, time_prompt, NEXT_MONTH, PREVIOUS_MONTH
from .intake_preview import sale_blocks
from .input_forms import TEXT_FORM, STEP_FORM, form_keyboard, text_template, parse_text, reminder
from .sales_options import discussion_choices, selected_programs, INACTIVE_STATUSES

USE_PHARMACY = 'Создать сделку на эту аптеку'
NEW_PHARMACY = 'Это новая аптека — добавить в базу'
SEARCH_AGAIN = 'Другая аптека'
USE_ADDRESS = 'Использовать этот адрес'
NEXT_DAY = 'Следующий рабочий день'
CONTINUE_DEAL = 'Продолжить создание сделки'
FOM_ID_FIELD = 'UF_CRM_8_1719398102513'
CONFIRM_INACTIVE = 'Да, создать сделку на эту аптеку'
PROGRAMS_DONE = '✅ Программы выбраны'


class PharmacyForm(OkbBot):
    def finish_form(self, user_id, state):
        # Collect only: no pharmacy or frozen operation before the final deal preview.
        state['collected'] = True
        return '', [], state


class SalesIntake:
    def __init__(self, bot):
        self.bot, self.store, self.crm = bot, bot.store, bot.crm
        settings = OkbSettings.load()
        config = replace(bot.config, targets={**bot.config.targets, 'pharmacy': settings.pharmacy})
        self.pharmacy_crm = OkbCrm(config, self.crm.api, settings)
        self.okb = OkbBot(config, self.store, self.pharmacy_crm, bot.telegram, clock=bot.clock)
        self.form = PharmacyForm(config, self.store, self.pharmacy_crm, bot.telegram, clock=bot.clock)
        self.store.db.execute('''CREATE TABLE IF NOT EXISTS sales_status_confirmations(
            request_id TEXT NOT NULL,user_id INTEGER NOT NULL,pharmacy_id INTEGER NOT NULL,
            status_id TEXT NOT NULL,PRIMARY KEY(request_id,user_id,pharmacy_id,status_id))''')
        self.store.db.commit()

    def pharmacy_status(self, row):
        value = row.get(self.pharmacy_crm.settings.pharmacy.fields['status'])
        if value in (None, '', 0):
            return None
        return next((item for item in self.pharmacy_crm.choices('status') if item['id'] == str(value)),
                    {'id': str(value), 'title': tr('Неизвестный статус')})

    def status_confirmed(self, user, state):
        pharmacy = state['pharmacy']
        status = pharmacy.get('status') or {}
        return status.get('id') not in INACTIVE_STATUSES or bool(self.store.db.execute(
            '''SELECT 1 FROM sales_status_confirmations WHERE request_id=? AND user_id=?
               AND pharmacy_id=? AND status_id=?''',
            (state['request_id'], user, pharmacy['id'], status['id'])).fetchone())

    def status_prompt(self, state):
        state['step'] = 'sales_status_confirm'
        pharmacy = state['pharmacy']
        text = (tr('⚠️ В базе у аптеки «') + esc(pharmacy['title']) + tr('» указан статус «')
                + esc(pharmacy['status']['title']) + tr('».\nВы уверены, что хотите создать сделку на эту аптеку?'))
        return text, [[CONFIRM_INACTIVE], [SEARCH_AGAIN, CANCEL]], state

    def programs_prompt(self, state):
        choices = discussion_choices(self.crm.api)
        state['step'] = 'sales_discussion'
        state['discussion_choices'] = choices
        chosen = {row['id'] for row in state.get('discussion_programs', [])}
        rows = [[('☑️ ' if row['id'] in chosen else '⬜️ ') + row['title']] for row in choices]
        return intake('programs_ask'), rows + [[PROGRAMS_DONE], [CANCEL]], state

    def begin(self, state):
        state['step'] = 'sales_bulk_input'
        state['miniapp_only'] = True
        return intake('form_only'), form_keyboard(state, self.store, state.get('telegram_user')), state

    def lookup_fom(self, value):
        if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}',value.strip()):
            raise ValueError(tr('Введите FOM ID аптеки без пробелов.'))
        value=value.strip()
        target=self.pharmacy_crm.settings.pharmacy
        rows=self.crm.api.list_all('crm.item.list', {'entityTypeId':target.entity_type_id,'useOriginalUfNames':'Y',
            'filter':{'categoryId':target.category_id,FOM_ID_FIELD:value},'select':['*'],'order':{'id':'ASC'}},key='items')
        rows=[row for row in rows if str(row.get(FOM_ID_FIELD) or '').strip()==value and str(row.get('categoryId'))==str(target.category_id)]
        if not rows:
            raise ValueError(intake('fom_not_found'))
        if len(rows)!=1:
            raise ValueError(intake('fom_duplicate'))
        row=self.pharmacy(positive_id(rows[0].get('id'),'Аптека'))
        if str(row.get(FOM_ID_FIELD) or '').strip()!=value:
            raise ValueError(intake('fom_not_found'))
        fields=target.fields
        return {'id':int(row['id']),'title':str(row.get('title') or value),'fom_id':value,
            'company_id':int(row.get('companyId') or 0),'address':str(row.get(fields['address']) or ''),
            'phone':str(row.get(fields['phone']) or ''), 'status': self.pharmacy_status(row)}

    def pharmacies(self, name):
        target = self.pharmacy_crm.settings.pharmacy
        keys = target.fields
        response = self.crm.api.call('crm.item.list', {'entityTypeId': target.entity_type_id,
            'useOriginalUfNames': 'Y', 'filter': {'categoryId': target.category_id, '%title': name},
            'select': ['*'], 'order': {'id': 'ASC'}})
        result = response['result']
        rows = result.get('items') if isinstance(result, dict) else ([] if result == [] else None)
        if not isinstance(rows, list):
            raise RemoteError('INVALID_PHARMACY_LIST')
        if response.get('next') is not None or len(rows) > 10:
            return None
        return [{'id': positive_id(row.get('id',row.get('ID')), 'Аптека'),
                 'title': str(row.get('title',row.get('TITLE')) or ''),
                 'company_id': int(row.get('companyId',row.get('COMPANY_ID')) or 0), 'address': str(row.get(keys['address']) or '')}
                for row in rows]

    def company(self, ident):
        row = self.crm.api.call('crm.company.get', {'id': ident})['result']
        if not isinstance(row, dict) or str(row.get('ID')) != str(ident):
            raise RemoteError('INVALID_COMPANY_RESPONSE')
        return {'id': ident, 'title': str(row.get('TITLE') or ident)}

    def pharmacy(self, ident):
        target = self.pharmacy_crm.settings.pharmacy
        row = self.crm.api.call('crm.item.get', {'entityTypeId': target.entity_type_id,
            'id': ident, 'useOriginalUfNames': 'Y'})['result'].get('item')
        if not isinstance(row, dict) or str(row.get('id')) != str(ident) or str(row.get('categoryId')) != str(target.category_id):
            raise ConfigError(tr('Выбранная аптека больше не доступна в базе. Выберите её заново.'))
        return row

    def route(self, user_id, text, state):
        step = state['step']
        if step == 'sales_status_confirm':
            if text == SEARCH_AGAIN:
                for key in ('pharmacy', 'company', 'new_pharmacy'):
                    state.pop(key, None)
                return self.begin(state)
            if text != CONFIRM_INACTIVE:
                raise ValueError(tr('Подтвердите создание сделки или выберите другую аптеку.'))
            pharmacy = state['pharmacy']
            with self.store.db:
                self.store.db.execute('INSERT OR IGNORE INTO sales_status_confirmations VALUES(?,?,?,?)',
                    (state['request_id'], user_id, pharmacy['id'], pharmacy['status']['id']))
            return self.finish_form(user_id, state)
        if step == 'sales_discussion':
            if text == PROGRAMS_DONE:
                if not state.get('discussion_programs'):
                    raise ValueError(tr('Выберите хотя бы одну программу для обсуждения.'))
                return self.finish_form(user_id, state)
            item = next((row for row in state['discussion_choices'] if text in {row['title'], '☑️ '+row['title'], '⬜️ '+row['title']}), None)
            if not item:
                raise ValueError(tr('Выберите программу кнопкой. Можно отметить несколько.'))
            chosen = state.setdefault('discussion_programs', [])
            if any(row['id'] == item['id'] for row in chosen):
                chosen[:] = [row for row in chosen if row['id'] != item['id']]
            else:
                chosen.append(item)
            return self.programs_prompt(state)
        if step in {'sales_pharmacy', 'sales_bulk_input'}:
            if step=='sales_bulk_input' and state.get('miniapp_only'):
                return self.begin(state)
            if text == TEXT_FORM:
                state['step'] = 'sales_bulk_input'
                return text_template(), form_keyboard(state, self.store, user_id), state
            if text == STEP_FORM:
                return self.begin(state)
            if step == 'sales_bulk_input' or '\n' in text:
                return self.route_payload(user_id, parse_text(text), state)
        if step == 'done':
            return self.success(state, self.store.operation(state['request_id'])['result'])
        if text == SEARCH_AGAIN and step in {'sales_pharmacy', 'sales_match', 'sales_pharmacy_confirm', 'sales_address'}:
            for key in ('pharmacy', 'company', 'pharmacy_form', 'new_pharmacy', 'address'):
                state.pop(key, None)
            return self.begin(state)
        if step == 'sales_pharmacy':
            self.bot.require_text(text, 120, tr('Название аптеки'))
            if len(text) < 2:
                raise ValueError(tr('Введите хотя бы два символа названия аптеки.'))
            rows = self.pharmacies(text)
            state['pharmacy_name'] = text
            if rows is None:
                return tr('Найдено больше 10 аптек. Уточните название, чтобы выбрать нужную точку.'), [[CANCEL]], state
            state.update(pharmacy_candidates=rows, step='sales_match')
            if not rows:
                return tr('Аптека с таким названием не найдена. Это новая аптека — добавить её в базу и создать на неё сделку?'), [[NEW_PHARMACY], [SEARCH_AGAIN, CANCEL]], state
            if len(rows) == 1:
                state.update(pharmacy=rows[0], step='sales_pharmacy_confirm')
                return self.match_preview(state)
            lines = [tr('Найдены аптеки. Выберите номер нужной точки:')]
            lines += [f"{index}. {esc(row['title'])} · {esc(row['address'] or tr('адрес не указан'))} · #{row['id']}"
                      for index, row in enumerate(rows, 1)]
            return '\n'.join(lines), [[str(n)] for n in range(1, len(rows)+1)] + [[NEW_PHARMACY], [SEARCH_AGAIN, CANCEL]], state
        if step in {'sales_match', 'sales_pharmacy_confirm'}:
            if text == NEW_PHARMACY:
                state.pop('pharmacy', None)
                child = {'kind': 'pharmacy', 'workflow': 'okb', 'sales_parent': state['request_id'],
                         'request_id': state['request_id']+'-pharmacy', 'step': 'okb_inn'}
                self.pharmacy_crm.validate_okb()
                state.update(new_pharmacy=True, pharmacy_form=child, step='sales_okb')
                return tr('Добавим новую аптеку после общего подтверждения. Введите ИНН её юридической компании.'), [[CANCEL]], state
            if step == 'sales_match':
                state.update(pharmacy=self.bot.pick(text, state['pharmacy_candidates']), step='sales_pharmacy_confirm')
                return self.match_preview(state)
            if text != USE_PHARMACY:
                from .navigation import phrase
                raise ValueError(phrase('choice'))
            pharmacy = state['pharmacy']
            state.update(new_pharmacy=False, company=self.company(pharmacy['company_id']) if pharmacy['company_id'] else None,
                         inn='', title='План: '+pharmacy['title'], step='sales_address')
            keyboard = [[USE_ADDRESS], [CANCEL]] if pharmacy['address'] else [[CANCEL]]
            return tr('Укажите физический адрес этой аптеки: город, улица и дом.') + ('\n'+esc(pharmacy['address']) if pharmacy['address'] else ''), keyboard, state
        if step == 'sales_okb':
            child = state['pharmacy_form']
            response, keyboard, child = self.form.route(user_id, text, child)
            if child.get('step') == 'okb_title':
                response, keyboard, child = self.form.route(user_id, state['pharmacy_name'], child)
            state['pharmacy_form'] = child
            if not child.get('collected'):
                return response, keyboard, state
            state.update(title='План: '+child['title'], company=child.get('company'), inn=child['inn'],
                         address=child['address'], phone=child['phone'])
            if state.get('bulk_reminder') is not None:
                state.update(state.pop('bulk_reminder'))
                return self.finish_form(user_id, state)
            return self.description_prompt(state)
        if step == 'sales_address':
            address = state['pharmacy']['address'] if text == USE_ADDRESS else text
            self.bot.require_text(address, 500, tr('Адрес'))
            state['address'] = address
            return self.description_prompt(state)
        if step == 'sales_description':
            self.bot.require_text(text, 1500, tr('Предмет сделки'))
            state['description'] = text
            return self.description_prompt(state)
        if step == 'sales_reminder':
            if text == NO_REMINDER:
                state.update(reminder=False, next_step='', deadline='')
                return self.after_reminder(user_id, state)
            if text != ADD_REMINDER:
                from .navigation import phrase
                raise ValueError(phrase('choice'))
            state.update(reminder=True, step='sales_next_step')
            return intake('reminder_action'), [[CANCEL]], state
        if step == 'sales_next_step':
            self.bot.require_text(text, 1000, tr('Следующий шаг'))
            state.update(next_step=text, step='sales_deadline')
            return calendar_prompt(state, self.bot.clock())
        if step == 'sales_deadline':
            now = self.bot.clock()
            if text in {NEXT_MONTH, PREVIOUS_MONTH}:
                return calendar_prompt(state, now, shift=1 if text == NEXT_MONTH else -1)
            if text in state.get('calendar_options', {}):
                state['reminder_date'] = state['calendar_options'][text]
                return time_prompt(state, now)
            if text == NEXT_DAY:
                due = (now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
                while due.weekday() > 4:
                    due += timedelta(days=1)
            else:
                try:
                    due = datetime.strptime(text, '%d.%m.%Y %H:%M' if ' ' in text else '%d.%m.%Y')
                except ValueError:
                    raise ValueError(tr('Укажите дату ДД.ММ.ГГГГ, например 09.10.2026; время можно добавить после пробела.')) from None
                due = due.replace(tzinfo=self.bot.config.timezone)
                if ' ' not in text:
                    due = due.replace(hour=18)
            if due <= now:
                raise ValueError(tr('Срок дела должен быть в будущем.'))
            state['deadline'] = due.isoformat()
            state['reminder'] = True
            return self.after_reminder(user_id, state)
        if step == 'sales_time':
            if text not in state['reminder_times']:
                raise ValueError(intake('reminder_time'))
            due = datetime.fromisoformat(state['reminder_date']+'T'+text).replace(tzinfo=self.bot.config.timezone)
            if due <= self.bot.clock():
                raise ValueError(tr('Срок дела должен быть в будущем.'))
            state['deadline'] = due.isoformat()
            return self.after_reminder(user_id, state)
        if step == 'sales_phone':
            state['phone'] = '' if text == SKIP_PHONE else normalize_phone(text)
            return self.finish_form(user_id, state)
        if step == 'confirm':
            if text in {CONFIRM, CONTINUE_DEAL, CHECK, '/pending'}:
                return self.submit(user_id, state, verify_only=text in {CHECK, '/pending'})
            from .navigation import phrase
            raise ValueError(phrase('choice'))
        raise ConfigError(tr('Шаг формы изменился. Отмените черновик и начните заново.'))

    @staticmethod
    def match_preview(state):
        row = state['pharmacy']
        return (tr('Это нужная аптека?') + '\n<b>' + esc(row['title']) + '</b>\n'
                + esc(row['address'] or tr('адрес не указан')) + f" · Б24 #{row['id']}",
                [[USE_PHARMACY], [SEARCH_AGAIN, NEW_PHARMACY], [CANCEL]], state)

    @staticmethod
    def description_prompt(state):
        state.setdefault('description', '')
        state['step'] = 'sales_reminder'
        return intake('reminder_ask'), [[ADD_REMINDER, NO_REMINDER], [CANCEL]], state

    def after_reminder(self, user_id, state):
        if state.get('new_pharmacy'):
            return self.finish_form(user_id, state)
        state['step'] = 'sales_phone'
        return tr('Телефон для связи или «Без телефона». Можно без +998.'), [[SKIP_PHONE], [CANCEL]], state

    def route_payload(self, user_id, data, state):
        if data.get('mode')=='deal' and data.get('pharmacy_kind') not in {'new','existing'}:
            raise ValueError(tr('Выберите новую или существующую аптеку.'))
        if state['step'] not in {'sales_pharmacy', 'sales_bulk_input'}:
            raise ValueError(tr('На этом шаге форма недоступна.'))
        planned = reminder(data, self.bot.config.timezone, self.bot.clock())
        if 'discussion_programs' in data:
            state['discussion_programs'] = selected_programs(data['discussion_programs'], discussion_choices(self.crm.api))
        if data.get('pharmacy_kind') == 'existing':
            pharmacy=self.lookup_fom(data.get('fom_id'))
            state.update(new_pharmacy=False,pharmacy=pharmacy,
                company=self.company(pharmacy['company_id']) if pharmacy['company_id'] else None,
                inn='',title='План: '+pharmacy['title'],address=pharmacy['address'],phone=pharmacy['phone'],**planned)
            return self.finish_form(user_id,state)
        child = {'kind': 'pharmacy', 'workflow': 'okb', 'sales_parent': state['request_id'],
                 'request_id': state['request_id']+'-pharmacy', 'step': 'okb_inn'}
        response, keyboard, child = self.form.route_payload(user_id, data, child)
        state.update(new_pharmacy=True, pharmacy_form=child, step='sales_okb', bulk_reminder=planned)
        if child.get('collected'):
            state.update(title='План: '+child['title'], company=child.get('company'), inn=child['inn'], address=child['address'], phone=child['phone'], **planned)
            state.pop('bulk_reminder')
            return self.finish_form(user_id, state)
        return response, keyboard, state

    def finish_form(self, user_id, state):
        if not state.get('discussion_programs'):
            return self.programs_prompt(state)
        if not state.get('new_pharmacy') and not self.status_confirmed(user_id, state):
            return self.status_prompt(state)
        if state.get('live_test'):
            state['title'] = '[ТЕСТ БОТА] ' + state['title']
        state['step'] = 'confirm'
        self.store.prepare(user_id, state, utc_text(self.bot.clock()))
        return self.preview(user_id, state)

    def preview(self, user_id, state):
        text = '<b>'+intake('preview')+'</b>\n\n'+sale_blocks(state, self.bot.config.users[user_id].name)
        if state.get('new_pharmacy'):
            text += '\n'+tr('После подтверждения добавим аптеку в ОКБ и свяжем с ней сделку.')
        else:
            text += '\n'+tr('Используем существующую аптеку; её справочные поля не меняем.')
        text += '\n\n'+intake('confirm_hint')
        return text, [[CONFIRM, CANCEL]], state

    def pending(self, user_id, text, state):
        if text in {CONTINUE_DEAL, CONFIRM, '/pending', CHECK}:
            return self.submit(user_id, state, verify_only=text in {'/pending', CHECK})
        return tr('Создание сделки ещё не завершено. Проверим сохранённые шаги, чтобы не создавать дубли.'), [[CONTINUE_DEAL, CHECK]], state

    def find_activity(self, state, deal):
        rows = self.crm.api.list_all('crm.activity.list', {'filter': {'OWNER_TYPE_ID': 2, 'OWNER_ID': deal['id'], 'PROVIDER_ID': 'CRM_TODO'},
            'select': ['ID', 'DESCRIPTION', 'ORIGINATOR_ID', 'ORIGIN_ID'], 'order': {'ID': 'DESC'}})
        matches = [row for row in rows if activity_identity.matches(row, state['request_id'])]
        if len(matches) > 1:
            raise RemoteError('DUPLICATE_NEXT_STEP')
        return {'id': int(matches[0]['ID'])} if matches else None

    def add_activity(self, state, deal, manager):
        result = self.crm.api.call('crm.activity.todo.add', {'ownerTypeId': 2, 'ownerId': deal['id'],
            'title': state['next_step'][:255], 'description': state['next_step']+'\n'+activity_identity.legacy_tag(state['request_id']),
            'responsibleId': manager, 'deadline': state['deadline'], 'pingOffsets': [15]})['result']
        value = result.get('id') if isinstance(result, dict) else result
        return {'id': self.pharmacy_crm.write_id(value)}

    def activity_metadata(self, state, deal, activity, *, verify_only=False):
        # Persist the todo ID first. If creation/cleanup loses its response, the
        # old marker or the native origin fields recover the same activity.
        def read():
            current = self.crm.api.call('crm.activity.get', {'id': activity['id']})['result']
            if (not isinstance(current, dict) or str(current.get('ID')) != str(activity['id'])
                    or str(current.get('OWNER_TYPE_ID')) != '2' or str(current.get('OWNER_ID')) != str(deal['id'])
                    or current.get('PROVIDER_ID') != 'CRM_TODO'
                    or not activity_identity.matches(current, state['request_id'])):
                raise ConfigError(tr('Связь напоминания со сделкой изменилась. Проверьте карточку Б24.'))
            return current

        def ready():
            current = read()
            if (current.get('ORIGINATOR_ID') == activity_identity.SOURCE
                    and current.get('ORIGIN_ID') == activity_identity.origin_id(state['request_id'])
                    and not activity_identity.has_legacy_tag(current, state['request_id'])):
                return {'id': activity['id']}
            return None

        def clean():
            current = read()
            try:
                description = activity_identity.cleaned_description(current, state['request_id'])
            except ValueError:
                raise ConfigError(tr('Не удалось убрать служебную строку напоминания. Проверьте карточку Б24.')) from None
            fields = {'ORIGINATOR_ID': activity_identity.SOURCE,
                      'ORIGIN_ID': activity_identity.origin_id(state['request_id']), 'DESCRIPTION': description}
            response = self.crm.api.call('crm.activity.update', {'id': activity['id'], 'fields': fields})
            if response.get('result') is not True or ready() is None:
                raise RemoteError('ACTIVITY_METADATA_NOT_CONFIRMED', uncertain=True)
            return {'id': activity['id']}

        return self.okb.phase(state, 'sales_next_step_metadata', ready, clean, verify_only=verify_only)

    def create_deal(self, state, manager, user_id):
        submitted = False
        def mark_submitted():
            nonlocal submitted
            submitted = True
        try:
            return self.crm.create(state, manager, user_id, on_submit=mark_submitted)
        except RemoteError as exc:
            raise RemoteError(exc.code, uncertain=submitted and exc.uncertain) from None

    def submit(self, user_id, state, *, verify_only=False):
        operation = self.store.operation(state['request_id'])
        if not operation or operation['user_id'] != user_id:
            raise ConfigError(tr('Не найден подтверждённый черновик пользователя'))
        state = operation['value']
        if operation['status'] == 'succeeded':
            return self.success(state, operation['result'])
        self.bot._configure(state)
        manager = self.bot.config.users[user_id].bitrix_id
        deal = None
        try:
            self.crm.validate('deal')
            if state.get('discussion_programs'):
                selected_programs([row['id'] for row in state['discussion_programs']], discussion_choices(self.crm.api))
            category, stage = self.crm.sales_destination()
            if (category['id'], stage['id']) != (state['category_id'], state['initial_stage']):
                raise ConfigError(tr('Воронка или стадия продаж изменилась. Проверьте форму заново.'))
            if state.get('new_pharmacy'):
                child = state['pharmacy_form']
                self.store.prepare(user_id, child, utc_text(self.bot.clock()))
                self.store.status(state['request_id'], 'created')
                response, _, _ = self.okb.submit(user_id, child, verify_only=verify_only)
                child_op = self.store.operation(child['request_id'])
                if child_op['status'] not in {'succeeded', 'existing'}:
                    if child_op['status'] in {'prepared', 'rejected'} and not self.store.operation_wrote(child['request_id']):
                        self.store.status(state['request_id'], 'rejected')
                    if getattr(self.okb, 'last_error_code', None) == 'ACCESS_DENIED':
                        if getattr(self.okb, 'last_error_step', None) == 'pharmacy':
                            response = intake('pharmacy_access')
                    else:
                        response = intake('pharmacy_error')
                    return response, [[CONTINUE_DEAL, CHECK]], state
                pharmacy = child_op['result']
                company = self.company(pharmacy['company_id'])
            else:
                pharmacy = state['pharmacy']
                current = self.pharmacy(pharmacy['id'])
                status = self.pharmacy_status(current)
                checked = {**state, 'pharmacy': {**pharmacy, 'status': status}}
                # Already submitted native writes are recovered by origin, never retried.
                prior = self.store.operation_step(state['request_id'], 'sales_deal')
                if (not prior or prior['status'] == 'rejected') and not self.status_confirmed(user_id, checked):
                    return self.status_prompt(checked)
                if pharmacy.get('fom_id') and str(current.get(FOM_ID_FIELD) or '').strip()!=pharmacy['fom_id']:
                    raise ConfigError(intake('fom_not_found'))
                if int(current.get('companyId') or 0) != pharmacy['company_id']:
                    raise ConfigError(tr('Компания выбранной аптеки изменилась. Проверьте привязку перед сохранением.'))
                company = state.get('company')
            actual = {**state, 'company': company, 'pharmacy_id': pharmacy['id']}
            deal = self.okb.phase(state, 'sales_deal', lambda: self.crm.find_request('deal', state['request_id']),
                lambda: self.create_deal(actual, manager, user_id), verify_only=verify_only)
            self.store.status(state['request_id'], 'created', deal)
            current_deal = self.crm.item(deal['id'])
            if (str(current_deal.get('parentId1034')) != str(pharmacy['id'])
                    or current_deal.get('originId') != state['request_id']
                    or current_deal.get('originatorId') != self.bot.config.targets['deal'].defaults['originatorId']):
                raise ConfigError(tr('Связь созданной сделки с аптекой не подтверждена. Проверьте карточку Б24 перед следующим шагом.'))
            manager = positive_id(current_deal.get('assignedById'), 'Ответственный сделки')
            activity = None
            if state.get('reminder', bool(state.get('next_step'))):
                activity = self.okb.phase(state, 'sales_next_step', lambda: self.find_activity(state, deal),
                    lambda: self.add_activity(state, deal, manager), verify_only=verify_only)
                self.activity_metadata(state, deal, activity, verify_only=verify_only)
            result = {**deal, 'activity_id': activity['id'] if activity else None, 'pharmacy_id': pharmacy['id']}
            self.store.status(state['request_id'], 'succeeded', result, utc_text(self.bot.clock()))
            return self.success(state, result)
        except PendingStep as exc:
            response = tr('Ответ Б24 пока не подтверждён. Повторную карточку или дело не создаём; нажмите «Проверить сохранение».') if exc.uncertain else tr('Сохранённые шаги проверены. Нажмите «Продолжить создание сделки», чтобы завершить оставшиеся.')
        except RemoteError as exc:
            response = tr('У подключения бота нет нужного права в Б24. Черновик и уже сохранённые шаги сохранены; после исправления прав продолжим без дублей.') if exc.code == 'ACCESS_DENIED' else tr('Б24 сейчас не подтвердил сохранение. Проверим сохранённые шаги; повторные карточки не создаём.')
        except ConfigError as exc:
            response = esc(str(exc))
        # A created deal remains visible even if the following activity was denied.
        if not deal:
            saved = self.store.operation_step(state['request_id'], 'sales_deal')
            deal = saved['result'] if saved and saved['result'] else None
        if deal:
            self.store.status(state['request_id'], 'created', deal)
            response += '\n'+tr('Сделка создана, но дело менеджеру ещё не подтверждено.')+'\n<a href="'+esc(deal['url'])+'">'+tr('Открыть сделку')+'</a>'
        return response, [[CONTINUE_DEAL, CHECK]], state

    def success(self, state, result):
        from .intake_success import saved_deal
        from .live_deals import MENU
        child_result = {}
        if state.get('new_pharmacy'):
            child = self.store.operation(state['pharmacy_form']['request_id'])
            if child and child['status'] in {'succeeded', 'existing'}:
                child_result = child['result']
        state['step'] = 'done'
        text = saved_deal(state, result,
            pharmacy_added=bool(child_result and not child_result.get('existing')),
            contact_added=bool(child_result.get('contact_created')),
            bulk=addition_notices(child_result) if child_result else '', zone=self.bot.config.timezone)
        return text, MENU, state
