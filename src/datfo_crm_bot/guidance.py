"""Read-only bilingual help. Opening help never advances a form."""
from __future__ import annotations

import html

from .help_content import HELP_TEXTS, HELP_LABELS
from .i18n import tr

GUIDE = "📖 Ёрдам"
GUIDE_LABELS = {GUIDE, "📖 Йўриқнома"}


def with_guide(keyboard):
    if keyboard is None:
        return None
    rows = [list(row) for row in keyboard]
    from .navigation import MORE
    if any(label in row for row in rows for label in (MORE, tr(MORE, 'uz'))):
        return rows
    if not any(GUIDE in row for row in rows):
        rows.append([GUIDE])
    return rows


TOPICS = {key: text['uz'] for key, text in HELP_TEXTS.items()}
# Previously sent callbacks remain usable.
TOPICS['inbox'] = TOPICS['work_inbox']
TOPICS['distribute'] = TOPICS['tech']


def topic_keyboard(topic='home'):
    topic = {'inbox': 'work_inbox', 'distribute': 'tech'}.get(topic, topic)
    layout = {
        'home': [['sales', 'tech'], ['register', 'rules'], ['next']],
        'sales': [['deal', 'okb'], ['work_inbox'], ['crm', 'statistics'], ['next', 'home']],
        'tech': [['crm'], ['next', 'home']],
        'register': [['rules'], ['next', 'home']],
        'rules': [['register'], ['next', 'home']],
        'crm': [['sales', 'tech'], ['next', 'home']],
        'next': [['home']],
    }.get(topic)
    if layout is None:
        layout = [['sales'], ['next', 'home']] if topic in TOPICS else [['sales', 'tech'], ['register', 'rules'], ['next']]
    return [[{'text': HELP_LABELS[key]['uz'], 'callback_data': 'guide:' + key} for key in row] for row in layout]


TOPIC_BUTTONS = topic_keyboard()



FORM_HINTS = {
    "live_category": "Б24да ёзув тушадиган воронка рақамини танланг.",
    "live_initial_stage": "Б24 ёзувининг бошланғич босқичини рўйхатдан танланг.",
    "okb_inn": "Компания ИННини рақамлар билан киритинг.",
    "okb_company": "Топилган компаниялар рўйхатидан ўз компаниянгиз рақамини танланг.",
    "okb_company_name": "Компания топилмади. Фирма номини киритинг, масалан: Zarafshon Pharm MChJ.",
    "okb_title": "Дорихона нуқтасининг номини киритинг.",
    "okb_phone": "Телефонни киритинг ёки «Без телефона»ни босинг. +998 шарт эмас.",
    "okb_contact": "Рўйхатдан керакли контакт рақамини танланг.",
    "okb_contact_create": "Янги контакт яратиш ёки контактсиз давом этишни танланг.",
    "okb_contact_phone": "Янги контакт рақамини ёзинг ёки кўрсатилган рақамни тасдиқланг.",
    "okb_contact_name": "Контакт исмини ёзинг. Исм ва фамилияни киритиш ҳам мумкин.",
    "okb_contact_position": "Б24 «Должность список» рўйхатидан контакт лавозимини танланг.",
    "okb_region": "Рўйхатдан дорихонанинг бизнес-ҳудудини танланг.",
    "okb_city_search": "Шаҳар ёки туман номини ёзинг.",
    "okb_city_choice": "Танланган бизнес-ҳудуддаги шаҳар ёки туман тугмасини босинг. Номини ёзиб қидириш ҳам мумкин.",
    "okb_address": "Дорихона манзилини ёзинг: кўча ва уй рақами.",
    "okb_landmark": "Мўлжални ёзинг ёки «Без ориентира»ни босинг.",
    "okb_program": "💻 Ҳозир ишлатилаётган дастурни рўйхатдан танланг. Танлаш мажбурий.",
    "confirm": "Якуний карточкани текширинг. Тўғри бўлса, «Подтвердить»ни босинг.",
    "title": "Қисқа ном ёки сделка мавзусини ёзинг.",
    "inn": "Компания ИННини киритинг ёки «Без ИНН»ни босинг.",
    "company": "Рўйхатдан компанияни танланг ёки ИННни ўзгартиринг.",
    "address": "Дорихонанинг манзилини ёзинг.",
    "description": "Нима қилиш кераклигини батафсил ёзинг.",
    "phone": "Телефонни киритинг ёки «Без телефона»ни босинг. +998 шарт эмас.",
    "sales_pharmacy": "Введите название аптеки для поиска в базе.",
    "sales_match": "Выберите найденную аптеку или подтвердите добавление новой.",
    "sales_pharmacy_confirm": "Проверьте название и адрес. Подтвердите создание сделки на эту аптеку.",
    "sales_address": "Укажите физический адрес аптеки или используйте найденный адрес.",
    "sales_description": "Опишите предмет сделки; следующий шаг спросим отдельно.",
    "sales_next_step": "Напишите действие для ответственного менеджера — оно станет делом в Б24.",
    "sales_deadline": "Укажите дату и при желании время следующего шага.",
    "sales_phone": "Введите телефон: знак + и код страны необязательны.",
    "stats_scope": "Ўзингиз ёки жамоа статистикасини танланг.",
    "stats_period": "Статистика учун даврни танланг.",
}


