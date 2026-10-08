from __future__ import annotations

from .i18n import tr

from dataclasses import dataclass
import html
import hashlib
import json
from pathlib import Path

from .api import Bitrix, RemoteError
from .config import ConfigError, ROOT, User, positive_id
from .storage import Store
from .guidance import with_guide
from .navigation import menu_text

REGISTER = "Зарегистрироваться"
PROFILE = "👤 Мой профиль"
PROFILE_LABELS = {PROFILE, "Моя регистрация"}
REQUESTS = "Заявки менеджеров"
MEMBERS = "Подключённые менеджеры"
CANCEL = "Отмена регистрации"


@dataclass(frozen=True)
class RegistrationSettings:
    admins: dict[int, User]

    @classmethod
    def load(cls, path: Path | None = None):
        path = path or ROOT / "registration.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raise ConfigError(tr("Не удалось прочитать registration.json")) from None
        if not isinstance(data, dict) or not isinstance(data.get("admins"), dict) or not data["admins"]:
            raise ConfigError(tr("registration.admins: нужен хотя бы один администратор"))
        admins = {}
        for key, value in data["admins"].items():
            if not isinstance(value, dict) or not isinstance(value.get("name"), str) or not value["name"].strip():
                raise ConfigError(tr("registration.admins: нужны bitrix_id и name"))
            admins[positive_id(key, tr("Telegram ID администратора"))] = User(
                positive_id(value.get("bitrix_id"), tr("Б24 ID администратора")), True, value["name"].strip())
        if len({user.bitrix_id for user in admins.values()}) != len(admins):
            raise ConfigError(tr("Один сотрудник Б24 не может иметь несколько аккаунтов администратора"))
        return cls(admins)


class Directory:
    """Only employee ID/name/activity are retained, never contact details or credentials."""
    def __init__(self, api: Bitrix):
        self.api = api

    def _users(self, filters: dict) -> list[dict]:
        rows = self.api.list_all("user.get", {
            "FILTER": {"ACTIVE": True, "USER_TYPE": "employee", **filters},
            "select": ["ID", "ACTIVE", "NAME", "LAST_NAME", "USER_TYPE"]})
        result = []
        for row in rows:
            if row.get("ACTIVE") not in (True, "Y", "true", 1) or row.get("USER_TYPE", "employee") != "employee":
                continue
            name = " ".join(str(row.get(key) or "").strip() for key in ("NAME", "LAST_NAME")).strip()
            if name:
                result.append({"id": positive_id(row.get("ID"), tr("ID сотрудника Б24")), "name": name})
        return result

    def search(self, name: str) -> list[dict]:
        return self._users({"NAME_SEARCH": name})

    def employee(self, employee_id: int) -> dict | None:
        rows = self._users({"ID": employee_id})
        matches = [row for row in rows if row["id"] == employee_id]
        return matches[0] if len(matches) == 1 else None


