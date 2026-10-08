from .i18n import tr
import json
from .api import RemoteError
from .config import ConfigError
from .okb_service import OKB, OKB_LABELS, CONTINUE, OkbBot
from .service import CANCEL, CHECK, MENU, PHARMACY, STATS, DEAL_LABELS, SUPPORT_LABELS
from .guidance import form_hint, with_guide


class OkbTelegram:
    def __init__(self, registration, telegram):
        self.registration, self.telegram = registration, telegram

    def send(self, chat_id, text, keyboard=None):
        if keyboard == MENU:
            keyboard = self.registration.keyboard(chat_id)
        state = self.registration.store.session(chat_id)
        hint = (tr("Сақлаш ҳали тасдиқланмаган. /pending орқали натижани текширинг.")
                if self.registration.store.unfinished(chat_id) else form_hint(state))
        if hint and not text.startswith(tr('📍 Сейчас: ')):
            text += tr("\n\n▶️ <b>Кейинги қадам:</b> ") + hint
        self.telegram.send(chat_id, text, with_guide(keyboard))


class OkbAccess:
    """Expose only the approved OKB workflow; unconfigured deal/support routes stay closed."""
    def __init__(self, registration, config, crm, telegram):
        self.registration, self.telegram = registration, telegram
        self.bot = OkbBot(config, registration.store, crm, OkbTelegram(registration, telegram))
        self.bot.registration = registration
        registration.okb_enabled = True

    def handle(self, update):
        message = update.get("message", {})
        sender, chat = message.get("from", {}), message.get("chat", {})
        if chat.get("type") != "private" or sender.get("is_bot") or not sender.get("id") or chat.get("id") != sender["id"]:
            return False
        user_id = int(sender["id"])
        text = message.get("text", "")
        text = text.strip() if isinstance(text, str) else ""
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else text
        state = self.registration.store.session(user_id)
        web_pharmacy = None
        web_data = message.get('web_app_data')
        if isinstance(web_data, dict) and isinstance(web_data.get('data'), str) and len(web_data['data'].encode('utf-8')) <= 4096:
            try:
                parsed = json.loads(web_data['data'])
                if isinstance(parsed, dict) and parsed.get('mode') == 'pharmacy':
                    web_pharmacy = parsed
            except ValueError:
                pass
        commands = {"/okb", "/pharmacy", *OKB_LABELS, PHARMACY, "/stats", STATS}
        if any(self.registration.store.operation(row['request_id'])['value'].get('workflow') == 'okb'
               for row in self.registration.store.unfinished(user_id)):
            commands.update({'/pending', CHECK, CONTINUE})
        from .registration import PROFILE_LABELS
        admin_commands = {"/register", "/profile", "/registrations", "/members", "/approve", "/reject", "/revoke", *PROFILE_LABELS, "Заявки менеджеров", "Подключённые менеджеры"}
        if command in admin_commands:
            return False
        if command not in commands and not state and web_pharmacy is None:
            return False
        user = self.registration.user(user_id)
        if not user:
            self.bot.config.users.pop(user_id, None)
            self.telegram.send(user_id, tr("Сначала зарегистрируйтесь и получите подтверждение Малика."), self.registration.keyboard(user_id))
            return True
        if not self.registration.sales_allowed(user_id):
            if command in commands or message.get('web_app_data'):
                from .role_content import TEXTS
                self.telegram.send(user_id,tr(TEXTS['sales_only']['ru']),self.registration.keyboard(user_id))
                return True
            return False
        if command in {"/deal", "/support", *DEAL_LABELS, *SUPPORT_LABELS}:
            self.telegram.send(user_id, tr("Продажи и поддержка ожидают выбора воронок. Доступно добавление аптеки в ОКБ."), [[OKB]])
            return True
        self.bot.config.users[user_id] = user
        if web_pharmacy is not None:
            from .input_forms import launch_matches, consume_launch
            from .navigation import phrase
            fresh = not state or state.get('workflow') != 'okb' or state.get('request_id') != web_pharmacy.get('token')
            if fresh:
                work = self.registration.work_inbox
                if (self.registration.store.unfinished(user_id) or
                        (state and state.get('step') not in {'done', 'stats_scope', 'stats_period'}) or
                        (work and work.store.session(user_id))):
                    self.telegram.send(user_id, phrase('active'), self.registration.keyboard(user_id))
                    return True
                if not launch_matches(self.registration.store, user_id, 'pharmacy', web_pharmacy.get('token')):
                    self.telegram.send(user_id, tr('Эта форма уже закрыта. Откройте новую форму через меню.'), self.registration.more_keyboard(user_id))
                    return True
                self.telegram.send(user_id, phrase('pharmacy_processing'))
                try:
                    self.bot.crm.validate_okb()
                except ConfigError:
                    self.telegram.send(user_id, tr('Этот процесс ещё не настроен в Б24. Сообщите администратору.'), self.registration.more_keyboard(user_id))
                    return True
                except RemoteError:
                    self.telegram.send(user_id, tr('Б24 сейчас недоступен. Откройте форму ещё раз.'), self.registration.more_keyboard(user_id))
                    return True
                state = {'kind': 'pharmacy', 'workflow': 'okb', 'step': 'okb_bulk_input', 'request_id': web_pharmacy['token']}
                self.registration.store.set_session(user_id, state)
                consume_launch(self.registration.store, user_id, 'pharmacy', web_pharmacy['token'])
        self.bot.handle(update)
        return True