def form_hint(state):
    if state and state.get('step') == 'sales_okb':
        state = state['pharmacy_form']
    from .intake_ui import phrase
    prompts = {'okb_contact_name': 'name', 'okb_region': 'region', 'okb_city_choice': 'city',
               'okb_location_choice': 'location_choice', 'okb_location': 'location_send',
               'sales_reminder': 'reminder_ask', 'sales_next_step': 'reminder_action',
               'sales_deadline': 'reminder_date', 'sales_time': 'reminder_time',
               'sales_bulk_input': 'template_help', 'okb_bulk_input': 'template_help'}
    if state and state.get('step') in prompts:
        return phrase(prompts[state['step']])
    return tr(FORM_HINTS.get(state.get("step"), "")) or None if state else None


class Guide:
    def __init__(self, registration, telegram, inbox=None, simulation=None, work_inbox=None):
        self.registration, self.telegram = registration, telegram
        self.inbox, self.simulation = inbox, simulation
        self.work_inbox = work_inbox

    def next_step(self, user):
        member = self.registration.user(user)
        if member and member.admin and self.inbox:
            mode = self.inbox.store.mode(user)
            if mode:
                from .communications import draft_prompt, PROFILES_UZ
                draft = mode["draft"]
                hint = draft_prompt(draft)[0] if draft else tr("«Янги сделка»ни яратинг ёки «Кирувчи сделкалар»ни очинг.")
                return tr("🧪 <b>Инбокс синови</b>\nРол: ") + PROFILES_UZ[mode["role"]] + "\n\n" + hint
        if member and member.admin and self.simulation and self.simulation.active(user):
            state = self.simulation.store.session(user)
            return tr("🧪 <b>Синов — Б24га ёзилмайди</b>\n\n") + self._form_step(state)
        if member and self.work_inbox and self.work_inbox.store.role(user) and not self.registration.sales_allowed(user):
            return tr(HELP_TEXTS['tech']['ru'])
        if member and self.work_inbox and self.work_inbox.store.role(user):
            draft = self.work_inbox.store.session(user)
            if draft:
                hints = {'deal':tr('🔗 Б24 сделкасини рўйхатдан танланг.'),
                         'recipient':tr('📥 Выберите «На распределение» или «Назначить технику». Техника можно найти по имени или фамилии. Контактное лицо аптеки указывается в форме.'),
                         'title':tr('📝 Заявка мавзусини ёзинг: 3–100 белги.'),
                         'description':tr('💬 Муаммо ва керакли ёрдамни ёзинг: 5–1200 белги.'),
                         'confirm':tr('👀 Текшириб, «Заявкани юбориш»ни босинг.')}
                return tr('▶️ <b>Техник ёрдам заявкаси — кейинги қадам</b>\n')+hints[draft['step']]
        registration_state = self.registration.store.registration_session(user)
        if registration_state:
            if registration_state["step"] == "name":
                return tr("👋 Б24даги исм-фамилиянгизни ёзинг.")
            return tr("👋 Ўзингизни рўйхатдан танланг:\n") + "\n".join(
                f"{number}. {html.escape(row['name'])}" for number, row in enumerate(registration_state.get("candidates", []), 1))
        if not member:
            row = self.registration.store.registration(user)
            if row and row["status"] == "pending":
                return tr("⏳ Рўйхатдан ўтиш сўрови юборилган. Малик тасдиқлашини кутинг; /profile орқали текширинг.")
            if row and row["status"] == "revoked":
                return tr("🔒 Кириш ҳуқуқи ўчирилган. Маликка мурожаат қилинг.")
            return tr("👋 Аввал /register орқали рўйхатдан ўтинг.")
        if self.registration.store.unfinished(user):
            return tr("⏳ Олдинги сақлаш ҳали тасдиқланмаган. /pending орқали натижани текширинг.")
        state = self.registration.store.session(user)
        if form_hint(state):
            return self._form_step(state)
        test_mode = self.registration.store.db.execute('SELECT value FROM settings WHERE key=?',('b24-test:'+str(user),)).fetchone()
        if test_mode and test_mode[0]=='1':
            return tr('🧪 <b>Ҳақиқий Б24 синови</b>\n\n/deal ёки /support ни танланг. Воронка ва босқични танлаб, формани тўлдиринг. /my_crm — сақланган ёзувлар.')
        if self.registration.live_deals_enabled:
            return tr('📌 /deal — сделка, /support — техник ёрдам заявкаси, /my_crm — сделкалар ва заявкалар.\n📥 /requests — ФОМ инбокси.\n🏪 /okb — дорихона қўшиш.')
        return self._form_step(state)

    @staticmethod
    def _form_step(state):
        if state and state.get('step') == 'sales_okb':
            state = state['pharmacy_form']
        hint = form_hint(state)
        if not hint:
            return tr("🏪 Дорихона қўшиш учун «Добавить аптеку в ОКБ»ни босинг. /profile — рўйхатдан ўтиш ҳолати.")
        fields = {"okb_company": "candidates", "company": "candidates", "okb_contact": "contact_candidates",
                  "okb_contact_position": "contact_position_candidates",
                  "okb_region": "region_candidates", "okb_city_choice": "city_candidates",
                  "okb_program":"program_candidates",
                  "live_category":"category_candidates", "live_initial_stage":"stage_candidates"}
        choices = state.get(fields.get(state["step"], ""), [])
        if choices:
            hint += "\n\n" + "\n".join(f"{number}. {html.escape(str(row['title']))}" for number, row in enumerate(choices, 1))
        return tr("▶️ <b>Кейинги қадам</b>\n") + hint

    def handle(self, update):
        callback = update.get("callback_query")
        message = callback.get("message", {}) if isinstance(callback, dict) else update.get("message", {})
        sender = callback.get("from", {}) if isinstance(callback, dict) else message.get("from", {})
        chat = message.get("chat", {})
        if chat.get("type") != "private" or sender.get("is_bot") or not sender.get("id") or sender["id"] != chat.get("id"):
            return False
        if callback:
            data = str(callback.get("data", ""))
            if not data.startswith("guide:"):
                return False
            topic = data.partition(":")[2]
        else:
            text = message.get("text", "")
            text = text.strip() if isinstance(text, str) else ""
            command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else text
            if command not in {*GUIDE_LABELS, "/guide", "/help", "/next"}:
                return False
            topic = "next" if command == "/next" else "home"
        user = int(sender["id"])
        if callback:
            from .api import RemoteError
            try:
                self.telegram.call("answerCallbackQuery", {"callback_query_id": callback.get("id", "")})
            except RemoteError as exc:
                if exc.code == "TELEGRAM_401":
                    raise
        text = self.next_step(user) if topic == "next" else tr(TOPICS.get(topic, TOPICS["home"]))
        self.telegram.call("sendMessage", {"chat_id": user, "text": text, "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": topic_keyboard(topic)}, "link_preview_options": {"is_disabled": True}})
        return True
