"""Two-persona inbox prototype. Storage and routing have no Bitrix dependency."""
from __future__ import annotations

from contextlib import contextmanager
import html
import json
from pathlib import Path
import re
import sqlite3
import uuid

from .api import RemoteError
from .config import ROOT
from .storage import process_lock
from .guidance import GUIDE, with_guide

TEST_INBOX = "🧪 Тест инбокса"
SALES = "👤 Сотув менежери"
TECH = "🛠 Техник"
INBOX = "📥 Кирувчи сделкалар"
NEW = "📝 Янги сделка"
MINE = "📌 Менинг сделкаларим"
EXIT = "🏁 Инбокс синовидан чиқиш"
SEND = "✅ Юбориш"
CANCEL_REQUEST = "❌ Бекор қилиш"
MENU = with_guide([[NEW, INBOX], [MINE], [SALES, TECH], [EXIT]])
PROFILES = {"sales": "Тестовый менеджер продаж", "tech": "Тестовый техник"}
PROFILES_UZ = {"sales": "Сотув менежери (синов)", "tech": "Техник (синов)"}
QUEUES = {"general": "Общая", "sales": "Продажи", "tech": "Техническая"}
QUEUES_UZ = {"general": "Умумий", "sales": "Сотув", "tech": "Техник ёрдам"}
STATUSES = {"new": "Новая", "triage": "На распределении", "assigned": "Назначена, ожидает принятия", "working": "В работе", "closed": "Закрыта"}
STATUSES_UZ = {"new": "Янги", "triage": "Тақсимланмоқда", "assigned": "Тайинланган, қабул қилиш кутилмоқда", "working": "Ишланмоқда", "closed": "Якунланган"}
QUEUE_LABELS = {label: role for labels in (QUEUES, QUEUES_UZ) for role, label in labels.items()}
OLD_LABELS = {"👤 Менеджер продаж": SALES, "👤 Техник": TECH, "📥 Инбокс": INBOX,
              "📝 Новая заявка": NEW, "📌 Мои заявки": MINE, "Завершить тест инбокса": EXIT,
              "Отправить заявку": SEND, "Отмена заявки": CANCEL_REQUEST}
OLD_LABELS.update({'📥 Кирувчи сўровлар':INBOX,'📝 Янги сўров':NEW,'📌 Менинг сўровларим':MINE})
NOTICE = "<b>🧪 ИНБОКС СИНОВИ — Б24га ёзилмайди</b>\n"


def draft_prompt(draft):
    step = draft['step']
    if step == 'queue':
        return ('1/4 · 🧭 Навбатни танланг. «Умумий»ни иккала рол ҳам кўради.',
                with_guide([list(QUEUES_UZ.values()), [CANCEL_REQUEST]]))
    if step == 'title':
        return ('2/4 · 📝 Қисқа сарлавҳа ёзинг: 3–100 белги.\nМасалан: «Дорихонада дастур очилмаяпти».',
                with_guide([[CANCEL_REQUEST]]))
    if step == 'description':
        return ('3/4 · 💬 Нима бўлганини ва қандай ёрдам кераклигини ёзинг: 5–1200 белги.\nДорихона номи, манзили ва боғланиш рақамини қўшсангиз, ижрочига осонроқ бўлади.',
                with_guide([[CANCEL_REQUEST]]))
    return ('4/4 · 👀 Маълумотларни текширинг. Тўғри бўлса, «Юбориш»ни босинг.',
            with_guide([[SEND], [CANCEL_REQUEST]]))


def ticket_hint(row, actor):
    if row['status'] == 'closed':
        return '✅ Сделка якунланган. Янги масала учун «Янги сделка»ни танланг.'
    if row['owner'] and row['owner'] != actor:
        return '👤 Масъул тайинланган. Ҳолатни «Кирувчи сделкалар»дан кузатинг; унинг сделкасини олиб қўйиб бўлмайди.'
    if row['owner'] == actor:
        return {
            'triage': '▶️ Ижрочини танланг ёки ўзингиз бажариш учун «Ўзимга олиш»ни босинг.',
            'assigned': '▶️ Бажаришни бошлаш учун «Ишга қабул қилиш»ни босинг.',
            'working': '▶️ Иш тугагач, «Якунлаш»ни босинг. Ҳозир бажара олмасангиз, навбатга қайтаринг.',
        }[row['status']]
    if row['queue'] in {'general', actor}:
        return '▶️ Ўзингиз бажарсангиз, «Ўзимга олиш»ни босинг. Ижрочи танлаш учун «Тақсимлашга қабул қилиш»ни босинг.'
    return '⏳ Бу бошқа навбатга юборилган сделка. Ижрочи қабул қилишини кутинг.'


