from __future__ import annotations

from .i18n import tr

from uuid import uuid4

from .api import RemoteError
from .config import ConfigError
from .service import Bot, CANCEL, CHECK, CONFIRM, MENU, PHARMACY, STATS, esc, normalize_inn, normalize_phone, utc_text
from .navigation import phrase
from .intake_ui import phrase as intake, ADD_LOCATION, SKIP_LOCATION, location_keyboard, coordinates
from .intake_preview import pharmacy_blocks
from .input_forms import TEXT_FORM, STEP_FORM, form_keyboard, text_template, parse_text, collect, pick as pick_field

OKB = "🏪 Добавить аптеку в ОКБ"
OKB_LABELS = {OKB, "🏪 Добавить в ОКБ"}
CONTINUE = "Продолжить добавление в ОКБ"
NO_LANDMARK = "Без ориентира"
CREATE_CONTACT = '➕ Создать новый контакт'
NO_CONTACT = 'Без контакта'
USE_CONTACT_PHONE = 'Использовать этот номер'
BULK_UPLOAD_URL = 'https://fom-analytics.uz/admin?workspace=sales#salesImportsPanel'


def addition_notices(result: dict):
    if result.get('existing') and not result.get('contact_created'):
        return ''
    return ('<blockquote>' + intake('bulk_short')
            + '<a href="'+esc(BULK_UPLOAD_URL)+'">'+intake('bulk_contacts')+'</a>'
            + intake('and') + '<a href="'+esc(BULK_UPLOAD_URL)+'">'+intake('bulk_pharmacies')+'</a>.</blockquote>')


class PendingStep(Exception):
    def __init__(self, step: str, *, uncertain: bool):
        self.step, self.uncertain = step, uncertain


