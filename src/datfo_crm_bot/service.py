from __future__ import annotations

from .i18n import tr

from datetime import datetime, timedelta, timezone
import html
import re
from uuid import uuid4

from .api import RemoteError
from .config import Config, ConfigError
from .crm import Crm
from .storage import Store
from .guidance import with_guide
from .navigation import check_phone_step, input_error, retry_keyboard, menu_text

DEAL = "➕ Сделка очиш"
PHARMACY = "🏪 Добавить аптеку"
SUPPORT = "🛠 Техник ёрдам заявкаси очиш"
DEAL_LABELS = {DEAL, "➕ Открыть сделку"}
SUPPORT_LABELS = {SUPPORT, "🛠 Заявка в поддержку", "🛠 Техник ёрдам сделкаси"}
STATS = "📊 Статистика"
CANCEL = "Отмена"
CONFIRM = "Подтвердить"
CHECK = "Проверить сохранение"
SKIP_INN = "Без ИНН"
UNBOUND = "Продолжить без компании"
SKIP_PHONE = "Без телефона"
MENU = [[DEAL, PHARMACY], [SUPPORT, STATS]]
KINDS = {DEAL: "deal", PHARMACY: "pharmacy", SUPPORT: "support", "/deal": "deal", "/pharmacy": "pharmacy", "/support": "support"}
KINDS.update({label:'deal' for label in DEAL_LABELS})
KINDS.update({label:'support' for label in SUPPORT_LABELS})
LABELS = {"deal": "Сделка", "pharmacy": "Аптека", "support": "Заявка в техподдержку"}


def esc(value) -> str:
    return html.escape(str(value))


def utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def normalize_inn(text: str, lengths: tuple[int, ...]) -> str:
    if not re.fullmatch(r"[0-9\s-]+", text):
        raise ValueError(tr("ИНН должен содержать только цифры; пробелы и дефисы допустимы."))
    value = re.sub(r"[\s-]", "", text)
    if len(value) not in lengths:
        raise ValueError(tr("Длина ИНН: ") + ", ".join(str(n) for n in lengths) + tr(" цифр."))
    return value


def normalize_phone(text: str) -> str:
    if not re.fullmatch(r"\+?[0-9()\s-]+", text):
        raise ValueError(tr("Введите номер телефона цифрами или нажмите «Без телефона». Пробелы, скобки и дефисы допустимы."))
    value = re.sub(r"[()\s-]", "", text)
    digits = value.lstrip("+")
    if not 7 <= len(digits) <= 15:
        raise ValueError(tr("В номере должно быть от 7 до 15 цифр; знак + и код страны необязательны."))
    if not value.startswith("+"):
        if len(digits) == 9:
            return "+998" + digits
        if len(digits) == 12 and digits.startswith("998"):
            return "+" + digits
    return value