class Registration:
    def __init__(self, settings: RegistrationSettings, store: Store, directory: Directory, telegram):
        self.settings, self.store, self.directory, self.telegram = settings, store, directory, telegram
        self.okb_enabled = False
        self.live_deals_enabled = False
        self.work_inbox = None
        self.personal_statistics_enabled = False
        self.commands = None
        bind = getattr(telegram, 'bind_registration', None)
        if callable(bind):
            bind(self)

    def direct_form_keyboard(self, telegram_id, keyboard):
        """Keep action buttons direct even when an old reply restores a menu."""
        work = self.work_inbox
        if keyboard is not None and work and self.user(telegram_id):
            from .service import DEAL_LABELS, SUPPORT_LABELS, PHARMACY, STATS
            from .okb_service import OKB_LABELS
            from .work_inbox import NEW, INBOX, OLD_LABELS, STATS as SUPPORT_STATS
            from .personal_statistics import MY_STATS
            from .live_deals import MY_CRM_LABELS
            from .reminders import REMINDERS
            from .language_ui import SETTINGS
            from .guidance import GUIDE_LABELS
            known = {*DEAL_LABELS, *SUPPORT_LABELS, *OKB_LABELS, *MY_CRM_LABELS, *PROFILE_LABELS,
                     *GUIDE_LABELS, PHARMACY, STATS, MY_STATS, REMINDERS, SETTINGS, INBOX, SUPPORT_STATS,
                     *(label for label, action in OLD_LABELS.items() if action == INBOX)}
            renderings = lambda labels: {tr(label, language) for label in labels for language in (None, 'ru', 'uz')}
            shown = {button if isinstance(button, str) else button.get('text') for row in keyboard for button in row}
            old_main = shown <= renderings(known) and (
                (shown & renderings(DEAL_LABELS) and shown & renderings(SUPPORT_LABELS)) or
                (shown & renderings(MY_CRM_LABELS) and shown & renderings(PROFILE_LABELS)))
            test_mode = self.store.db.execute('SELECT value FROM settings WHERE key=?',
                                             ('b24-test:' + str(telegram_id),)).fetchone()
            if (old_main and self.live_deals_enabled and not (test_mode and test_mode[0] == '1') and
                    not (work.simulation and work.simulation.active(telegram_id)) and
                    not (work.test_inbox and work.test_inbox.store.mode(telegram_id))):
                return self.keyboard(telegram_id)
            blocked = set()
            if not self.sales_allowed(telegram_id):
                blocked |= {*DEAL_LABELS,*SUPPORT_LABELS,*OKB_LABELS,PHARMACY,STATS,MY_STATS,NEW,'/deal','/support','/okb','/pharmacy','/stats','/my_stats'}
            if not self.support_statistics_allowed(telegram_id):
                blocked |= {SUPPORT_STATS,'/requests_stats'}
            if not self.support_inbox_allowed(telegram_id):
                blocked |= {INBOX, '/requests', *(label for label, action in OLD_LABELS.items() if action == INBOX)}
            blocked = {tr(label,language) for label in blocked for language in (None,'ru','uz')}
            keyboard = [[button for button in row if (button if isinstance(button,str) else button.get('text')) not in blocked] for row in keyboard]
            keyboard = [row for row in keyboard if row]
        if (keyboard is None or not self.live_deals_enabled or not work or
                not self.user(telegram_id) or work.store.role(telegram_id) != 'fom_sales'):
            return keyboard
        if ((work.simulation and work.simulation.active(telegram_id)) or
                (work.test_inbox and work.test_inbox.store.mode(telegram_id))):
            return keyboard
        test_mode = self.store.db.execute('SELECT value FROM settings WHERE key=?',
                                         ('b24-test:' + str(telegram_id),)).fetchone()
        if test_mode and test_mode[0] == '1':
            return keyboard
        from .service import DEAL_LABELS, SUPPORT_LABELS, PHARMACY
        from .okb_service import OKB_LABELS
        from .work_inbox import NEW
        labels = {tr(label, language): mode
                  for mode, values in (('deal', DEAL_LABELS), ('support', SUPPORT_LABELS | {NEW}),
                                       ('pharmacy', OKB_LABELS | {PHARMACY}))
                  for label in values for language in (None, 'ru', 'uz')}
        rows = []
        for row in keyboard:
            buttons = []
            for button in row:
                text = button if isinstance(button, str) else button.get('text') if isinstance(button, dict) else None
                mode = labels.get(text)
                if mode and (isinstance(button, str) or set(button).issubset({'text', 'web_app'})):
                    button = self.form_action_button(telegram_id, mode, text)
                buttons.append(button)
            rows.append(buttons)
        return rows

    def form_action_button(self, telegram_id, mode, label):
        """An unopened form is resumed with its existing token, never replaced."""
        from .input_forms import form_keyboard, launch_button
        state = None
        if mode == 'deal':
            draft = self.store.session(telegram_id)
            if draft and draft.get('sales_intake') and draft.get('step') == 'sales_bulk_input':
                state = draft
        elif mode == 'support' and self.work_inbox:
            session = getattr(self.work_inbox.store, 'session', None)
            draft = session(telegram_id) if callable(session) else None
            if draft and draft.get('web_intake') and draft.get('step') == 'web_form':
                state = draft
        elif mode == 'pharmacy':
            draft = self.store.session(telegram_id)
            if draft and draft.get('workflow') == 'okb' and draft.get('step') == 'okb_bulk_input':
                state = draft
        if state:
            rows = form_keyboard(state, self.store, telegram_id)
            if rows and isinstance(rows[0][0], dict):
                return {**rows[0][0], 'text': label}
        return launch_button(self.store, telegram_id, mode, label)

    def bootstrap(self) -> None:
        for telegram_id, user in self.settings.admins.items():
            employee = self.directory.employee(user.bitrix_id)
            if not employee or employee["name"].casefold() != user.name.casefold():
                raise ConfigError(tr("Учётная запись администратора не подтверждена в Б24"))
            current = self.store.registration(telegram_id)
            if current:
                if current["status"] != "approved" or current["bitrix_id"] != user.bitrix_id:
                    raise ConfigError(tr("Существующая регистрация администратора не совпадает с настройками"))
                continue
            self.store.request_registration(telegram_id, telegram_id, employee)
            if not self.store.approve_registration(telegram_id, telegram_id, employee):
                raise ConfigError(tr("Учётная запись администратора уже привязана к другому Telegram ID"))

    def user(self, telegram_id: int) -> User | None:
        row = self.store.registration(telegram_id)
        if not row or row["status"] != "approved":
            return None
        return User(row["bitrix_id"], telegram_id in self.settings.admins, row["name"])

    def sales_allowed(self, user):
        return bool(self.user(user)) and (not self.work_inbox or self.work_inbox.store.role(user) == 'fom_sales')

    def support_inbox_allowed(self, user):
        return bool(self.user(user) and self.work_inbox and self.work_inbox.store.role(user) in {'tech', 'trainer', 'dispatcher'})

    def support_statistics_allowed(self, user):
        return self.support_inbox_allowed(user)

    def command_menu(self, user, commands):
        common = {'start','profile','help','guide','next','whoami','register','cancel','pending'}
        if self.user(user):
            common |= {'my_crm','reminders','request_cancel'}
            if self.work_inbox and self.work_inbox.store.role(user):
                common |= {'requests_mine','requests_sent','request_ticket','diagnostics'}
            if self.support_inbox_allowed(user):
                common |= {'requests'}
            if self.sales_allowed(user):
                common |= {'deal','pharmacy','okb','support','request','stats','my_stats','crm_deal'}
            elif self.support_statistics_allowed(user):
                common |= {'requests_stats'}
        from .role_content import TEXTS
        return [{**row, 'description': TEXTS['tasks_tech']['ru']}
                if row['command'] == 'my_crm' and self.user(user) and not self.sales_allowed(user) else row
                for row in commands if row['command'] in common]

    def refresh_commands(self, user):
        if self.commands is None or not callable(getattr(self.telegram, 'call', None)):
            return
        commands = self.command_menu(user, self.commands)
        from .i18n import stored_language
        digest=hashlib.sha256(json.dumps({'commands':commands,'language':stored_language(self.store,user)},ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()
        key='role-command-menu:'+str(user)
        saved=self.store.db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        if saved and saved[0]==digest:
            return
        try:
            for language in (None, 'ru', 'uz'):
                payload = {'scope': {'type':'chat','chat_id':user}, 'commands': [
                    {**row,'description':tr(row['description'], language or stored_language(self.store,user))} for row in commands]}
                if language:
                    payload['language_code'] = language
                self.telegram.call('setMyCommands', payload)
            with self.store.db:
                self.store.db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,digest))
        except RemoteError as exc:
            if exc.code in {'TELEGRAM_401','TELEGRAM_403'}:
                raise

    def keyboard(self, telegram_id: int) -> list[list[str]]:
        from .navigation import MORE
        if not self.user(telegram_id):
            return with_guide([[REGISTER], [PROFILE]])
        sales = self.sales_allowed(telegram_id)
        rows = []
        if self.live_deals_enabled:
            from .live_deals import MY_CRM
            from .service import DEAL, SUPPORT
            if sales:
                rows.append([self.form_action_button(telegram_id, 'deal', DEAL),
                             self.form_action_button(telegram_id, 'support', SUPPORT) if self.work_inbox else SUPPORT])
            from .role_content import TEXTS
            rows.append([MY_CRM if sales else TEXTS['tasks_tech']['ru'], MORE])
        else:
            rows.append([PROFILE, MORE])
        if self.support_inbox_allowed(telegram_id):
            from .work_inbox import INBOX
            rows = [[INBOX, rows[0][0]], [MORE]]
        return rows

    def more_keyboard(self, telegram_id):
        from .guidance import GUIDE
        from .navigation import HOME
        if not self.user(telegram_id):
            return self.keyboard(telegram_id)
        rows = []
        if self.live_deals_enabled and self.sales_allowed(telegram_id):
            rows.append(self.keyboard(telegram_id)[0])
        actions = []
        if self.okb_enabled and self.sales_allowed(telegram_id):
            from .okb_service import OKB
            actions.append(self.form_action_button(telegram_id, 'pharmacy', OKB))
        if self.personal_statistics_enabled and self.sales_allowed(telegram_id):
            from .personal_statistics import MY_STATS
            actions.append(MY_STATS)
        if self.support_statistics_allowed(telegram_id):
            from .work_inbox import STATS
            actions.append(STATS)
        rows.extend([actions[index:index + 2] for index in range(0, len(actions), 2)])
        return rows + [[PROFILE, GUIDE], [HOME]]

    def profile_keyboard(self, telegram_id):
        from .language_ui import CHOICES
        from .navigation import HOME
        return [list(row) for row in CHOICES] + [[HOME]]

    def handle(self, update: dict, bot=None) -> None:
        message = update.get("message", {})
        sender, chat = message.get("from", {}), message.get("chat", {})
        if chat.get("type") != "private" or sender.get("is_bot") or not sender.get("id") or chat.get("id") != sender["id"]:
            return
        user_id, chat_id = int(sender["id"]), int(chat["id"])
        self.store.remember_identity(sender)
        text = message.get("text", "")
        text = text.strip() if isinstance(text, str) else ""
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else text
        for label, action in ((tr("Подтвердить "), "/approve"), (tr("Отклонить "), "/reject"), (tr("Отключить "), "/revoke")):
            if text.startswith(label):
                text, command = action + " " + text[len(label):], action
                break
        if command in PROFILE_LABELS:
            command = PROFILE
        if command == '/approve' and ' · ' in text:
            prefix, label = text.split(' · ', 1)
            from .role_content import TEXTS
            role = next((role for role in ('fom_sales','tech','trainer','dispatcher')
                         if label == tr(TEXTS['sales' if role=='fom_sales' else role]['ru'])), None)
            text = prefix + ' ' + (role or 'invalid')
        member = self.user(user_id)
        registration_commands = {"/start", "/register", "/profile", "/registrations", "/members", "/approve", "/reject", "/revoke", "/admin", REGISTER, PROFILE, REQUESTS, MEMBERS, CANCEL}
        if bot and member and command not in registration_commands and not self.store.registration_session(user_id):
            bot.config.users[user_id] = member
            bot.handle(update)
            return
        if bot and not member:
            bot.config.users.pop(user_id, None)
        try:
            response, keyboard = self.route(user_id, chat_id, command, text)
        except RemoteError as exc:
            response, keyboard = ''.join([tr('Б24 сейчас недоступен ('), format(html.escape(exc.code), ''), tr('). Регистрация сохранена; повторите действие позже.')]), self.keyboard(user_id)
        except (ValueError, ConfigError):
            response, keyboard = tr("Укажите имя сотрудника или выберите номер из показанного списка."), [[CANCEL]]
        self.telegram.send(chat_id, response, with_guide(keyboard))

    def profile(self, user_id: int) -> str:
        row = self.store.registration(user_id)
        if not row:
            return tr("Регистрация ещё не подана. Нажмите «Зарегистрироваться».")
        statuses = {"pending": tr("Ожидает подтверждения Малика"), "approved": tr("Подтверждена"), "rejected": tr("Отклонена; можно подать заново"), "revoked": tr("Доступ отключён; обратитесь к Малику")}
        from .role_content import TEXTS
        role = self.work_inbox.store.role(user_id) if self.work_inbox else None
        role_key = 'sales' if role == 'fom_sales' else role or 'no_role'
        text = (tr(TEXTS['profile_heading']['ru']) + '\n\n' + tr(TEXTS['account']['ru']) + html.escape(row['name']) +
                ' · #' + str(row['bitrix_id']) + '\n' + tr(TEXTS['status']['ru']) + statuses[row['status']] +
                '\n' + tr(TEXTS['role']['ru']) + tr(TEXTS[role_key]['ru']))
        if user_id in self.settings.admins:
            text += '\n' + tr(TEXTS['admin']['ru'])
        from .i18n import stored_language
        from .language_ui import RU, UZ
        language = stored_language(self.store, user_id)
        text += '\n\n' + tr(TEXTS['profile_language']['ru']) + (UZ if language == 'uz' else RU)
        return text + '\n' + tr(TEXTS['profile_language_hint']['ru'])

    def route(self, user_id: int, chat_id: int, command: str, text: str):
        keyboard = self.keyboard(user_id)
        row = self.store.registration(user_id)
        from .navigation import MORE, phrase
        if command == MORE and self.user(user_id):
            return phrase('more_heading'), self.more_keyboard(user_id)
        if command=='/admin':
            if not self.user(user_id) or user_id not in self.settings.admins:
                return tr('Бошқарув администратор учун.'),keyboard
            return tr('⚙️ Бошқарув: ходимлар ва роллар.'),with_guide([[REQUESTS,MEMBERS],['/inbox_team'],['/start']])
        if command=='/start' and self.user(user_id):
            self.refresh_commands(user_id)
            return (tr('👋 Ассалому алайкум, ')+html.escape(row['name'])+tr('!\n\n'
                    'Менюдан керакли амални танланг. /next ҳозирги кейинги қадамни кўрсатади.')),keyboard
        if command == "/whoami":
            return ''.join([tr('Ваш Telegram ID: <code>'), format(user_id, ''), '</code>.']), keyboard
        if command in {"/profile", PROFILE}:
            return self.profile(user_id), self.profile_keyboard(user_id)
        if command in {"/registrations", REQUESTS, "/members", MEMBERS, "/approve", "/reject", "/revoke"}:
            if not self.user(user_id) or user_id not in self.settings.admins:
                return tr("Управление регистрациями доступно Малику."), keyboard
            return self.admin_route(user_id, command, text)
        if command in {"/cancel", CANCEL}:
            self.store.set_registration_session(user_id, None)
            return tr("Ввод регистрации отменён. ") + self.profile(user_id), keyboard
        if row and row["status"] in {"approved", "pending", "revoked"}:
            if row['status'] == 'approved':
                return menu_text(self, user_id, text=text), keyboard
            return self.profile(user_id), keyboard
        if command in {"/start", "/register", REGISTER}:
            self.store.set_registration_session(user_id, {"step": "name"})
            return tr("Введите свои имя и фамилию, как они записаны в Б24. Username и телефон не нужны."), [[CANCEL]]
        state = self.store.registration_session(user_id)
        if not state:
            return tr("Datfo Sales: самостоятельная регистрация менеджеров. Нажмите «Зарегистрироваться»."), keyboard
        if state["step"] == "name":
            if text.startswith("/") or not 3 <= len(text) <= 120:
                return tr("Введите имя и фамилию из Б24 (3–120 символов)."), [[CANCEL]]
            candidates = self.directory.search(text)
            if not candidates:
                return tr("Активный сотрудник не найден. Проверьте написание имени в Б24 и повторите ввод."), [[CANCEL]]
            if len(candidates) > 20:
                return tr("Слишком много совпадений. Введите имя и фамилию полностью."), [[CANCEL]]
            choices = "\n".join(''.join([format(index, ''), '. ', format(html.escape(candidate['name']), ''), tr(' · Б24 #'), format(candidate['id'], '')]) for index, candidate in enumerate(candidates, 1))
            if len(choices) > 3400:
                return tr("Слишком много совпадений. Введите имя и фамилию полностью."), [[CANCEL]]
            self.store.set_registration_session(user_id, {"step": "choice", "candidates": candidates})
            return choices + tr("\n\nВыберите номер своей учётной записи. Затем Малик подтвердит подключение."), [[str(index)] for index in range(1, len(candidates) + 1)] + [[CANCEL]]
        if not text.isascii() or not text.isdigit() or not 1 <= int(text) <= len(state["candidates"]):
            choices = "\n".join(''.join([format(index, ''), '. ', format(html.escape(candidate['name']), ''), tr(' · Б24 #'), format(candidate['id'], '')]) for index, candidate in enumerate(state["candidates"], 1))
            return choices + tr("\n\nВыберите номер из списка либо отмените регистрацию."), [[str(index)] for index in range(1, len(state["candidates"]) + 1)] + [[CANCEL]]
        employee = state["candidates"][int(text) - 1]
        self.store.request_registration(user_id, chat_id, employee)
        self.store.set_registration_session(user_id, None)
        return self.profile(user_id) + tr("\nПосле подтверждения ваши запросы будут связаны с этой учётной записью."), self.keyboard(user_id)

    def admin_route(self, user_id: int, command: str, text: str):
        keyboard = self.keyboard(user_id)
        if command in {"/registrations", REQUESTS, "/members", MEMBERS}:
            pending = command in {"/registrations", REQUESTS}
            rows = self.store.registrations("pending" if pending else "approved")
            if not rows:
                return tr("Новых заявок нет.") if pending else tr("Подключённых менеджеров нет."), keyboard
            lines = [tr("Заявки на подключение:") if pending else tr("Подключённые сотрудники:")]
            actions = []
            # Telegram messages are bounded; a numeric cursor allows the rest of a large team to be viewed.
            parts = text.split()
            start = int(parts[1]) if command in {"/registrations", "/members"} and len(parts) == 2 and parts[1].isdigit() else 0
            shown = 0
            for row in rows[start:start + 10]:
                identity = self.store.db.execute("SELECT value FROM settings WHERE key=?", ("identity:" + str(row['telegram_id']),)).fetchone()
                identity = json.loads(identity[0]) if identity else {}
                telegram_name = " ".join(str(identity.get(key) or "") for key in ("first_name", "last_name")).strip()
                entry = ''.join(['\n', format(html.escape(row['name'][:120]), ''), tr(' · Б24 #'), format(row['bitrix_id'], ''), '\nTelegram: ', format(html.escape(telegram_name[:128]), ''), ' · <code>', format(row['telegram_id'], ''), '</code>'])
                if pending:
                    entry += ''.join([tr('\nПодтвердить: /approve '), format(row['telegram_id'], ''), tr('\nОтклонить: /reject '), format(row['telegram_id'], '')])
                elif row['telegram_id'] not in self.settings.admins:
                    entry += ''.join([tr('\nОтключить: /revoke '), format(row['telegram_id'], '')])
                if sum(len(line) for line in lines) + len(entry) > 3400:
                    break
                lines.append(entry)
                shown += 1
                if pending:
                    actions.append([''.join([tr('Подтвердить '), format(row['telegram_id'], '')]), ''.join([tr('Отклонить '), format(row['telegram_id'], '')])])
                elif row['telegram_id'] not in self.settings.admins:
                    actions.append([''.join([tr('Отключить '), format(row['telegram_id'], '')])])
            if start + shown < len(rows):
                next_page = f"{'/registrations' if pending else '/members'} {start + shown}"
                lines.append(tr("\nСледующие: ") + next_page)
                actions.append([next_page])
            return "\n".join(lines), actions + keyboard
        parts = text.split()
        if len(parts) not in ({2,3} if command=='/approve' else {2}) or not parts[1].isascii() or not parts[1].isdigit():
            return tr("Выберите заявку через /registrations или сотрудника через /members и используйте указанную команду."), keyboard
        target_id = int(parts[1])
        if target_id in self.settings.admins:
            return tr("Администратор задан в настройках; его привязку нельзя изменить этой командой."), keyboard
        row = self.store.registration(target_id)
        if not row:
            return tr("Такой регистрации нет."), keyboard
        if command == "/approve":
            if row["status"] == "approved":
                return tr("Регистрация уже подтверждена."), keyboard
            if row["status"] != "pending":
                return tr("Подтвердить можно только ожидающую заявку."), keyboard
            role = None
            if self.work_inbox:
                from .role_content import TEXTS
                from .work_inbox import ROLES
                if len(parts)==2:
                    choices = [[tr('Подтвердить ') + str(target_id) + ' · ' +
                                tr(TEXTS['sales' if role=='fom_sales' else role]['ru'])] for role in ROLES]
                    return tr(TEXTS['choose_role']['ru']), choices + keyboard
                role = parts[2]
                if role not in ROLES:
                    return tr(TEXTS['choose_role']['ru']), keyboard
            employee = self.directory.employee(row["bitrix_id"])
            if not employee:
                return tr("Этот сотрудник больше не активен в Б24. Доступ не выдан."), keyboard
            if not self.store.approve_registration(target_id, user_id, employee, work_role=role):
                return tr("Учётная запись Б24 уже привязана к другому Telegram ID. Сначала проверьте существующую привязку."), keyboard
            self.refresh_commands(target_id)
            if role:
                return tr(TEXTS['approved']['ru']).format(role=tr(TEXTS['sales' if role=='fom_sales' else role]['ru'])), keyboard
            return tr('Подключён ') + html.escape(employee['name']) + '. /start · ' + tr(PROFILE), keyboard
        status = "rejected" if command == "/reject" else "revoked"
        changed = self.store.deny_registration(target_id, user_id, status)
        return (tr("Заявка отклонена.") if status == "rejected" else tr("Доступ отключён.")) if changed else tr("Регистрация уже обработана; проверьте текущий список."), keyboard