class OkbBot(Bot):
    @staticmethod
    def success(state: dict, result: dict):
        if state.get("workflow") != "okb":
            return Bot.success(state, result)
        state["step"] = "done"
        action = tr("Использована существующая аптека") if result.get("existing") else tr("Аптека добавлена в ОКБ Б24")
        text = ''.join(['✅ ', format(action, ''), ': ', format(esc(state['title']), ''), tr('.\nКомпания: '), format(esc(state['company_name']), ''), '.\n<a href="', format(esc(result['url']), ''), tr('">Открыть аптеку</a>\nПриём в УТ подтверждается отдельно.')])
        if not result.get('existing'):
            text = intake('thanks_pharmacy') + '\n\n' + text
        if result.get('contact_created'):
            text += '\n' + intake('contact_saved_short')
        notices = addition_notices(result)
        if notices:
            text += '\n\n'+notices
        return text, MENU, state
    def route(self, user_id: int, text: str, state: dict | None):
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else text
        pending = self.store.unfinished(user_id)
        if pending:
            operation = self.store.operation(pending[0]["request_id"])
            if operation["value"].get("workflow") == "okb":
                if command in {"/pending", CHECK, CONTINUE}:
                    return self.submit(user_id, operation["value"], verify_only=command != CONTINUE)
                if command not in {"/stats", STATS}:
                    return tr("Добавление в ОКБ ещё не завершено. Проверим сохранённые этапы, чтобы не создавать повторные компании и контакты."), [[CHECK, CONTINUE], [STATS]], operation["value"]
        if command in {"/okb", *OKB_LABELS, "/pharmacy", PHARMACY}:
            if state and state.get("step") not in {"done", "stats_scope", "stats_period"}:
                return tr("Сначала закончите текущую форму или нажмите «Отмена»."), [[CANCEL]], state
            self.crm.validate_okb()
            state = {"kind": "pharmacy", "workflow": "okb", "step": "okb_bulk_input", "request_id": "datfo-okb-" + str(uuid4())}
            return intake('choose_pharmacy_form'), form_keyboard(state, self.store, user_id), state
        if not state or state.get("workflow") != "okb" or command in {"/start", "/menu", "/cancel", CANCEL, "/stats", STATS, "/help", "/pending"}:
            return super().route(user_id, text, state)
        step = state["step"]
        if step in {'okb_inn', 'okb_bulk_input'}:
            if text == TEXT_FORM:
                state['step'] = 'okb_bulk_input'
                return text_template(), form_keyboard(state, self.store, user_id), state
            if text == STEP_FORM:
                state['step'] = 'okb_bulk_input'
                return intake('choose_pharmacy_form'), form_keyboard(state, self.store, user_id), state
            if step == 'okb_bulk_input' or '\n' in text:
                return self.route_payload(user_id, parse_text(text), state)
        if step == "done":
            if text == CONFIRM:
                return self.success(state, self.store.operation(state["request_id"])["result"])
            return tr("Выберите следующее действие."), MENU, state
        if step == "okb_inn":
            inn = normalize_inn(text, self.config.inn_lengths)
            companies = self.crm.companies(inn)
            state.update(inn=inn, candidates=companies, company=None)
            if len(companies) > 20:
                return tr("По ИНН найдено больше 20 компаний. Нужна проверка дублей; повторите ИНН после исправления."), [[CANCEL]], state
            if companies:
                state["step"] = "okb_company"
                return self.choice_prompt(tr("Компания найдена. Подтвердите свою компанию:"), companies, state)
            state.update(step="okb_company_name", new_company=True)
            return tr("Компания с этим ИНН в Б24 не найдена. Введите название фирмы. После итогового подтверждения создадим компанию и реквизиты с ИНН."), [[CANCEL]], state
        if step == "okb_company":
            company = self.pick(text, state["candidates"])
            state.update(company=company, company_name=company["title"], new_company=False, step="okb_title")
            if state.get('bulk_ready'):
                return self.bulk_contact(user_id, state)
            return tr("Компания: ") + esc(company["title"]) + tr(". Теперь введите название аптечной точки."), [[CANCEL]], state
        if step == "okb_company_name":
            self.require_text(text, 120, tr("Название фирмы"))
            state.update(company_name=text, step="okb_title")
            if state.get('bulk_ready'):
                return self.bulk_contact(user_id, state)
            return tr("Введите название аптечной точки."), [[CANCEL]], state
        if step == "okb_title":
            self.require_text(text, 120, tr("Название аптеки"))
            state.update(title=text, step="okb_phone")
            return intake('phone'), [[tr("Без телефона")], [CANCEL]], state
        if step == "okb_phone":
            state.update(phone="" if text == "Без телефона" else normalize_phone(text), contact=None, create_contact=False)
            contacts = self.crm.contacts(state["phone"]) if state["phone"] else []
            if contacts:
                state.update(step="okb_contact", contact_candidates=contacts)
                return self.choice_prompt(tr("По телефону найдены контакты. Выберите контакт для привязки к аптеке и компании:"), contacts, state)
            if state['phone']:
                return self.new_contact_prompt(state)
            return self.region_prompt(state)
        if step == 'okb_contact_create':
            answer = text.casefold()
            if text == NO_CONTACT or answer in {'нет', 'йўқ'}:
                state.update(phone='', contact=None, create_contact=False)
                if state.get('bulk_ready'):
                    return self.finish_bulk(user_id, state)
                return self.region_prompt(state)
            if text != CREATE_CONTACT and answer not in {'да', 'ҳа'}:
                raise ValueError(phrase('choice'))
            state.update(create_contact=True, step='okb_contact_name')
            return intake('name'), [[CANCEL]], state
        if step == 'okb_contact_phone':
            phone = state['phone'] if text == USE_CONTACT_PHONE else normalize_phone(text)
            contacts = self.crm.contacts(phone)
            state.update(phone=phone, contact=None)
            if contacts:
                state.update(step='okb_contact', contact_candidates=contacts, create_contact=False)
                return self.choice_prompt(tr('Контакт с этим номером уже есть. Выберите его — новый контакт не создаём:'), contacts, state)
            state.update(step='okb_contact_name')
            return intake('name'), [[CANCEL]], state
        if step == 'okb_contact_name':
            self.require_text(text, 100, tr('Имя контакта'))
            positions = self.crm.contact_positions(fresh=True)
            state.update(contact_name=text, contact_position_candidates=positions, step='okb_contact_position')
            return self.choice_prompt(tr('Выберите должность из списка Б24 «Должность список»:'), positions, state)
        if step == 'okb_contact_position':
            state['contact_position'] = self.pick(text, state['contact_position_candidates'])
            if state.get('bulk_ready'):
                return self.finish_bulk(user_id, state)
            return self.region_prompt(state)
        if step == "okb_contact":
            state["contact"] = self.pick(text, state["contact_candidates"])
            state['create_contact'] = False
            if state.get('bulk_ready'):
                return self.finish_bulk(user_id, state)
            return self.region_prompt(state)
        if step == "okb_region":
            region = self.pick(text, state["region_candidates"])
            cities = self.crm.cities_for_region(region['id'])
            state.update(business_region=region, city_candidates=cities, step="okb_city_choice")
            return self.city_prompt(state)
        if step == "okb_city_search":
            # Drafts from the previous version also search only inside their chosen region.
            self.require_text(text, 100, tr("Город/район"))
            candidates = [item for item in self.crm.cities_for_region(state['business_region']['id']) if text.casefold() in item["title"].casefold()]
            if not candidates:
                return tr("Город/район не найден в справочнике Б24. Попробуйте другое написание."), [[CANCEL]], state
            if len(candidates) > 15:
                return tr("Найдено слишком много вариантов. Введите название точнее."), [[CANCEL]], state
            state.update(city_candidates=candidates, step="okb_city_choice")
            return self.choice_prompt(tr("Выберите город или район:"), candidates, state)
        if step == "okb_city_choice":
            allowed = self.crm.cities_for_region(state['business_region']['id'])
            # Recheck persisted choices: an old global search must not cross regions.
            candidates = [city for city in state['city_candidates'] if city['id'] in {item['id'] for item in allowed}]
            if candidates != state['city_candidates']:
                state['city_candidates'] = allowed
                return self.city_prompt(state)
            try:
                city = self.pick(text, candidates)
            except ValueError:
                matches = [city for city in allowed if text.casefold() in city['title'].casefold()]
                if not matches:
                    raise ValueError(tr('В этом регионе город/район не найден. Выберите кнопку из списка или уточните название.')) from None
                state['city_candidates'] = matches
                return self.city_prompt(state)
            state.update(city=city, step="okb_address")
            return tr("Введите физический адрес аптеки: улица и дом. Это адрес точки, а не юридический адрес фирмы."), [[CANCEL]], state
        if step == "okb_address":
            self.require_text(text, 500, tr("Адрес"))
            state.update(address=text, landmark='', step='okb_location_choice')
            return intake('location_choice'), [[ADD_LOCATION, SKIP_LOCATION], [CANCEL]], state
        if step in {'okb_location_choice', 'okb_location'}:
            if text == SKIP_LOCATION:
                state.pop('location', None)
                return self.program_prompt(user_id, state)
            if text == ADD_LOCATION:
                state['step'] = 'okb_location'
                return intake('location_send'), location_keyboard(), state
            raise ValueError(intake('location_invalid'))
        if step == "okb_landmark":
            if text != NO_LANDMARK:
                self.require_text(text, 500, tr("Ориентир"))
            state.update(landmark="" if text == NO_LANDMARK else text, step="confirm")
            return self.program_prompt(user_id, state)
        if step=='okb_program':
            state.update(current_program=self.pick(text,state['program_candidates']),step='confirm')
            return self.finish_form(user_id,state)
        if step == "confirm":
            if text != CONFIRM:
                raise ValueError(phrase('choice'))
            return self.submit(user_id, state)
        return super().route(user_id, text, state)

    def new_contact_prompt(self, state):
        state.update(step='okb_contact_create', create_contact=False)
        return tr('Контакт с этим номером не найден. Создать новый контакт?'), [[CREATE_CONTACT], [NO_CONTACT], [CANCEL]], state

    def route_location(self, user_id, location, state):
        if not state or state.get('step') not in {'okb_location_choice', 'okb_location'}:
            return super().route_location(user_id, location, state)
        if self.store.unfinished(user_id):
            raise ValueError(tr('Сначала проверьте сохранение через /pending.'))
        state['location'] = coordinates(location)
        return self.program_prompt(user_id, state)

    def route_payload(self, user_id, data, state):
        if not state or state.get('step') not in {'okb_inn', 'okb_bulk_input'} or self.store.unfinished(user_id):
            raise ValueError(tr('На этом шаге форма недоступна.'))
        values = collect(data, self.crm, self.config)
        companies = self.crm.companies(values['inn'])
        if len(companies) > 20:
            raise ValueError(tr('По ИНН найдено больше 20 компаний. Нужна проверка дублей.'))
        state.update(values, company=None, new_company=not companies)
        if not companies and not values['company_name']:
            state['step'] = 'okb_company_name'
            return tr('Компания не найдена по ИНН. Заполните название фирмы.'), [[CANCEL]], state
        if len(companies) > 1:
            state.update(step='okb_company', candidates=companies)
            return self.choice_prompt(tr('Компания найдена. Подтвердите свою компанию:'), companies, state)
        if companies:
            state.update(company=companies[0], company_name=companies[0]['title'])
        return self.bulk_contact(user_id, state)

    def bulk_contact(self, user_id, state):
        if state.get('phone_only'):
            state.update(contact=None,create_contact=False)
            return self.finish_bulk(user_id,state)
        contacts = self.crm.contacts(state['phone']) if state['phone'] else []
        state['contact'] = None
        if len(contacts) > 1:
            state.update(step='okb_contact', contact_candidates=contacts, create_contact=False)
            return self.choice_prompt(tr('По телефону найдены контакты. Выберите контакт для привязки к аптеке и компании:'), contacts, state)
        if contacts:
            state.update(contact=contacts[0], create_contact=False)
        elif state['phone']:
            if not state.get('create_contact'):
                return self.new_contact_prompt(state)
            if not state.get('contact_name'):
                state['step'] = 'okb_contact_name'
                return intake('name'), [[CANCEL]], state
            if not state.get('contact_position'):
                positions = self.crm.contact_positions(fresh=True)
                if state.get('bulk_position'):
                    state['contact_position'] = pick_field(state['bulk_position'], positions, tr('Должность контакта'))
                    return self.finish_bulk(user_id, state)
                state.update(step='okb_contact_position', contact_position_candidates=positions)
                return self.choice_prompt(tr('Выберите должность из списка Б24 «Должность список»:'), positions, state)
        return self.finish_bulk(user_id, state)

    def finish_bulk(self, user_id, state):
        state['step'] = 'confirm'
        return self.finish_form(user_id, state)

    def program_prompt(self, user_id, state):
        if 'current_program' in self.config.targets['pharmacy'].fields:
            state.update(step='okb_program', program_candidates=self.crm.choices('current_program'))
            return self.choice_prompt(tr('💻 Ҳозир ишлатилаётган дастурни танланг — мажбурий:'), state['program_candidates'], state)
        state['step'] = 'confirm'
        return self.finish_form(user_id, state)

    def finish_form(self,user_id,state):
        existing = self.crm.duplicate_pharmacy(state) if state.get("company") else None
        state["existing_pharmacy"] = existing
        self.store.prepare(user_id, state, utc_text(self.clock()))
        return self.preview(user_id, state)

    @staticmethod
    def pick(text: str, candidates: list[dict]):
        for index, item in enumerate(candidates, 1):
            if text == f"{index}. {item['title'][:100]}":
                return item
        if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= len(candidates):
            raise ValueError(tr("Выберите номер из показанного списка."))
        return candidates[int(text) - 1]

    @staticmethod
    def choice_prompt(title: str, candidates: list[dict], state: dict):
        lines = [title] + [f"{index}. {esc(item['title'][:100])}" for index, item in enumerate(candidates, 1)]
        return "\n".join(lines), [[f"{index}. {item['title'][:100]}"] for index, item in enumerate(candidates, 1)] + [[CANCEL]], state

    def region_prompt(self, state):
        candidates = self.crm.choices("business_region")
        state.update(step="okb_region", region_candidates=candidates)
        return self.choice_prompt(intake('region'), candidates, state)

    def city_prompt(self, state):
        title = tr('📍 Регион: ') + esc(state['business_region']['title']) + '\n' + intake('city')
        return self.choice_prompt(title, state['city_candidates'], state)

    def preview(self, user_id: int, state: dict):
        if state.get("workflow") != "okb":
            return super().preview(user_id, state)
        text = '<b>'+intake('preview')+'</b>\n\n'+pharmacy_blocks(state, self.config.users[user_id].name)
        if state.get('existing_pharmacy'):
            text += '\n\n'+tr('Такая аптека уже существует. Привяжем выбранный контакт; справочные поля существующей аптеки сохранятся.')
        text += '\n\n'+intake('confirm_hint')
        return text, [[CONFIRM, CANCEL]], state
    def phase(self, state: dict, name: str, find, create, *, verify_only: bool):
        self.last_error_step = name
        request_id = state["request_id"]
        previous = self.store.operation_step(request_id, name)
        if previous and previous["status"] in {"succeeded", "succeeded_write"}:
            return previous["result"]
        found = find()
        if found is not None:
            recovered_write = previous and previous["status"] in {"sending", "uncertain"}
            self.store.set_operation_step(request_id, name, "succeeded_write" if recovered_write else "succeeded", found)
            return found
        if previous and previous["status"] in {"sending", "uncertain"}:
            raise PendingStep(name, uncertain=True)
        if verify_only:
            raise PendingStep(name, uncertain=False)
        self.store.set_operation_step(request_id, name, "sending")
        self.store.status(request_id, "sending")
        try:
            result = create()
        except RemoteError as exc:
            self.store.set_operation_step(request_id, name, "uncertain" if exc.uncertain else "rejected")
            self.store.status(request_id, "uncertain" if exc.uncertain else ("created" if self.store.operation_wrote(request_id) else "rejected"))
            raise
        except ConfigError:
            self.store.set_operation_step(request_id, name, "rejected")
            self.store.status(request_id, "created" if self.store.operation_wrote(request_id) else "rejected")
            raise
        self.store.set_operation_step(request_id, name, "succeeded_write", result)
        return result

    def submit(self, user_id: int, state: dict, *, verify_only: bool = False):
        self.last_error_code = None
        self.last_error_step = None
        if state.get("workflow") != "okb":
            return super().submit(user_id, state, verify_only=verify_only)
        operation = self.store.operation(state["request_id"])
        if not operation or operation["user_id"] != user_id:
            raise ConfigError(tr("Не найден черновик ОКБ"))
        state = operation["value"]
        if operation["status"] in {"succeeded", "existing"}:
            return self.success(state, operation["result"])
        manager_id = self.config.users[user_id].bitrix_id
        try:
            self.crm.validate_okb(state)
            # All ambiguity checks happen before the first mutation.
            contact = state.get("contact")
            current_contacts = self.crm.contacts(state["phone"]) if state["phone"] and not state.get('phone_only') else []
            if not state.get('phone_only') and not current_contacts and state["phone"] and self.store.other_inflight(state["request_id"], "phone", state["phone"], phase="contact"):
                return tr("Создание контакта с этим телефоном в другом запросе ещё не подтверждено. Повторите подтверждение после проверки того запроса."), [[CONFIRM, CANCEL]], state
            if contact and not any(row["id"] == contact["id"] for row in current_contacts):
                raise ConfigError(tr("Привязка телефона к контакту изменилась"))
            if not contact and len(current_contacts) > 1:
                raise ConfigError(tr("По телефону появились дубли; выберите контакт заново"))
            contact = contact or (current_contacts[0] if current_contacts else None)
            if state['phone'] and not contact and not state.get('phone_only'):
                # Validate consent and the live enum before creating even a company.
                self.crm.validate_new_contact(state)
            company = state.get("company")
            if company:
                if not any(row["id"] == company["id"] for row in self.crm.companies(state["inn"])):
                    raise ConfigError(tr("Связь ИНН с компанией изменилась"))
            else:
                if self.store.other_inflight(state["request_id"], "inn", state["inn"]):
                    return tr("Добавление компании с этим ИНН в другом запросе ещё не завершено. Сначала нужно проверить тот запрос, чтобы не создать дубль компании."), [[CONFIRM, CANCEL]], state
                if not self.store.operation_step(state["request_id"], "company") and not self.crm.find_company_request(state["request_id"]) and self.crm.companies(state["inn"]):
                    raise ConfigError(tr("По ИНН появилась компания; выберите её заново"))
                company = self.phase(state, "company", lambda: self.crm.find_company_request(state["request_id"]),
                    lambda: self.crm.create_company(state, manager_id), verify_only=verify_only)
            self.phase(state, "requisite", lambda: self.crm.find_requisite(company["id"], state["inn"]),
                lambda: self.crm.create_requisite(state, company), verify_only=verify_only)
            if state["phone"] and not contact and not state.get('phone_only'):
                contact = self.phase(state, "contact", lambda: self.crm.find_contact_request(state["request_id"]),
                    lambda: self.crm.create_contact(state, manager_id), verify_only=verify_only)
            if contact:
                self.phase(state, "contact_company", lambda: self.crm.contact_company_link(contact["id"], company["id"]),
                    lambda: self.crm.add_contact_company(contact["id"], company["id"]), verify_only=verify_only)
            actual_state = {**state, "company": company}
            result = self.phase(state, "pharmacy", lambda: self.crm.find_request("pharmacy", state["request_id"]) or self.crm.duplicate_pharmacy(actual_state),
                lambda: self.crm.create_okb_pharmacy(state, company, manager_id, contact), verify_only=verify_only)
            if contact:
                self.phase(state, "pharmacy_contact", lambda: self.crm.pharmacy_contact_link(result["id"], contact["id"]),
                    lambda: self.crm.add_pharmacy_contact(result["id"], contact["id"]), verify_only=verify_only)
            if state.get('location'):
                self.phase(state, 'pharmacy_location', lambda: self.crm.pharmacy_location(result['id'], state['location']),
                    lambda: self.crm.add_pharmacy_location(result['id'], state['location']), verify_only=verify_only)
            # Native fields already contain the pharmacy data. Only an explicit
            # user comment needs a separate timeline write (including old drafts).
            if str(state.get("description") or "").strip() and not result.get("existing") and self.crm.needs_note("pharmacy"):
                self.phase(state, "note", lambda: {"confirmed": True} if self.crm.find_note(actual_state, result) else None,
                    lambda: self.add_note(actual_state, result, manager_id, user_id), verify_only=verify_only)
            contact_step = self.store.operation_step(state['request_id'], 'contact')
            result = {**result, "company_id": company["id"], "contact_id": contact["id"] if contact else None,
                      'contact_created':bool(contact_step and contact_step['status'] == 'succeeded_write')}
            self.store.status(state["request_id"], "existing" if result.get("existing") else "succeeded", result, utc_text(self.clock()))
            return self.success(state, result)
        except PendingStep as exc:
            if exc.uncertain:
                response = ''.join([tr('Этап «'), format(esc(exc.step), ''), tr('» пока не подтверждён. Повторную запись не отправляем. Нажмите «Проверить сохранение» позже. Номер: <code>'), format(state['request_id'], ''), '</code>.'])
                return response, [[CHECK]], state
            return tr("Сохранённые этапы проверены. Для завершения оставшихся шагов нажмите «Продолжить добавление в ОКБ»."), [[CONTINUE, CHECK]], state
        except ConfigError:
            return tr("Данные или поля Б24 изменились. Запись не продолжена. Если этапы уже сохранялись, сообщите администратору номер запроса; иначе отмените черновик и выберите данные заново. Номер: <code>") + state["request_id"] + "</code>.", [[CHECK, CONTINUE], [CANCEL]], state
        except RemoteError as exc:
            self.last_error_code = exc.code
            if exc.code == 'ACCESS_DENIED':
                if self.last_error_step == 'note':
                    return intake('pharmacy_note_access'), [[CHECK, CONTINUE]], state
                return tr('У подключения бота нет права на этот шаг добавления аптеки в Б24. Черновик и сохранённые этапы остаются; после исправления прав продолжим без дублей.'), [[CHECK, CONTINUE]], state
            return ''.join([tr('Добавление в ОКБ не завершено ('), format(esc(exc.code), ''), tr('). Сохранённые этапы не будут созданы повторно. Проверить: /pending. Номер: <code>'), format(state['request_id'], ''), '</code>.']), [[CHECK, CONTINUE]], state

    def add_note(self, state, result, manager_id, user_id):
        self.crm.add_note(state, result, manager_id, user_id)
        return {"confirmed": True}