class InboxError(ValueError):
    pass


def person_label(person: dict, *, markup=True) -> str:
    username = str(person.get("username") or "")
    if re.fullmatch(r"[A-Za-z0-9_]{1,32}", username):
        return "@" + username
    name = str(person.get("name") or "Участник")
    telegram_id = person.get("telegram_id")
    if markup and isinstance(telegram_id, int) and telegram_id > 0:
        return f'<a href="tg://user?id={telegram_id}">{html.escape(name)}</a>'
    return html.escape(name) if markup else name


class InboxStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS inbox_profiles(
                team INTEGER NOT NULL, role TEXT NOT NULL, name TEXT NOT NULL,
                PRIMARY KEY(team,role));
            CREATE TABLE IF NOT EXISTS inbox_modes(
                user_id INTEGER PRIMARY KEY, role TEXT NOT NULL, draft TEXT);
            CREATE TABLE IF NOT EXISTS inbox_tickets(
                id INTEGER PRIMARY KEY AUTOINCREMENT, team INTEGER NOT NULL,
                request_id TEXT NOT NULL UNIQUE, creator TEXT NOT NULL,
                queue TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new', owner TEXT,
                version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS inbox_team ON inbox_tickets(team,status,id);
            CREATE TABLE IF NOT EXISTS inbox_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ticket INTEGER NOT NULL,
                version INTEGER NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
                at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(ticket,version));
            CREATE TABLE IF NOT EXISTS inbox_outbox(
                event INTEGER NOT NULL, team INTEGER NOT NULL, recipient TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', error TEXT,
                PRIMARY KEY(event,recipient));
            CREATE TABLE IF NOT EXISTS inbox_replies(
                update_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
                outputs TEXT NOT NULL DEFAULT '[]');
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def begin_reply(self, update_id, user):
        with self.db:
            changed = self.db.execute('INSERT OR IGNORE INTO inbox_replies(update_id,user_id) VALUES(?,?)', (update_id,user)).rowcount
        row = self.db.execute('SELECT user_id,outputs FROM inbox_replies WHERE update_id=?', (update_id,)).fetchone()
        if row['user_id'] != user:
            raise InboxError('Повторный запрос принадлежит другому пользователю')
        return None if changed else json.loads(row['outputs'])

    def cache_output(self, update_id, output):
        with self.db:
            row = self.db.execute('SELECT outputs FROM inbox_replies WHERE update_id=?',(update_id,)).fetchone()
            outputs = json.loads(row['outputs'])
            outputs.append(output)
            self.db.execute('UPDATE inbox_replies SET outputs=? WHERE update_id=?',(json.dumps(outputs,ensure_ascii=False),update_id))

    def prune_updates(self, offset):
        with self.db:
            self.db.execute('DELETE FROM inbox_replies WHERE update_id<?',(offset-1000,))

    def bootstrap(self, team):
        with self.db:
            for role, name in PROFILES.items():
                self.db.execute('INSERT OR IGNORE INTO inbox_profiles VALUES(?,?,?)', (team, role, name))

    def mode(self, user):
        row = self.db.execute('SELECT * FROM inbox_modes WHERE user_id=?', (user,)).fetchone()
        if not row:
            return None
        return {**dict(row), 'draft':json.loads(row['draft']) if row['draft'] else None}

    def set_mode(self, user, role, draft=None):
        if role not in PROFILES:
            raise InboxError('Неизвестная роль')
        with self.db:
            self.db.execute('INSERT INTO inbox_modes VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET role=excluded.role,draft=excluded.draft',
                            (user, role, json.dumps(draft, ensure_ascii=False) if draft else None))

    def exit(self, user):
        with self.db:
            self.db.execute('DELETE FROM inbox_modes WHERE user_id=?', (user,))

    def ticket(self, team, ident):
        row = self.db.execute('SELECT * FROM inbox_tickets WHERE team=? AND id=?', (team, ident)).fetchone()
        if not row:
            raise InboxError('Заявка не найдена в вашем инбоксе')
        return dict(row)

    @staticmethod
    def visible(ticket, role):
        return ticket['queue'] in {'general', role} or role in {ticket['creator'], ticket['owner']}

    def listing(self, team, role, mine=False):
        sql = "SELECT * FROM inbox_tickets WHERE team=? AND (queue IN ('general',?) OR creator=? OR owner=?)"
        arguments = [team,role,role,role]
        if mine:
            sql += ' AND owner=?'
            arguments.append(role)
        return [dict(row) for row in self.db.execute(sql + ' ORDER BY id DESC LIMIT 20', arguments)]

    def _event(self, ticket, actor, action, recipients):
        event = self.db.execute('INSERT INTO inbox_events(ticket,version,actor,action) VALUES(?,?,?,?)',
                                (ticket['id'], ticket['version'], actor, action)).lastrowid
        for role in set(recipients) - {actor, None}:
            self.db.execute('INSERT INTO inbox_outbox(event,team,recipient) VALUES(?,?,?)', (event, ticket['team'], role))

    def create(self, team, actor, queue, title, description, request_id):
        if actor not in PROFILES or queue not in QUEUES or not 3 <= len(title) <= 100 or not 5 <= len(description) <= 1200:
            raise InboxError('Название: 3–100 символов; описание: 5–1200 символов; выберите очередь')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            existing = self.db.execute('SELECT * FROM inbox_tickets WHERE request_id=?', (request_id,)).fetchone()
            if existing:
                if existing['team'] != team or existing['creator'] != actor:
                    raise InboxError('Номер запроса принадлежит другому участнику')
                result = dict(existing)
            else:
                ident = self.db.execute('INSERT INTO inbox_tickets(team,request_id,creator,queue,title,description) VALUES(?,?,?,?,?,?)',
                                        (team, request_id, actor, queue, title, description)).lastrowid
                result = self.ticket(team, ident)
                recipients = PROFILES if queue == 'general' else [queue]
                self._event(result, actor, 'created', recipients)
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            raise

    def change(self, team, actor, ident, action, version, target=None):
        if actor not in PROFILES:
            raise InboxError('Неизвестный участник')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.ticket(team, ident)
            if not self.visible(row, actor):
                raise InboxError('Заявка недоступна вашей роли')
            owner = row['owner']
            # Report the actual owner even when a second contender presses an old card.
            if action in {'take', 'triage'} and owner and owner != actor:
                prefix = 'Заявка уже назначена: ' if row['status'] == 'assigned' else 'Заявку уже взял '
                message = prefix + PROFILES[owner]
                result = {'changed':False, 'message':message, 'ticket':row, 'busy_owner':owner, 'busy_prefix':prefix}
            elif action == 'take' and owner == actor and row['status'] == 'working':
                result = {'changed':False, 'message':'Заявка уже у вас в работе', 'ticket':row}
            else:
                if row['version'] != version:
                    raise InboxError('Карточка изменилась. Откройте её заново через инбокс')
                if row['status'] == 'closed':
                    raise InboxError('Заявка уже закрыта')
                next_owner, next_status, next_queue = owner, row['status'], row['queue']
                if action in {'take', 'triage'}:
                    if row['queue'] not in {'general', actor}:
                        raise InboxError('Эту очередь обрабатывает другая роль')
                    if owner is None and row['status'] == 'new':
                        next_owner, next_status = actor, 'working' if action == 'take' else 'triage'
                    elif action == 'take' and owner == actor and row['status'] in {'assigned', 'triage'}:
                        next_status = 'working'
                    else:
                        raise InboxError('Этот шаг уже выполнен. Обновите карточку')
                elif action == 'assign':
                    if owner != actor or row['status'] != 'triage' or target not in PROFILES:
                        raise InboxError('Назначать можно свою заявку, принятую на распределение')
                    next_owner, next_status, next_queue = target, 'assigned', target
                elif action == 'close':
                    if owner != actor or row['status'] != 'working':
                        raise InboxError('Закрыть заявку может её исполнитель после принятия в работу')
                    next_status = 'closed'
                elif action == 'release':
                    if owner != actor:
                        raise InboxError('Вернуть в очередь можно только свою заявку')
                    next_owner, next_status = None, 'new'
                else:
                    raise InboxError('Неизвестное действие')
                changed = self.db.execute('''UPDATE inbox_tickets SET owner=?,status=?,queue=?,version=version+1,
                    updated_at=CURRENT_TIMESTAMP WHERE id=? AND team=? AND version=?''',
                    (next_owner, next_status, next_queue, ident, team, version)).rowcount
                if changed != 1:
                    raise InboxError('Заявку изменил другой участник. Обновите инбокс')
                updated = self.ticket(team, ident)
                recipients = {row['creator'], next_owner}
                if action == 'release':
                    recipients.update(PROFILES if next_queue == 'general' else [next_queue])
                self._event(updated, actor, action, recipients)
                result = {'changed':True, 'message':'Сохранено: ' + STATUSES[next_status], 'ticket':updated}
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            raise