class Bot:
    def __init__(self, config: Config, store: Store, crm: Crm, telegram, *, clock=None):
        self.config, self.store, self.crm, self.telegram = config, store, crm, telegram
        self.clock = clock or (lambda: datetime.now(config.timezone))

    def handle(self, update: dict) -> None:
        message = update.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("from"), dict):
            return
        sender, chat = message["from"], message.get("chat", {})
        if sender.get("is_bot") or not isinstance(chat, dict) or "id" not in chat or "id" not in sender:
            return
        update_id, user_id, chat_id = int(update["update_id"]), int(sender["id"]), int(chat["id"])
        # Recheck access before replaying a stored reply: removal from the allowlist revokes old statistics too.
        if chat.get("type") != "private" or chat_id != user_id:
            return
        text = message.get("text", "")
        if not isinstance(text, str):
            text = ""
        if text.split(maxsplit=1)[0:1] == ["/whoami"]:
            self.telegram.send(chat_id, ''.join([tr('Ваш Telegram ID: <code>'), format(user_id, ''), '</code>']))
            return
        if user_id not in self.config.users:
            self.telegram.send(chat_id, ''.join([tr('Доступ ещё не настроен. Передайте администратору ваш Telegram ID: <code>'), format(user_id, ''), '</code>.']))
            return
        saved = self.store.delivery(update_id)
        if saved:
            if saved.get("admin_only") and not self.config.users[user_id].admin:
                self.telegram.send(chat_id, tr("Права доступа изменились. Запросите статистику заново через /stats."), MENU)
                return
            self.telegram.send(saved["chat_id"], saved["text"], with_guide(saved["keyboard"]))
            return
        state = self.store.session(user_id)
        if text.strip() == CONFIRM and state and state.get('step')=='confirm':
            from .intake_ui import phrase
            self.telegram.send(chat_id, phrase('saving'))
        try:
            venue = message.get('venue')
            location = message.get('location') or (venue.get('location') if isinstance(venue, dict) else None)
            if message.get('web_app_data') is not None:
                import json
                web_data = message['web_app_data']
                raw = web_data.get('data', '') if isinstance(web_data, dict) else None
                if not isinstance(raw, str) or len(raw.encode('utf-8')) > 4096:
                    raise ValueError(tr('Форма не распознана. Откройте её заново.'))
                try:
                    data = json.loads(raw)
                except ValueError:
                    raise ValueError(tr('Форма не распознана. Откройте её заново.')) from None
                if not state or not isinstance(data, dict) or data.get('token') != state.get('request_id'):
                    raise ValueError(tr('Эта форма уже закрыта. Откройте новую форму через меню.'))
                response, keyboard, state = self.route_payload(user_id, data, state)
            elif location is not None:
                response, keyboard, state = self.route_location(user_id, location, state)
            else:
                if not self.store.unfinished(user_id):
                    check_phone_step(text.strip(), state)
                response, keyboard, state = self.route(user_id, text.strip(), state)
        except ConfigError:
            response, keyboard = tr("Этот процесс ещё не настроен в Б24. Черновик сохранён; сообщите администратору."), [[CANCEL]]
        except ValueError as exc:
            response, keyboard = input_error(state, exc), retry_keyboard(state, self.store, user_id)
        except RemoteError as exc:
            response, keyboard = ''.join([tr('Б24 сейчас не дал подтверждённый ответ ('), format(esc(exc.code), ''), tr('). Черновик сохранён. Повторите ввод или используйте /pending для проверки сохранения.')]), [[CANCEL]]
        if state and state.get('step')=='done' and getattr(self,'registration',None):
            keyboard=self.registration.keyboard(user_id)
        self.store.commit_reply(update_id, chat_id, user_id, state, response, keyboard, admin_only=self.config.users[user_id].admin)
        self.telegram.send(chat_id, response, with_guide(keyboard))

    def route_location(self, user_id, location, state):
        from .intake_ui import phrase
        raise ValueError(phrase('location_wrong_step'))

    def route_payload(self, user_id, data, state):
        raise ValueError(tr('На этом шаге форма недоступна.'))

    def route(self, user_id: int, text: str, state: dict | None):
        user = self.config.users[user_id]
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else text
        if command in {"/stats", STATS}:
            state = {"step": "stats_scope" if user.admin else "stats_period", "scope": "mine"}
            return (tr("Чья статистика нужна?"), [[tr("Мои"), tr("По команде")], [CANCEL]], state) if user.admin else self.period_prompt(state)
        if command == "/help":
            return tr("Меню: /deal — сделка, /pharmacy — аптека, /support — поддержка, /stats — создано через бота, /pending — проверка незавершённого сохранения, /cancel — отмена. ИНН ищет компанию в Б24, но не выдаёт доступ."), MENU, state
        pending = self.store.unfinished(user_id)
        if pending:
            operation = self.store.operation(pending[0]["request_id"])
            pending_state = operation["value"]
            if command == "/pending" or text == CHECK:
                return self.submit(user_id, pending_state, verify_only=True)
            return tr("Результат предыдущего сохранения ещё не подтверждён. Сначала проверим его, чтобы не создать повторную карточку."), [[CHECK, STATS]], pending_state
        if command in {"/cancel", CANCEL}:
            return menu_text(getattr(self, 'registration', None), user_id, cancelled=True), MENU, None
        if command in {"/start", "/menu"}:
            if state and state.get("step") not in {"done", "stats_scope", "stats_period"}:
                return tr("У вас есть черновик. Продолжите форму или отмените её кнопкой «Отмена»."), [[CANCEL]], state
            return tr("Datfo Sales: сделка, аптека, поддержка и статистика. Выберите действие."), MENU, None
        if command == "/pending":
            return tr("Сохранений с неопределённым результатом нет."), MENU, state
        kind = KINDS.get(command)
        if kind:
            if state and state.get("step") not in {"done", "stats_scope", "stats_period"}:
                return tr("Сначала закончите текущую форму или нажмите «Отмена»."), [[CANCEL]], state
            self.crm.validate(kind)
            state = {"kind": kind, "request_id": "datfo-" + str(uuid4()), "step": "title"}
            prompts = {"deal": tr("Как назовём сделку?"), "pharmacy": tr("Введите название аптечной точки."), "support": tr("Коротко напишите тему обращения.")}
            return prompts[kind], [[CANCEL]], state
        if not state:
            return menu_text(getattr(self, 'registration', None), user_id, text=text), MENU, None
        step = state["step"]
        if step == "done":
            if text == CONFIRM:
                return self.success(state, self.store.operation(state["request_id"])["result"])
            return tr("Выберите следующее действие."), MENU, state
        if step == "stats_scope":
            if text not in {"Мои", "По команде"}:
                raise ValueError(tr("Выберите «Мои» или «По команде»."))
            state["scope"] = "team" if text == "По команде" else "mine"
            state["step"] = "stats_period"
            return self.period_prompt(state)
        if step == "stats_period":
            return self.stats(user_id, state, text)
        if step == "title":
            self.require_text(text, 120, tr("Название"))
            state.update(title=text, step="inn")
            return tr("Введите ИНН компании или нажмите «Без ИНН»."), [[SKIP_INN], [CANCEL]], state
        if step == "inn":
            if text == SKIP_INN:
                state.update(inn="", company=None)
                return self.details_prompt(state)
            inn = normalize_inn(text, self.config.inn_lengths)
            companies = self.crm.companies(inn)
            state.update(inn=inn, candidates=companies, step="company")
            if not companies:
                return tr("По этому ИНН компания в Б24 не найдена. Можно исправить ИНН или сохранить карточку без привязки; введённый ИНН останется в описании."), [[tr("Изменить ИНН")], [UNBOUND], [CANCEL]], state
            if len(companies) > 20:
                state["step"] = "inn"
                return tr("По ИНН найдено больше 20 компаний. Требуется проверить дубли в Б24; автоматически выбирать компанию нельзя."), [[CANCEL]], state
            choices = "\n".join(''.join([format(index, ''), '. ', format(esc(company['title'][:100]), ''), tr(' · Б24 #'), format(company['id'], '')]) for index, company in enumerate(companies, 1))
            return ''.join([tr('ИНН '), format(inn, ''), '\n', format(choices, ''), tr('\n\nПодтвердите компанию: отправьте её номер из списка.')]), [[str(n) for n in range(1, min(len(companies), 4) + 1)], [tr("Изменить ИНН"), CANCEL]], state
        if step == "company":
            if text == "Изменить ИНН":
                state.update(step="inn", inn="", company=None)
                return tr("Введите ИНН компании."), [[SKIP_INN], [CANCEL]], state
            candidates = state.get("candidates", [])
            if not candidates and text == UNBOUND:
                state["company"] = None
                return self.details_prompt(state)
            if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= len(candidates):
                raise ValueError(tr("Отправьте номер компании из списка либо измените ИНН."))
            state["company"] = candidates[int(text) - 1]
            return self.details_prompt(state)
        if step in {"address", "description"}:
            self.require_text(text, 500 if step == "address" else 1500, tr("Адрес") if step == "address" else tr("Описание"))
            state[step] = text
            state["step"] = "phone"
            return tr("Телефон для связи или «Без телефона». Можно без +998."), [[SKIP_PHONE], [CANCEL]], state
        if step == "phone":
            state["phone"] = "" if text == SKIP_PHONE else normalize_phone(text)
            state["step"] = "confirm"
            self.store.prepare(user_id, state, utc_text(self.clock()))
            return self.preview(user_id, state)
        if step == "confirm":
            if text != CONFIRM:
                from .navigation import phrase
                raise ValueError(phrase('choice'))
            return self.submit(user_id, state)
        return tr("Выберите действие."), MENU, None

    @staticmethod
    def require_text(text: str, maximum: int, label: str) -> None:
        if not text or text.startswith("/") or len(text) > maximum:
            raise ValueError(''.join([format(label, ''), tr(': нужно от 1 до '), format(maximum, ''), tr(' символов текста.')]))

    def details_prompt(self, state: dict):
        is_pharmacy = state["kind"] == "pharmacy"
        state["step"] = "address" if is_pharmacy else "description"
        prompt = tr("Введите адрес аптечной точки: город, улица и дом.") if is_pharmacy else (
            tr("Опишите проблему: что не работает и что ожидалось.") if state["kind"] == "support" else tr("Коротко опишите предмет сделки и следующий шаг."))
        return prompt, [[CANCEL]], state

    def preview(self, user_id: int, state: dict):
        company = state.get("company")
        target = self.config.targets[state["kind"]]
        responsible = (tr("назначенный специалист поддержки") if state["kind"] == "support" else tr("назначенный менеджер")) if target.responsible_id else (self.config.users[user_id].name or tr("вы"))
        lines = [''.join(['<b>', format(tr(LABELS[state['kind']]), ''), tr(' — перед сохранением</b>')]), ''.join([tr('Название: '), format(esc(state['title']), '')]),
                 ''.join([tr('ИНН: '), format(esc(state.get('inn') or 'не указан'), '')]),
                 ''.join([tr('Компания: '), format(esc(company['title'][:300]), ''), tr(' · Б24 #'), format(company['id'], '')]) if company else tr("Компания: без привязки"),
                 ''.join([tr('Ответственный: '), format(esc(responsible), '')])]
        for key, label in (("address", tr("Адрес")), ("description", tr("Описание")), ("phone", tr("Телефон"))):
            if state.get(key):
                lines.append(f"{label}: {esc(state[key])}")
        lines.append(tr("Нажмите «Подтвердить», чтобы сохранить в Б24."))
        return "\n".join(lines), [[CONFIRM, CANCEL]], state

    def submit(self, user_id: int, state: dict, *, verify_only: bool = False):
        operation = self.store.operation(state["request_id"])
        if not operation or operation["user_id"] != user_id:
            raise ConfigError(tr("Не найден подтверждённый черновик пользователя"))
        # Use the frozen preview, rather than any subsequently changed dialog fields.
        state = operation["value"]
        if operation["status"] in {"succeeded", "existing"}:
            return self.success(state, operation["result"])
        if operation["status"] == "created":
            return self.finish(user_id, state, operation["result"])
        self.crm.validate(state["kind"])
        found = self.crm.find_request(state["kind"], state["request_id"])
        if found:
            return self.finish(user_id, state, found)
        if verify_only or operation["status"] in {"sending", "uncertain"}:
            state["step"] = "confirm"
            return tr("Б24 пока не подтвердил карточку с этим номером запроса. Повторную запись не отправляю. Нажмите «Проверить сохранение» позже; при длительной неопределённости передайте администратору номер запроса: <code>") + esc(state["request_id"]) + "</code>.", [[CHECK, STATS]], state
        if state.get("company"):
            current = self.crm.companies(state["inn"])
            if not any(row["id"] == state["company"]["id"] for row in current):
                return tr("Связь ИНН с выбранной компанией изменилась в Б24. Отмените черновик и выберите компанию заново."), [[CANCEL]], state
        duplicate = self.crm.duplicate_pharmacy(state)
        if duplicate:
            self.store.status(state["request_id"], "existing", duplicate, utc_text(self.clock()))
            return self.success(state, duplicate)
        sent = False
        def mark_submitted():
            nonlocal sent
            self.store.status(state["request_id"], "sending")
            sent = True
        try:
            result = self.crm.create(state, self.config.users[user_id].bitrix_id, user_id, on_submit=mark_submitted)
        except ConfigError:
            self.store.status(state["request_id"], "rejected")
            raise
        except RemoteError as exc:
            self.store.status(state["request_id"], "uncertain" if sent and exc.uncertain else "rejected")
            if not sent:
                return ''.join([tr('Не удалось проверить подключение перед записью ('), format(esc(exc.code), ''), tr('). Запись не отправлена; повторите подтверждение позже.')]), [[CONFIRM, CANCEL]], state
            if exc.uncertain:
                return tr("Запрос отправлен, но ответ потерян. Проверим Б24 по номеру запроса; повторно создавать карточку пока нельзя."), [[CHECK, STATS]], state
            if exc.code == "ACCESS_DENIED":
                return tr("Не удалось сохранить: у подключения бота нет права создавать карточки в этой воронке Б24. Черновик сохранён; после исправления прав можно повторить подтверждение."), [[CONFIRM, CANCEL]], state
            return ''.join([tr('Б24 отклонил сохранение ('), format(esc(exc.code), ''), tr('). Карточка не подтверждена, черновик сохранён. Администратор может исправить настройки; затем повторите подтверждение.')]), [[CONFIRM, CANCEL]], state
        return self.finish(user_id, state, result)

    def finish(self, user_id: int, state: dict, result: dict):
        if not self.crm.needs_note(state["kind"]):
            self.store.status(state["request_id"], "succeeded", result, utc_text(self.clock()))
            return self.success(state, result)
        # The pharmacy has no native description field. Preserve the entered INN and author in its timeline.
        self.store.status(state["request_id"], "created", result)
        if self.crm.find_note(state, result):
            self.store.status(state["request_id"], "succeeded", result, utc_text(self.clock()))
            return self.success(state, result)
        if result.get("note_status") in {"sending", "uncertain"}:
            return ''.join([tr('Карточка #'), format(result['id'], ''), tr(' уже создана; Б24 пока не подтвердил примечание с данными формы. Повторим проверку.\n<a href="'), format(esc(result['url']), ''), tr('">Открыть карточку</a>')]), [[CHECK, STATS]], state
        result["note_status"] = "sending"
        self.store.status(state["request_id"], "created", result)
        try:
            self.crm.add_note(state, result, self.config.users[user_id].bitrix_id, user_id)
        except RemoteError as exc:
            result["note_status"] = "uncertain" if exc.uncertain else "rejected"
            self.store.status(state["request_id"], "created", result)
            return ''.join([tr('Карточка #'), format(result['id'], ''), tr(' создана. Примечание с данными формы пока не подтверждено ('), format(esc(exc.code), ''), tr('); нажмите «Проверить сохранение».\n<a href="'), format(esc(result['url']), ''), tr('">Открыть карточку</a>')]), [[CHECK, STATS]], state
        result["note_status"] = "confirmed"
        self.store.status(state["request_id"], "succeeded", result, utc_text(self.clock()))
        return self.success(state, result)

    @staticmethod
    def success(state: dict, result: dict):
        state["step"] = "done"
        action = tr("Уже есть в Б24; повторную аптеку не создавали") if result.get("existing") else tr("Сохранено в Б24")
        return ''.join(['✅ ', format(action, ''), ': ', format(tr(LABELS[state['kind']]), ''), ' #', format(result['id'], ''), '.\n<a href="', format(esc(result['url']), ''), tr('">Открыть карточку</a>')]), MENU, state

    @staticmethod
    def period_prompt(state: dict):
        return tr("За какой период показать создания через бот и в Б24?"), [[tr("Сегодня"), tr("Эта неделя"), tr("Этот месяц")], [CANCEL]], state

    def stats(self, user_id: int, state: dict, text: str):
        now = self.clock().astimezone(self.config.timezone)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if text == "Эта неделя":
            start -= timedelta(days=start.weekday())
        elif text == "Этот месяц":
            start = start.replace(day=1)
        elif text != "Сегодня":
            from .navigation import phrase
            raise ValueError(phrase('choice'))
        team = state.get("scope") == "team" and self.config.users[user_id].admin
        if getattr(self, 'creation_statistics', None):
            end = (now + timedelta(seconds=1)).replace(microsecond=0)
            if team:
                report = self.creation_statistics.counts(start.isoformat(), end.isoformat(), None)
            else:
                report = self.creation_statistics.personal_counts(start.isoformat(), end.isoformat(),
                    self.config.users[user_id].bitrix_id, self.store, user_id,
                    kinds=('deal', 'pharmacy', 'support'))
            lines = [tr('<b>📊 Создано · ') + (tr('команда') if team else tr('мои')) + '</b>',
                     tr('С ') + start.strftime('%d.%m.%Y %H:%M') + tr(' по ') + now.strftime('%d.%m.%Y %H:%M')
                     + ' · ' + esc(self.config.timezone.key)]
            crm_heading = '<b>🏢 Всего в Б24</b>' if team else '<b>🏢 Создано вами в Б24</b>'
            for source, heading in (('bot', '<b>🤖 Через бот</b>'), ('all', crm_heading)):
                lines.extend(['', tr(heading)])
                for kind, label in (('deal', 'Сделки: '), ('pharmacy', 'Аптеки: '), ('support', 'Заявки техподдержки: ')):
                    metric = report[source][kind]
                    value = str(metric['count']) if 'count' in metric else tr('недоступно')
                    lines.append(tr(label) + value)
            if team:
                lines.extend(['', tr('Оба блока — по дате создания в Б24. «Через бот» входит в «Всего в Б24»; складывать их не нужно.'),
                              tr('«По команде» — все записи, доступные подключению Б24.')])
            else:
                from .statistics_content import TEXTS
                lines.extend(['', tr(TEXTS['explanation']['ru'])])
            lines.append(tr('Сделки — все воронки, кроме техподдержки. Аптеки — ОКБ. Заявки — воронка «Техобслуживание [KG]».'))
            if any('error' in metric for source in report.values() for metric in source.values()):
                lines.append(tr('Часть данных Б24 сейчас недоступна. Повторите /stats позже; это не нулевой результат.'))
            return '\n'.join(lines), MENU, None
        counts = self.store.statistics(utc_text(start), utc_text(now + timedelta(microseconds=1)), None if team else user_id)
        lines = [tr("<b>Создано через бота · ") + (tr("команда") if team else tr("мои действия")) + "</b>",
                 ''.join([tr('С '), format(start, f'%d.%m.%Y %H:%M'), tr(' по '), format(now, f'%d.%m.%Y %H:%M'), ' · ', format(esc(self.config.timezone.key), '')]),
                 ''.join([tr('Сделки: '), format(counts.get('deal', 0), '')]), ''.join([tr('Аптеки: '), format(counts.get('pharmacy', 0), '')]), ''.join([tr('Заявки техподдержки: '), format(counts.get('support', 0), '')]),
                 tr("Считаются подтверждённые создания через этот бот по времени подтверждения. Найденные существующие аптеки не прибавляются. Это журнал создания, а не текущие статусы и не вся CRM.")]
        return "\n".join(lines), MENU, None