class InboxTest:
    def __init__(self, registration, store, telegram, simulation=None):
        self.registration, self.store, self.telegram, self.simulation = registration, store, telegram, simulation
        self.reply_update = None
        primary = Path(registration.store.db.execute('PRAGMA database_list').fetchone()[2]).resolve()
        if primary == store.path.resolve():
            raise RuntimeError('Инбокс требует отдельную учебную базу')

    def say(self, user, text, keyboard=MENU):
        keyboard = with_guide(keyboard)
        if self.reply_update is not None:
            self.store.cache_output(self.reply_update,{'kind':'text','text':text,'keyboard':keyboard})
        self.telegram.send(user, NOTICE + text, keyboard)

    def profile(self, role):
        return {'name':PROFILES_UZ[role]}

    def card(self, user, row, actor, heading='', *, cache=True):
        if not self.store.visible(row, actor):
            raise InboxError('Заявка недоступна вашей роли')
        if cache and self.reply_update is not None:
            self.store.cache_output(self.reply_update,{'kind':'card','id':row['id'],'actor':actor,'heading':heading})
        owner = person_label(self.profile(row['owner'])) if row['owner'] else 'Ҳали тайинланмаган'
        text = (NOTICE + f'<b>Рол: {PROFILES_UZ[actor]}</b>\n' + (html.escape(heading) + '\n' if heading else '')
                + f"<b>Сделка #{row['id']}: {html.escape(row['title'])}</b>\n"
                + f"Навбат: {QUEUES_UZ[row['queue']]}\nҲолат: {STATUSES_UZ[row['status']]}\n"
                + f"Муаллиф: {PROFILES_UZ[row['creator']]}\nМасъул: {owner}\n\n"
                + html.escape(row['description']) + '\n\n' + ticket_hint(row, actor))
        def button(label, action, target='-'):
            return {'text':label, 'callback_data':f"ib:{actor}:{row['id']}:{row['version']}:{action}:{target}"}
        buttons = []
        if row['status'] == 'new' and row['queue'] in {'general', actor}:
            buttons += [[button('🙋 Ўзимга олиш', 'take')], [button('🧭 Тақсимлашга қабул қилиш', 'triage')]]
        elif row['owner'] == actor and row['status'] == 'triage':
            buttons += [[button('👤 Менежерга бериш', 'assign', 'sales'), button('🛠 Техникка бериш', 'assign', 'tech')], [button('🙋 Ўзимга олиш', 'take')]]
        elif row['owner'] == actor and row['status'] == 'assigned':
            buttons += [[button('▶️ Ишга қабул қилиш', 'take')]]
        elif row['owner'] == actor and row['status'] == 'working':
            buttons += [[button('✅ Якунлаш', 'close')]]
        elif row['owner'] and row['owner'] != actor and row['status'] != 'closed':
            buttons += [[button('👤 Ким олганини текшириш', 'take')]]
        if row['owner'] == actor and row['status'] != 'closed':
            buttons += [[button('↩️ Навбатга қайтариш', 'release')]]
        buttons.append([{'text': GUIDE, 'callback_data':'guide:inbox'}])
        payload = {'chat_id':user, 'text':text, 'parse_mode':'HTML', 'link_preview_options':{'is_disabled':True}}
        if buttons:
            payload['reply_markup'] = {'inline_keyboard':buttons}
        self.telegram.call('sendMessage', payload)

    def flush(self):
        pending = list(self.store.db.execute('''SELECT o.*,e.ticket FROM inbox_outbox o
            JOIN inbox_events e ON e.id=o.event WHERE o.state='pending' ORDER BY o.event,o.recipient'''))
        for event in pending:
            member = self.registration.user(event['team'])
            if not member or not member.admin:
                with self.store.db:
                    self.store.db.execute("UPDATE inbox_outbox SET state='blocked',error='ACCESS_REVOKED' WHERE event=? AND recipient=?", (event['event'], event['recipient']))
                continue
            with self.store.db:
                self.store.db.execute("UPDATE inbox_outbox SET state='sending' WHERE event=? AND recipient=?", (event['event'],event['recipient']))
            try:
                row = self.store.ticket(event['team'],event['ticket'])
                if self.store.visible(row,event['recipient']):
                    self.card(event['team'],row,event['recipient'],'🔔 Бошқа синов роли учун хабар',cache=False)
                state, error = 'sent', None
            except RemoteError as exc:
                if exc.code == 'TELEGRAM_401':
                    raise
                state, error = ('uncertain' if exc.uncertain else 'failed'), exc.code
            with self.store.db:
                self.store.db.execute('UPDATE inbox_outbox SET state=?,error=? WHERE event=? AND recipient=?', (state,error,event['event'],event['recipient']))

    def _callback_answer(self, ident, text):
        try:
            self.telegram.call('answerCallbackQuery', {'callback_query_id':ident, 'text':text[:180], 'show_alert':True})
        except RemoteError as exc:
            if exc.code == 'TELEGRAM_401':
                raise
            # An expired callback answer never rolls back or repeats the claim.

    def handle(self, update):
        self.reply_update = None
        try:
            return self._handle(update)
        finally:
            self.reply_update = None

    def _handle(self, update):
        callback = update.get('callback_query')
        message = callback.get('message', {}) if isinstance(callback, dict) else update.get('message', {})
        sender = callback.get('from', {}) if isinstance(callback, dict) else message.get('from', {})
        chat = message.get('chat', {})
        if chat.get('type') != 'private' or sender.get('is_bot') or not sender.get('id') or sender['id'] != chat.get('id'):
            return False
        user = int(sender['id'])
        text = message.get('text', '') if not callback else ''
        text = text.strip() if isinstance(text,str) else ''
        text = OLD_LABELS.get(text,text)
        command = text.split(maxsplit=1)[0].split('@',1)[0].lower() if text.startswith('/') else text
        mode = self.store.mode(user)
        if callback and not str(callback.get('data','')).startswith('ib:'):
            return False
        if not mode and command not in {'/inbox_test', TEST_INBOX} and not callback:
            return False
        member = self.registration.user(user)
        if not member or not member.admin:
            self.store.exit(user)
            if callback:
                self._callback_answer(callback.get('id',''),'Тест инбокса доступен администратору')
            else:
                self.telegram.send(user,'Тест инбокса доступен администратору.',self.registration.keyboard(user))
            return True
        if command in {'/test','🧪 Тестовая симуляция'}:
            self.store.exit(user)
            return False
        if command in {'/profile','/members','/registrations','/approve','/reject','/revoke'}:
            return False
        if not callback:
            update_id = update.get('update_id')
            if not isinstance(update_id,int):
                return False
            previous = self.store.begin_reply(update_id,user)
            if previous is not None:
                for output in previous:
                    if output['kind'] == 'text':
                        self.say(user,output['text'],output['keyboard'])
                    else:
                        self.card(user,self.store.ticket(user,output['id']),output['actor'],output['heading'])
                if not previous:
                    # A crash between the update marker and reply cannot apply the same input twice.
                    draft = mode['draft'] if mode else None
                    prompt, keyboard = draft_prompt(draft) if draft else ('Натижани кўриш учун «Кирувчи сделкалар»ни очинг.', MENU)
                    self.say(user,'Аввалги хабар қабул қилинган.\n\n' + prompt, keyboard)
                self.flush()
                return True
            self.reply_update = update_id
        if command in {'/end_inbox_test','/endtest',EXIT}:
            self.store.exit(user)
            self.telegram.send(user,'🏁 Инбокс синови тугади. Синов сделкалари сақланди. Асосий менюга қайтдингиз.',self.registration.keyboard(user))
            return True
        if command in {'/inbox_test', TEST_INBOX}:
            if self.simulation:
                self.simulation.set_active(user,False)
            self.store.bootstrap(user)
            self.store.set_mode(user,'sales')
            self.say(user,'👋 <b>Инбокс синовига хуш келибсиз!</b>\n\nБир аккаунтда иккита синов роли: сотув менежери ва техник. Ҳозир сиз — сотув менежери.\n\n'
                     '1️⃣ «Янги сделка»ни босинг ва «Умумий» навбатни танланг.\n'
                     '2️⃣ Сарлавҳа ва тавсифни ёзиб, сделкани юборинг.\n'
                     '3️⃣ «Техник»ка ўтинг → «Кирувчи сделкалар» → «Ўзимга олиш».\n\n'
                     'Сўнг менежерга қайтиб, ўша сделкани олишни синаб кўринг — бот ким олганини кўрсатади.\n'
                     '📖 Йўриқномада тақсимлаш усули ҳам бор. /end_inbox_test — чиқиш.')
            self.flush()
            return True
        if not mode:
            self._callback_answer(callback.get('id',''),'Сначала откройте /inbox_test')
            return True
        actor = mode['role']
        try:
            if callback:
                match = re.fullmatch(r'ib:(sales|tech):(\d+):(\d+):(take|triage|assign|close|release):(-|sales|tech)', str(callback.get('data','')))
                if not match:
                    raise InboxError('Некорректная кнопка')
                role, ident, version, action, target = match.groups()
                if role != actor:
                    raise InboxError('Переключитесь в профиль «' + PROFILES[role] + '» и откройте инбокс')
                result = self.store.change(user,actor,int(ident),action,int(version),target if target != '-' else None)
                response = result['message']
                if result.get('busy_owner'):
                    response = result['busy_prefix'] + person_label(self.profile(result['busy_owner']),markup=False)
                self._callback_answer(callback['id'],response)
                self.card(user,result['ticket'],actor,response)
                self.flush()
                return True
            if command in {SALES,TECH,'/as_sales','/as_tech'}:
                actor = 'sales' if command in {SALES,'/as_sales'} else 'tech'
                self.store.set_mode(user,actor)
                self.say(user,'👤 Ҳозирги рол: ' + PROFILES_UZ[actor] + '.\nТугалланмаган қоралама бекор қилинди; юборилган сделкалар сақланди.\n\n▶️ «Кирувчи сделкалар»ни очинг ёки «Янги сделка»ни яратинг.')
            elif command in {INBOX,MINE,'/inbox','/mine'} or re.fullmatch(r'/ticket\s+\d+',text):
                rows = [self.store.ticket(user,int(text.split()[1]))] if text.startswith('/ticket ') else self.store.listing(user,actor,command in {MINE,'/mine'})
                self.say(user,'👤 Рол: ' + PROFILES_UZ[actor] + '\n' + ('📭 Ҳозирча сделкалар йўқ. «Янги сделка»ни яратишингиз мумкин.' if not rows else f'📥 Охирги {len(rows)} та сделка. Кейинги амал карточка остида. Битта сделка учун: /ticket рақам.'))
                missing = self.store.db.execute("SELECT count(*) FROM inbox_outbox WHERE team=? AND state IN ('failed','uncertain')", (user,)).fetchone()[0]
                if missing:
                    self.say(user,f'Не подтверждена доставка уведомлений: {missing}. Заявки сохранены и доступны в инбоксе. /inbox_resend повторит уведомления; уже доставленное при потере ответа может появиться ещё раз.')
                for row in rows:
                    self.card(user,row,actor)
            elif command == '/inbox_resend':
                with self.store.db:
                    self.store.db.execute("UPDATE inbox_outbox SET state='pending',error=NULL WHERE team=? AND state IN ('failed','uncertain')", (user,))
                self.flush()
                self.say(user,'Повторная отправка выполнена. Результат доставки записан; заявки доступны через инбокс.')
            elif command in {NEW,'/request','/support'}:
                draft = {'step':'queue','request_id':'inbox-' + uuid.uuid4().hex}
                self.store.set_mode(user,actor,draft)
                self.say(user,*draft_prompt(draft))
            elif command in {'/cancel',CANCEL_REQUEST}:
                self.store.set_mode(user,actor)
                self.say(user,'❌ Қоралама бекор қилинди. «Янги сделка»ни яратинг ёки инбоксни очинг.')
            elif mode['draft']:
                draft = mode['draft']
                if draft['step'] == 'queue':
                    if text not in QUEUE_LABELS:
                        raise InboxError('Выберите очередь кнопкой')
                    draft.update(queue=QUEUE_LABELS[text],step='title')
                    self.store.set_mode(user,actor,draft)
                    self.say(user,*draft_prompt(draft))
                elif draft['step'] == 'title':
                    if not 3 <= len(text) <= 100 or text.startswith('/'):
                        raise InboxError('Название: 3–100 символов')
                    draft.update(title=text,step='description')
                    self.store.set_mode(user,actor,draft)
                    self.say(user,*draft_prompt(draft))
                elif draft['step'] == 'description':
                    if not 5 <= len(text) <= 1200 or text.startswith('/'):
                        raise InboxError('Описание: 5–1200 символов')
                    draft.update(description=text,step='confirm')
                    self.store.set_mode(user,actor,draft)
                    prompt, keyboard = draft_prompt(draft)
                    self.say(user,'Навбат: ' + QUEUES_UZ[draft['queue']] + '\n<b>' + html.escape(draft['title']) + '</b>\n' + html.escape(text) + '\n\n' + prompt, keyboard)
                elif draft['step'] == 'confirm':
                    if text != SEND:
                        raise InboxError('Нажмите «Отправить заявку» или отмените черновик')
                    row = self.store.create(user,actor,draft['queue'],draft['title'],draft['description'],draft['request_id'])
                    self.store.set_mode(user,actor)
                    self.say(user,f"✅ Сделка #{row['id']} синов инбоксига юборилди.\n\n▶️ «Техник»ка ўтинг ва «Кирувчи сделкалар»ни очинг.\n📖 Йўриқномада тақсимлаш қадамлари ҳам бор.")
                    self.card(user,row,actor)
                    self.flush()
            else:
                self.say(user,'👤 Рол: ' + PROFILES_UZ[actor] + '.\n▶️ «Янги сделка», «Кирувчи сделкалар» ёки «Йўриқнома»ни танланг.')
        except InboxError as exc:
            if callback:
                self._callback_answer(callback.get('id',''),str(exc))
            draft = (self.store.mode(user) or {}).get('draft')
            prompt, keyboard = draft_prompt(draft) if draft else ('▶️ Инбоксни очинг ёки йўриқномадан фойдаланинг.', MENU)
            self.say(user,'⚠️ ' + html.escape(str(exc)) + '\n\n' + prompt, keyboard)
        return True


@contextmanager
def inbox_test_for(registration, telegram, simulation=None):
    path = ROOT / 'data' / 'communications_test.sqlite3'
    with process_lock(path):
        store = InboxStore(path)
        try:
            for administrator in registration.settings.admins:
                member = registration.user(administrator)
                if member and member.admin:
                    store.bootstrap(administrator)
            with store.db:
                store.db.execute("UPDATE inbox_outbox SET state='uncertain',error='PROCESS_RESTART' WHERE state='sending'")
            yield InboxTest(registration,store,telegram,simulation)
        finally:
            store.close()
