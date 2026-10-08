"""FOM requests: confirmed employees, direct recipient, stable CRM link and handoffs."""
from __future__ import annotations

from .i18n import tr

import html
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

from .api import RemoteError
from .communications import InboxError, person_label
from .config import ConfigError
from .guidance import GUIDE, with_guide
from .service import SUPPORT, SUPPORT_LABELS
from .i18n import user_language
from .navigation import HOME, check_phone_step, input_error, menu_text, phrase
from .intake_ui import phrase as intake_phrase
from .support_types import preview as support_preview
from .support_contact import preview as contact_preview, collect as collect_contact
from . import technician_picker


def intake_saving():
    return intake_phrase('saving')

INBOX = '📥 Техник ёрдам заявкалари'
NEW = '📝 Техник ёрдам заявкаси очиш'
MINE = '📌 Менга тайинланган'
SENT = '📤 Юборган заявкаларим'
STATS = '📊 Техник ёрдам статистикаси'
SEND = '✅ Заявкани юбориш'
CANCEL = '❌ Заявка қораламасини бекор қилиш'
TO_POOL = '📥 На распределение'
TO_PERSON = '🛠 Назначить технику'
ALL_TECHNICIANS = '↩️ Все техники'
REFRESH_TECHNICIANS = '🔄 Обновить список техников'
OLD_LABELS = {'📥 Сўровлар':INBOX,'📝 ФОМ сўрови':NEW,'📤 Юборган сўровларим':SENT,
              '📊 Сўровлар статистикаси':STATS,'✅ Сўровни юбориш':SEND,
              '❌ Сўров қораламасини бекор қилиш':CANCEL}
OLD_LABELS.update({'📥 Техник ёрдам сделкалари':INBOX,'📝 Техник ёрдам сделкаси':NEW,
                  '📤 Юборган сделкаларим':SENT,'✅ Сделкани юбориш':SEND,
                  '❌ Сделка қораламасини бекор қилиш':CANCEL})
ROLES = {'fom_sales': 'ФОМ сотув менежери', 'tech': 'Техник', 'trainer': 'Ўқитувчи', 'dispatcher': 'Диспетчер'}
DIAGNOSTICS = '📚 ФОМ диагностикаси'
DIAGNOSTIC_LINKS = [
    ('🛠 Техник хатолар','https://docs.superhuman.com/d/_dKjHjZhfPX9/_sua2EZmm#_lu4bUKyu'),
    ('🎓 Ўқитиш','https://docs.superhuman.com/d/_dKjHjZhfPX9/_su1EZQTM#_lum9ct5H')]
STATUSES = {'assigned': 'Қабул қилиш кутилмоқда', 'new': 'Тақсимлаш навбатида',
            'triage': 'Тақсимланмоқда', 'working': 'Ишланмоқда', 'waiting': 'Жавоб кутилмоқда',
            'closed': 'Якунланган', 'failed': 'Б24да бекор қилинган'}
ACTIONS = {'created': 'Юборилди', 'take': 'Ишга қабул қилинди', 'triage': 'Тақсимлашга қабул қилинди',
           'assign': 'Бошқа ходимга берилди', 'pool': 'Тақсимлашга юборилди', 'close': 'Якунланди',
           'recover': 'Масъулнинг кириши ўчирилган: тақсимлашга қайтарилди',
           'wait': 'Кутишга ўтказилди', 'resume': 'Иш давом эттирилди',
           'r1': 'R1 навбати танланди', 'r234': 'R2–R3–R4 навбати танланди',
           'crm': 'Б24даги ҳолат қабул қилинди'}
TERMINAL = {'closed', 'failed'}


def timestamp(value):
    result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def elapsed(row, now=None):
    end = timestamp(row['resolved_at']) if row.get('resolved_at') else (now or datetime.now(timezone.utc))
    return max(0, int((end-timestamp(row['created_at'])).total_seconds()))


def duration(seconds):
    days, seconds = divmod(int(seconds), 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return (''.join([format(days, ''), tr(' кун ')]) if days else '') + f'{hours:02}:{minutes:02}:{seconds:02}'


class WorkStore:
    def __init__(self, primary, admins=()):
        self.primary, self.db = primary, primary.db
        self.admins = frozenset(admins)
        self.crm_enabled = False
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS fom_request_members(
                telegram_id INTEGER PRIMARY KEY, role TEXT NOT NULL,
                granted_by INTEGER NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS fom_request_sessions(user_id INTEGER PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fom_deal_references(
                user_id INTEGER NOT NULL,deal_id INTEGER NOT NULL,url TEXT NOT NULL,title TEXT NOT NULL,
                bitrix_owner INTEGER NOT NULL,test INTEGER NOT NULL DEFAULT 0,
                checked_at TEXT DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(user_id,deal_id));
            CREATE TABLE IF NOT EXISTS fom_pharmacy_references(
                user_id INTEGER NOT NULL,pharmacy_id INTEGER NOT NULL,company_id INTEGER NOT NULL,
                url TEXT NOT NULL,title TEXT NOT NULL,checked_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id,pharmacy_id));
            CREATE TABLE IF NOT EXISTS fom_requests(
                id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL UNIQUE,
                creator INTEGER NOT NULL, initial_recipient INTEGER NOT NULL,
                deal_id INTEGER NOT NULL, deal_url TEXT NOT NULL, deal_request TEXT NOT NULL,
                title TEXT NOT NULL, description TEXT NOT NULL, test INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL, owner INTEGER, version INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS fom_request_participants(
                ticket INTEGER NOT NULL,user_id INTEGER NOT NULL,PRIMARY KEY(ticket,user_id));
            CREATE TABLE IF NOT EXISTS fom_request_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ticket INTEGER NOT NULL,version INTEGER NOT NULL,
                actor INTEGER NOT NULL,action TEXT NOT NULL,old_owner INTEGER,new_owner INTEGER,
                at TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(ticket,version));
            CREATE TABLE IF NOT EXISTS fom_request_outbox(
                event INTEGER NOT NULL,recipient INTEGER NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
                error TEXT,PRIMARY KEY(event,recipient));
            CREATE TABLE IF NOT EXISTS fom_request_replies(
                update_id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL,outputs TEXT NOT NULL DEFAULT '[]');
            CREATE INDEX IF NOT EXISTS fom_request_owner ON fom_requests(owner,status,id);
        ''')
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(fom_requests)')}
        for name, definition in [('queue', "TEXT NOT NULL DEFAULT 'dispatch'"),
                                  ('work_at', 'TEXT'), ('resolved_at', 'TEXT'),
                                  ('archived', 'INTEGER NOT NULL DEFAULT 0'),
                                  ('link_kind', "TEXT NOT NULL DEFAULT 'deal'"),
                                  ('pharmacy_id','INTEGER'),('pharmacy_company_id','INTEGER'),
                                  ('support_details','TEXT'),('contact_details','TEXT'),
                                  ('technician_details','TEXT')]:
            if name not in columns:
                self.db.execute(f'ALTER TABLE fom_requests ADD COLUMN {name} {definition}')
        # Recover exact timestamps from the existing event journal, never from restart time.
        self.db.execute('''UPDATE fom_requests SET work_at=(SELECT min(at) FROM fom_request_events
            WHERE ticket=fom_requests.id AND action='take') WHERE work_at IS NULL''')
        self.db.execute('''UPDATE fom_requests SET resolved_at=(SELECT max(at) FROM fom_request_events
            WHERE ticket=fom_requests.id AND action='close') WHERE status='closed' AND resolved_at IS NULL''')
        self.db.commit()

    def role(self, user):
        row = self.db.execute('''SELECT m.role FROM fom_request_members m JOIN registrations r
            ON r.telegram_id=m.telegram_id WHERE m.telegram_id=? AND r.status='approved' ''', (user,)).fetchone()
        return row['role'] if row and row['role'] in ROLES else None

    def grant(self, user, role, admin):
        if admin not in self.admins or not self.primary.registration(admin) or self.primary.registration(admin)['status']!='approved':
            raise InboxError(tr('Ролларни тасдиқланган администратор белгилайди.'))
        if role not in {*ROLES, 'off'}:
            raise InboxError(tr('Ролни рўйхатдан танланг.'))
        registration = self.primary.registration(user)
        if not registration or registration['status'] != 'approved':
            raise InboxError(tr('Аввал ходимнинг рўйхатдан ўтишини тасдиқланг.'))
        with self.db:
            if role == 'off':
                self.db.execute('DELETE FROM fom_request_members WHERE telegram_id=?', (user,))
            else:
                self.db.execute('''INSERT INTO fom_request_members(telegram_id,role,granted_by) VALUES(?,?,?)
                    ON CONFLICT(telegram_id) DO UPDATE SET role=excluded.role,granted_by=excluded.granted_by,
                    updated_at=CURRENT_TIMESTAMP''', (user, role, admin))
                if role == 'tech' and self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fom_request_crm'").fetchone():
                    for ticket in self.db.execute('SELECT id,technician_details FROM fom_requests WHERE owner IS NULL AND technician_details IS NOT NULL AND archived=0'):
                        details = json.loads(ticket['technician_details'])
                        if details['bitrix_id'] == registration['bitrix_id']:
                            self.db.execute('UPDATE fom_request_crm SET last_checked=NULL WHERE ticket=?',(ticket['id'],))

    def members(self):
        return [dict(row) for row in self.db.execute('''SELECT r.telegram_id,r.name,r.bitrix_id,m.role
            FROM registrations r JOIN fom_request_members m ON r.telegram_id=m.telegram_id
            WHERE r.status='approved' ORDER BY r.name,r.telegram_id''') if row['role'] in ROLES]

    def person(self, user):
        registration = self.primary.registration(user)
        identity = self.db.execute('SELECT value FROM settings WHERE key=?', ('identity:'+str(user),)).fetchone()
        identity = json.loads(identity['value']) if identity else {}
        return {'telegram_id': int(user), 'name': registration['name'] if registration else str(user),
                'username': identity.get('username')}

    def session(self, user):
        row = self.db.execute('SELECT value FROM fom_request_sessions WHERE user_id=?', (user,)).fetchone()
        return json.loads(row['value']) if row else None

    def set_session(self, user, value):
        with self.db:
            if value is None:
                self.db.execute('DELETE FROM fom_request_sessions WHERE user_id=?', (user,))
            else:
                self.db.execute('''INSERT INTO fom_request_sessions VALUES(?,?)
                    ON CONFLICT(user_id) DO UPDATE SET value=excluded.value''', (user, json.dumps(value, ensure_ascii=False)))

    def begin_reply(self, ident, user):
        with self.db:
            changed = self.db.execute('INSERT OR IGNORE INTO fom_request_replies(update_id,user_id) VALUES(?,?)', (ident,user)).rowcount
        row = self.db.execute('SELECT * FROM fom_request_replies WHERE update_id=?', (ident,)).fetchone()
        if row['user_id'] != user:
            raise InboxError(tr('Хабар бошқа ходимга тегишли.'))
        return None if changed else json.loads(row['outputs'])

    def cache(self, ident, output):
        with self.db:
            row = self.db.execute('SELECT outputs FROM fom_request_replies WHERE update_id=?', (ident,)).fetchone()
            outputs = json.loads(row['outputs'])
            outputs.append(output)
            self.db.execute('UPDATE fom_request_replies SET outputs=? WHERE update_id=?',
                            (json.dumps(outputs, ensure_ascii=False),ident))

    def prune(self, offset):
        with self.db:
            self.db.execute('DELETE FROM fom_request_replies WHERE update_id<?', (offset-1000,))

    def ticket(self, ident):
        row = self.db.execute('SELECT * FROM fom_requests WHERE id=?', (ident,)).fetchone()
        if not row:
            raise InboxError(tr('Заявка топилмади.'))
        return self._ticket_value(row)

    @staticmethod
    def _ticket_value(row):
        value = dict(row)
        value['support_details'] = json.loads(value.get('support_details') or 'null')
        value['contact_details'] = json.loads(value.get('contact_details') or 'null')
        value['technician_details'] = json.loads(value.get('technician_details') or 'null')
        return value

    def connected_technician(self, bitrix_id):
        matches = [member for member in self.members()
                   if member['role'] == 'tech' and int(member['bitrix_id']) == int(bitrix_id)]
        return matches[0]['telegram_id'] if len(matches) == 1 else None

    def visible(self, row, user):
        return bool(not row.get('archived',False) and self.role(user) and (user in self.admins or user in {row['creator'],row['owner']}
            or (row['status'] == 'new' and row['owner'] is None)
            or self.db.execute('SELECT 1 FROM fom_request_participants WHERE ticket=? AND user_id=?', (row['id'],user)).fetchone()))

    def listing(self, user, scope='all', before=None, limit=10):
        if not self.role(user):
            return []
        sql = '''SELECT t.* FROM fom_requests t WHERE
            (t.creator=? OR t.owner=? OR (t.status='new' AND t.owner IS NULL)
            OR EXISTS(SELECT 1 FROM fom_request_participants p WHERE p.ticket=t.id AND p.user_id=?))'''
        args = [user,user,user]
        if user in self.admins:
            sql,args = 'SELECT t.* FROM fom_requests t WHERE 1=1',[]
        sql += ' AND t.archived=0'
        if scope == 'mine':
            sql += " AND t.owner=? AND t.status NOT IN ('closed','failed')"
            args.append(user)
        elif scope == 'sent':
            sql += ' AND t.creator=?'
            args.append(user)
        if before is not None:
            sql += ' AND t.id<?'
            args.append(before)
        sql += ' ORDER BY t.id DESC'
        if limit is not None:
            sql += ' LIMIT '+str(int(limit))
        return [self._ticket_value(row) for row in self.db.execute(sql,args)]

    def _event(self, row, actor, action, previous, recipients):
        ident = self.db.execute('''INSERT INTO fom_request_events(ticket,version,actor,action,old_owner,new_owner)
            VALUES(?,?,?,?,?,?)''', (row['id'],row['version'],actor,action,previous,row['owner'])).lastrowid
        for recipient in set(recipients)-{None}:
            self.db.execute('INSERT OR IGNORE INTO fom_request_participants VALUES(?,?)', (row['id'],recipient))
            if recipient != actor and self.role(recipient):
                self.db.execute('INSERT INTO fom_request_outbox(event,recipient) VALUES(?,?)', (ident,recipient))
        if self.crm_enabled and action != 'crm':
            self.db.execute('''INSERT INTO fom_request_crm_jobs(ticket,version,snapshot)
                VALUES(?,?,?)''', (row['id'],row['version'],json.dumps(row,ensure_ascii=False)))

    def create(self, actor, recipient, link, title, description, request_id, queue='dispatch', *, support_details=None, contact_details=None, technician_details=None):
        if technician_details is not None:
            if not self.role(actor) == 'fom_sales' or not isinstance(technician_details,dict) or not getattr(self,'technicians',None):
                raise InboxError(tr('Выберите техника из структуры компании.'))
            try:
                technician_details = self.technicians.employee(technician_details.get('bitrix_id'))
            except RemoteError as exc:
                raise InboxError(tr('Не удалось проверить техника в Б24. Обновите список и попробуйте ещё раз.')) from exc
            if technician_details['bitrix_id'] == self.primary.registration(actor)['bitrix_id']:
                raise InboxError(tr('Выберите другого техника или оставьте заявку в очереди.'))
            recipient = self.connected_technician(technician_details['bitrix_id'])
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.role(actor) != 'fom_sales' or (recipient is not None and (recipient == actor or not self.role(recipient))):
                raise InboxError(tr('Заявкани ФОМ сотув менежери тасдиқланган ходимга юборади.'))
            first_recipient = recipient or 0
            if not 3 <= len(title) <= 100 or not 5 <= len(description) <= 1200:
                raise InboxError(tr('Мавзу: 3–100 белги. Тавсиф: 5–1200 белги.'))
            if queue not in {'dispatch','r1','r234'}:
                raise InboxError(tr('Ҳудуд навбатини рўйхатдан танланг.'))
            if contact_details is not None:
                if not isinstance(contact_details,dict) or collect_contact({'support_contact':'person',
                        'support_contact_name':contact_details.get('name'),'support_contact_phone':contact_details.get('phone')}) != contact_details:
                    raise InboxError(tr('Проверьте имя и телефон контактного лица.'))
            # The reference must originate from a confirmed bot operation; never trust a typed URL.
            operation = self.primary.operation(link['request_id'])
            bot_link = bool(operation and operation['status'] in {'succeeded','created'}
                    and operation['kind'] in {'deal','support'} and operation['result']
                    and operation['value'].get('workflow') == 'live_deal'
                    and operation['result']['id'] == link['id'] and operation['result']['url'] == link['url']
                    and (operation['user_id'] == actor or actor in self.admins))
            reference = self.db.execute('SELECT * FROM fom_deal_references WHERE user_id=? AND deal_id=?', (actor,link['id'])).fetchone()
            existing_link = bool(reference and reference['url']==link['url']
                and reference['bitrix_owner']==self.primary.registration(actor)['bitrix_id']
                and link['request_id']==f"b24-existing:{actor}:{link['id']}")
            pharmacy_reference = self.db.execute('SELECT * FROM fom_pharmacy_references WHERE user_id=? AND pharmacy_id=?',
                (actor,link.get('pharmacy_id',0))).fetchone()
            pharmacy_link = bool(link.get('link_kind')=='pharmacy' and link['id']==0 and pharmacy_reference
                and pharmacy_reference['url']==link['url'] and pharmacy_reference['company_id']==link.get('company_id')
                and link['request_id']==f"pharmacy:{actor}:{link['pharmacy_id']}")
            if not bot_link and not existing_link and not pharmacy_link:
                raise InboxError(tr('Б24 билан тасдиқланган боғланишни танланг.'))
            test = bool((operation and operation['value'].get('live_test')) or
                        (reference and reference['test']) or title.startswith('[ТЕСТ БОТА]'))
            existing = self.db.execute('SELECT * FROM fom_requests WHERE request_id=?', (request_id,)).fetchone()
            if existing:
                row = dict(existing)
                if json.loads(row.get('support_details') or 'null') != support_details:
                    raise InboxError(tr('Заявка рақами бошқа маълумотларга тегишли.'))
                if json.loads(row.get('contact_details') or 'null') != contact_details:
                    raise InboxError(tr('Заявка рақами бошқа маълумотларга тегишли.'))
                saved_technician = json.loads(row.get('technician_details') or 'null')
                if (saved_technician or {}).get('bitrix_id') != (technician_details or {}).get('bitrix_id'):
                    raise InboxError(tr('Заявка рақами бошқа маълумотларга тегишли.'))
                if (row['creator'],row['deal_request'],row['title'],row['description']) != (
                        actor,link['request_id'],title,description) or (not technician_details and row['initial_recipient'] != first_recipient):
                    raise InboxError(tr('Заявка рақами бошқа маълумотларга тегишли.'))
                if pharmacy_link and (row['pharmacy_id'],row['pharmacy_company_id']) != (link['pharmacy_id'],link['company_id']):
                    raise InboxError(tr('Б24 билан тасдиқланган боғланишни танланг.'))
            else:
                ident = self.db.execute('''INSERT INTO fom_requests(request_id,creator,initial_recipient,
                    deal_id,deal_url,deal_request,title,description,test,status,owner,queue,support_details,contact_details,technician_details)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (request_id,actor,first_recipient,link['id'],link['url'],
                    link['request_id'],title,description,int(test),'assigned' if recipient or technician_details else 'new',recipient,queue,
                    json.dumps(support_details,ensure_ascii=False) if support_details else None,
                    json.dumps(contact_details,ensure_ascii=False) if contact_details else None,
                    json.dumps(technician_details,ensure_ascii=False) if technician_details else None)).lastrowid
                if pharmacy_link:
                    self.db.execute("UPDATE fom_requests SET link_kind='pharmacy',pharmacy_id=?,pharmacy_company_id=? WHERE id=?",
                        (link['pharmacy_id'],link['company_id'],ident))
                row = self.ticket(ident)
                recipients = {actor,recipient} if recipient or technician_details else {actor,*[member['telegram_id'] for member in self.members() if member['role'] != 'fom_sales']}
                self._event(row,actor,'created',None,recipients)
            self.db.execute('DELETE FROM fom_request_sessions WHERE user_id=?', (actor,))
            self.db.commit()
            return self.ticket(row['id'])
        except Exception:
            self.db.rollback()
            raise

    def change(self, actor, ident, version, action, target=None, *, admin=False):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.ticket(ident)
            admin = bool(admin and actor in self.admins and self.primary.registration(actor)
                         and self.primary.registration(actor)['status']=='approved')
            if not self.visible(row,actor) and not (admin and action=='recover'):
                raise InboxError(tr('Бу заявка сизга очиқ эмас.'))
            if row['status'] in TERMINAL:
                raise InboxError(tr('Заявка якунланган.'))
            owner = row['owner']
            if action in {'take','triage'} and owner and owner != actor:
                self.db.commit()
                return {'changed':False,'ticket':row,'busy':owner}
            if row['version'] != version:
                raise InboxError(tr('Карточка ўзгарди. /requests орқали янгиланг.'))
            if self.crm_enabled and self.db.execute('''SELECT 1 FROM fom_request_crm_jobs
                    WHERE ticket=? AND state NOT IN ('confirmed','superseded') LIMIT 1''', (ident,)).fetchone():
                raise InboxError(tr('Аввал Б24 билан сақлашни текширинг: «Б24ни текшириш»ни босинг.'))
            if self.crm_enabled:
                binding = self.db.execute('SELECT state FROM fom_request_crm WHERE ticket=?',(ident,)).fetchone()
                if binding and binding['state']!='confirmed':
                    raise InboxError(tr('Б24даги ўзгаришни текширинг. Ҳозирча янги амал сақланмайди.'))
            next_owner, status = owner, row['status']
            if action == 'take' and ((owner is None and status=='new') or (owner==actor and status in {'assigned','triage'})):
                next_owner,status = actor,'working'
            elif action == 'triage' and owner is None and status=='new':
                next_owner,status = actor,'triage'
            elif action == 'assign' and owner==actor and self.role(target):
                if target == actor:
                    raise InboxError(tr('Ўзингизга қайта тайинлаш шарт эмас. Ишга қабул қилинг.'))
                next_owner,status = target,'assigned'
            elif action == 'pool' and (owner==actor or (owner is None and row.get('technician_details')
                    and row['status']=='assigned' and (actor==row['creator'] or admin))):
                next_owner,status = None,'new'
            elif action == 'close' and owner==actor and status=='working':
                status = 'closed'
            elif action == 'wait' and owner==actor and status=='working':
                status = 'waiting'
            elif action == 'resume' and owner==actor and status=='waiting':
                status = 'working'
            elif action in {'r1','r234'} and (owner==actor or
                    (owner is None and status=='new' and self.role(actor)=='dispatcher')) and status in {'new','assigned','triage'}:
                pass
            elif action == 'recover' and admin and owner and not self.role(owner):
                next_owner,status = None,'new'
            else:
                raise InboxError(tr('Бу амални ҳозирги масъул бажаради. Аввал заявкани қабул қилинг.'))
            queue = action if action in {'r1','r234'} else 'dispatch' if action in {'pool','recover','triage'} else row['queue']
            if self.db.execute('''UPDATE fom_requests SET owner=?,status=?,queue=?,
                work_at=CASE WHEN ?='working' THEN coalesce(work_at,CURRENT_TIMESTAMP) ELSE work_at END,
                resolved_at=CASE WHEN ?='closed' THEN CURRENT_TIMESTAMP ELSE resolved_at END,version=version+1,
                updated_at=CURRENT_TIMESTAMP WHERE id=? AND version=?''',
                (next_owner,status,queue,status,status,ident,version)).rowcount != 1:
                raise InboxError(tr('Заявкани бошқа ходим ўзгартирди.'))
            after = self.ticket(ident)
            if action in {'pool','recover','assign'} and after.get('technician_details'):
                self.db.execute('UPDATE fom_requests SET technician_details=NULL WHERE id=?',(ident,))
                after = self.ticket(ident)
            recipients = {row['creator'],owner,next_owner}
            if action in {'pool','recover'}:
                recipients.update(member['telegram_id'] for member in self.members())
            self._event(after,actor,action,owner,recipients)
            self.db.commit()
            return {'changed':True,'ticket':after}
        except Exception:
            self.db.rollback()
            raise

    def history(self, ident):
        return [dict(row) for row in self.db.execute('SELECT * FROM fom_request_events WHERE ticket=? ORDER BY id DESC LIMIT 10', (ident,))][::-1]

    def stats(self, user, start=None, end=None):
        sql='''SELECT count(*) total,
            coalesce(sum(creator=?),0) sent,coalesce(sum(owner=? AND status!='closed'),0) assigned,
            coalesce(sum(owner=? AND status='working'),0) working,
            coalesce(sum(owner=? AND status='closed'),0) closed,
            coalesce(sum(owner=? AND status='waiting'),0) waiting,
            avg(CASE WHEN owner=? AND status='closed' AND resolved_at IS NOT NULL THEN
                max(0,(julianday(resolved_at)-julianday(created_at))*86400) END) resolution_seconds
            FROM fom_requests WHERE test=0 AND archived=0 AND (creator=? OR owner=?)'''
        args=[user]*8
        if start:
            sql+=' AND julianday(created_at)>=julianday(?)'
            args.append(start)
        if end:
            sql+=' AND julianday(created_at)<=julianday(?)'
            args.append(end)
        return dict(self.db.execute(sql,args).fetchone())


class WorkInbox:
    def __init__(self, registration, telegram, live, simulation=None, test_inbox=None, support_settings=None):
        self.registration, self.telegram, self.live = registration,telegram,live
        self.simulation, self.test_inbox = simulation,test_inbox
        self.store = WorkStore(registration.store, registration.settings.admins)
        from .work_sync import WorkSync
        self.sync = WorkSync(self.store,live.crm,support_settings) if support_settings else None
        self.background = None
        self.reply_update = None
        if not self.store.db.execute("SELECT 1 FROM settings WHERE key='fom-inbox-bootstrap'").fetchone():
            for user in registration.settings.admins:
                self.store.grant(user,'fom_sales',user)
            with self.store.db:
                self.store.db.execute("INSERT INTO settings VALUES('fom-inbox-bootstrap','1')")
        registration.work_inbox = self
        live.work_inbox = self
        from .support_intake import SupportIntake
        self.intake = SupportIntake(self)

    def keyboard(self, user):
        rows = [[MINE],[SENT],[DIAGNOSTICS]]
        if self.registration.support_inbox_allowed(user):
            rows[0].insert(0,INBOX)
        if self.registration.support_statistics_allowed(user):
            rows.insert(2,[STATS])
        if self.store.role(user)=='fom_sales':
            rows.insert(0,[self.registration.form_action_button(user,'support',NEW)])
        return with_guide(rows+[['/start']])

    def say(self, user, text, keyboard=None, *, cache=True):
        keyboard = keyboard if keyboard is not None else self.keyboard(user)
        if cache and self.reply_update is not None:
            self.store.cache(self.reply_update,{'kind':'text','text':text,'keyboard':keyboard})
        self.telegram.send(user,text,keyboard)

    def inline(self, user, text, buttons):
        self.telegram.call('sendMessage',{'chat_id':user,'text':text,'parse_mode':'HTML',
            'reply_markup':{'inline_keyboard':buttons},'link_preview_options':{'is_disabled':True}})

    def answer(self, ident, text=''):
        try:
            self.telegram.call('answerCallbackQuery',{'callback_query_id':ident,'text':text[:180],'show_alert':bool(text)})
        except RemoteError as exc:
            if exc.code=='TELEGRAM_401':
                raise

    @user_language
    def card(self, user, row, *, cache=True, heading='', heading_action=None):
        if row.get('archived'):
            raise InboxError(tr('Бу заявка архивланган. /requests орқали жорий заявкаларни очинг.'))
        if not self.store.visible(row,user):
            raise InboxError(tr('Заявка сизга очиқ эмас.'))
        if self.sync:
            self.sync.pull(row['id'])
            row = self.store.ticket(row['id'])
            if not self.store.visible(row,user):
                raise InboxError(tr('Заявка сизга очиқ эмас.'))
        if cache and self.reply_update is not None:
            self.store.cache(self.reply_update,{'kind':'card','id':row['id']})
        if heading_action:
            heading = '🔔 '+tr(ACTIONS[heading_action[0]])+''.join([tr(' · амал #'), format(heading_action[1], ''), tr('; қуйида ҳозирги ҳолат')])
        label = '[ТЕСТ БОТА] ' if row['test'] else ''
        technician = row.get('technician_details')
        owner = person_label(self.store.person(row['owner'])) if row['owner'] else html.escape(technician['name']) if technician else tr('Ҳали тайинланмаган')
        first_recipient = html.escape(technician['name']) if technician else person_label(self.store.person(row['initial_recipient'])) if row['initial_recipient'] else tr('На распределение')
        text = (heading+'\n' if heading else '') + ''.join(['<b>📩 ', format(label, ''), tr('Заявка #'), format(row['id'], ''), ' · ', format(html.escape(row['title']), ''), '</b>\n'])
        text += (''.join([tr('Ҳолат: '), format(tr(STATUSES[row['status']]), ''), tr('\nМуаллиф: '), format(person_label(self.store.person(row['creator'])), ''), tr('\nБиринчи қабул қилувчи: '), format(first_recipient, ''), tr('\nМасъул: '), format(owner, ''), '\n🔗 <a href="', format(html.escape(row['deal_url'],quote=True), ''), tr('">Б24 сделкаси #'), format(row['deal_id'], ''), '</a>\n\n'])
                 + (support_preview(row.get('support_details'))+'\n\n' if row.get('support_details') else '')
                 + (contact_preview(row.get('contact_details'),pending=False)+'\n\n' if row.get('contact_details') else '')
                 + html.escape(row['description']))
        zone = self.live.crm.config.timezone
        def local(value):
            return timestamp(value).astimezone(zone).strftime('%d.%m.%Y %H:%M:%S') if value else '—'
        text += (''.join([tr('\n\n🗓 Яратилган: '), format(local(row['created_at']), ''), tr('\n▶️ Ишга олинган: '), format(local(row['work_at']), ''), tr('\n✅ Якунланган: '), format(local(row['resolved_at']), ''), tr('\n⏱ Яратилишдан '), format(tr('якунгача' if row['resolved_at'] else 'ҳозиргача'), ''), ': ', format(duration(elapsed(row)), ''), tr('\n🕒 Тошкент вақти. Кутиш ва беришлар умумий вақтга киради.')]))
        queue_label = {'dispatch':tr('Тақсимловчи'),'r1':tr('R1 ҳудуди'),'r234':tr('R2–R3–R4 ҳудудлари')}[row['queue']]
        text += tr('\n🗺 Ҳудуд навбати: ')+queue_label
        if self.sync:
            text += self.sync.text(row)
        def button(label,action):
            return {'text':label,'callback_data':f"wr:{action}:{row['id']}:{row['version']}"}
        buttons = []
        if row['status']=='new':
            buttons += [[button(tr('🙋 Ўзимга олиш'),'take')],[button(tr('🧭 Тақсимлашга қабул қилиш'),'triage')]]
        elif row['owner']==user and row['status'] in {'assigned','triage'}:
            buttons += [[button(tr('▶️ Ишга қабул қилиш'),'take')]]
        elif row['owner'] and row['owner']!=user and row['status'] not in TERMINAL:
            buttons += [[button(tr('👤 Ким олганини текшириш'),'take')]]
        if row['owner']==user and row['status'] not in TERMINAL:
            buttons += [[button(tr('👤 Бошқа ходимга бериш'),'forward')],[button(tr('🧭 Тақсимлашга юбориш'),'pool')]]
            if row['status']=='working':
                buttons += [[button(tr('⏸ Жавоб кутиш'),'wait'),button(tr('✅ Якунлаш'),'close')]]
            if row['status']=='waiting':
                buttons += [[button(tr('▶️ Ишни давом эттириш'),'resume')]]
        if technician and row['owner'] is None and row['status']=='assigned':
            binding = self.sync.binding(row['id']) if self.sync else None
            if binding and binding['state']=='confirmed':
                text += '\n\n'+tr('Техник назначен в Б24. Уведомление в Telegram придёт ему после подключения к боту.')
            if user == row['creator'] or user in self.store.admins:
                buttons += [[button(tr('🧭 Тақсимлашга юбориш'),'pool')]]
        if row['status'] in {'new','assigned','triage'} and (row['owner']==user or
                (row['owner'] is None and self.store.role(user)=='dispatcher')):
            buttons += [[button('🗺 R1','r1'),button('🗺 R2–R3–R4','r234')]]
        member = self.registration.user(user)
        if member and member.admin and row['owner'] and not self.store.role(row['owner']) and row['status'] not in TERMINAL:
            buttons += [[button(tr('🔧 Тақсимлашга қайтариш'),'recover')]]
        if self.sync and user in {row['creator'],row['owner'],*self.store.admins}:
            buttons += [[button(tr('🔗 Б24ни текшириш'),'sync')]]
            binding = self.sync.binding(row['id'])
            if binding and binding['state']=='conflict':
                buttons += [[button(tr('↩️ Б24даги ҳолатни қабул қилиш'),'reconcile')]]
        buttons += [[button(tr('🕘 Ҳаракатлар тарихи'),'history'),button(tr('🔄 Янгилаш'),'card')],
                    [{'text':GUIDE,'callback_data':'guide:tech' if self.store.role(user) in {'tech','trainer','dispatcher'} else 'guide:work_inbox'}]]
        text += tr('\n\n▶️ Карточка остида кейинги амални танланг.')
        if row.get('link_kind')=='pharmacy':
            text = text.replace(tr('Б24 сделкаси #')+str(row['deal_id']),tr('🏪 Аптека'))
        self.inline(user,text,buttons)

    def link(self, user, ident):
        member = self.registration.user(user)
        operations = list(self.store.db.execute("""SELECT request_id,user_id FROM operations WHERE kind IN ('deal','support')
            AND result IS NOT NULL AND json_extract(result,'$.id')=?""", (ident,)))
        if not operations or (len(operations)==1 and operations[0]['user_id']!=user and not member.admin):
            # An existing CRM card can be referenced only by its actual responsible employee.
            # This gives no stage editing rights and does not invent a bot creation operation.
            row = self.live.crm.item(ident)
            if str(row.get('assignedById'))!=str(member.bitrix_id):
                raise ConfigError(tr('Б24да бу сделканинг масъули сиз эмассиз.'))
            url = self.live.crm.portal+f'/crm/deal/details/{ident}/'
            with self.store.db:
                self.store.db.execute('''INSERT INTO fom_deal_references(user_id,deal_id,url,title,bitrix_owner,test)
                    VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,deal_id) DO UPDATE SET url=excluded.url,title=excluded.title,
                    bitrix_owner=excluded.bitrix_owner,test=excluded.test,checked_at=CURRENT_TIMESTAMP''',
                    (user,ident,url,str(row['title']),member.bitrix_id,int(str(row['title']).startswith('[ТЕСТ БОТА]'))))
            return {'id':ident,'url':url,'title':row['title'],'request_id':f'b24-existing:{user}:{ident}'}
        operation = self.live.stages.operation_for(ident,user,member.admin)
        if operation['status'] not in {'succeeded','created'} or operation['value'].get('workflow')!='live_deal':
            raise InboxError(tr('Аввал Б24да яратилган сделкани танланг.'))
        row = self.live.item_for(operation,member,user)
        return {'id':int(row['id']),'url':operation['result']['url'],'title':row['title'],
                'request_id':operation['request_id']}

    def start(self, user, ident=None):
        if self.store.role(user)!='fom_sales':
            raise InboxError(tr('Техник ёрдам заявкасини ФОМ сотув менежери юборади. Ролни Малик белгилайди.'))
        state = self.registration.store.session(user)
        if state and state.get('step') not in {'done','stats_scope','stats_period'}:
            raise InboxError(tr('Аввал жорий Б24/ОКБ қораламасини тугатинг ёки /cancel ни босинг.'))
        if self.store.session(user):
            self.prompt(user,self.store.session(user))
            return
        draft = {'step':'deal','request_id':'fom-request-'+uuid4().hex}
        if ident is not None:
            draft.update(link=self.link(user,ident),step='recipient')
        else:
            rows = self.store.db.execute("""SELECT request_id,result FROM operations WHERE user_id=?
                AND kind IN ('deal','support') AND status IN ('succeeded','created') AND result IS NOT NULL
                AND json_extract(value,'$.workflow')='live_deal' ORDER BY created_at DESC LIMIT 15""", (user,))
            draft['deals'] = [dict(json.loads(row['result']),request_id=row['request_id']) for row in rows]
            if not draft['deals']:
                self.say(user,tr('🔗 Ўзингиз масъул бўлган Б24 сделкасининг ID рақамини киритинг: «ID 123». /deal орқали янги сделка ҳам яратиш мумкин.'))
        self.store.set_session(user,draft)
        self.prompt(user,draft)

    @user_language
    def prompt(self, user, draft, *, error=None):
        if draft.get('web_intake') and draft['step'] in {'web_form','pharmacy_form'}:
            return self.intake.prompt(user,draft,error)
        text = {'deal':tr('1/5 · 🔗 Б24 сделкасини танланг: рўйхат рақами ёки «ID 123» деб ёзинг.'),
                'recipient':tr('2/5 · 👤 Биринчи қабул қилувчини танланг. Заявка аввал шу ходимга боради.'),
                'title':tr('3/5 · 📝 Қисқа мавзу ёзинг: 3–100 белги.'),
                'description':tr('4/5 · 💬 Муаммо ва керакли ёрдамни ёзинг: 5–1200 белги.'),
                'confirm':tr('5/5 · 👀 Карточкани текширинг. «Юбориш»дан кейин қабул қилувчига хабар боради.')}[draft['step']]
        if draft.get('web_intake'):
            text = tr('👤 Выберите первого получателя заявки.') if draft['step']=='recipient' else tr('👀 Проверьте заявку перед отправкой.')
        keyboard = [[CANCEL]]
        if draft['step']=='deal':
            text += '\n\n'+'\n'.join(''.join([format(i, ''), tr('. Б24 #'), format(row['id'], '')]) for i,row in enumerate(draft['deals'],1))
            keyboard = [[str(i)] for i in range(1,len(draft['deals'])+1)]+keyboard
        if draft['step']=='recipient':
            # Persist the shown IDs so registration changes never shift a numeric choice.
            if 'recipients' not in draft:
                draft['recipients'] = [member for member in self.store.members() if member['telegram_id'] != user]
            page = draft.get('recipient_page',0)
            rows = [(i,row) for i,row in enumerate(draft['recipients'][page*8:(page+1)*8],page*8+1) if row['telegram_id'] != user]
            self.store.set_session(user,draft)
            if draft.get('technician_picker') and self.sync:
                text, keyboard = technician_picker.prompt(self,user,draft)
                if draft.get('technician_directory_error'):
                    text, keyboard = technician_picker.unavailable()
            elif draft.get('direct_recipient'):
                text = tr('👤 Кому направить заявку?')
                text += '\n\n'+'\n'.join(f"{i}. {html.escape(row['name'])} · {tr(ROLES[row['role']])}" for i,row in rows)
                keyboard = [[str(i)] for i,row in rows]+[[tr(TO_POOL)]]+keyboard
                if page:
                    keyboard.insert(-1,['/request_prev'])
                if (page+1)*8<len(draft['recipients']):
                    keyboard.insert(-1,['/request_next'])
            else:
                text = tr('📥 Куда отправить заявку?')+'\n\n'+tr('На распределение — заявка останется в очереди региона. Назначить технику — выберите сотрудника из структуры компании.')
                keyboard = [[tr(TO_POOL)]]+keyboard
                if self.sync or any(row['telegram_id'] != user for row in draft['recipients']):
                    keyboard.insert(1,[tr(TO_PERSON)])
        if draft.get('link'):
            text += ''.join(['\n\n🔗 <a href="', format(html.escape(draft['link']['url'],quote=True), ''), tr('">Б24 сделкаси #'), format(draft['link']['id'], ''), '</a>'])
        if draft['step']=='confirm':
            if draft.get('web_intake'):
                text += '\n\n'+self.intake.preview(user,draft)
            if draft.get('support_details'):
                text += '\n\n'+support_preview(draft['support_details'])
            text += tr('\nҚабул қилувчи: ')+(html.escape(draft['technician']['name']) if draft.get('technician') else person_label(self.store.person(draft['recipient'])) if draft.get('recipient') else tr('На распределение'))
            if draft.get('technician') and not self.store.connected_technician(draft['technician']['bitrix_id']):
                text += '\n'+tr('Назначим техника в Б24. Для уведомлений в Telegram ему нужно подключиться к боту.')
            text += '\n<b>'+html.escape(draft['title'])+'</b>\n'+html.escape(draft['description'])
            keyboard = [[SEND]]+keyboard
            if self.sync:
                text += tr('\n🗺 Ҳудуд навбати: ')+{'dispatch':tr('Тақсимловчи'),'r1':'R1','r234':'R2–R3–R4'}[draft.get('queue','dispatch')]
                text += tr('\n🛠 Б24 «Техобслуживание [KG]»да алоҳида техник ёрдам заявкаси яратилади.')
                keyboard.insert(1,[tr('🗺 R1 ҳудуди'),tr('🗺 R2–R3–R4 ҳудудлари')])
                keyboard.insert(2,[tr('🧭 Тақсимловчи')])
        if error:
            text = input_error(draft, error, request=True) + '\n\n' + text
            keyboard += [[HOME]]
        self.say(user,text,with_guide(keyboard))

    @staticmethod
    def pick(text, choices):
        if not text.isascii() or not text.isdigit() or not 1<=int(text)<=len(choices):
            raise InboxError(tr('Рўйхатдан рақамни танланг.'))
        return choices[int(text)-1]

    def form(self, user, text, draft):
        if draft.get('web_intake') and draft['step'] in {'web_form','pharmacy_form'}:
            return self.intake.route(user,text,draft)
        check_phone_step(text, draft, request=True)
        step = draft['step']
        if step=='deal':
            match = re.fullmatch(r'ID\s+([1-9]\d*)',text,re.I)
            ident = int(match[1]) if match else int(self.pick(text,draft['deals'])['id'])
            draft.update(link=self.link(user,ident),step='recipient')
        elif step=='recipient':
            if self.sync and text in {TO_PERSON,tr(TO_PERSON,'uz'),'👤 Конкретному сотруднику','👤 Аниқ ходимга юбориш',REFRESH_TECHNICIANS,tr(REFRESH_TECHNICIANS,'uz')}:
                try:
                    technician_picker.open_picker(self,user,draft,force=text in {REFRESH_TECHNICIANS,tr(REFRESH_TECHNICIANS,'uz')})
                    draft.pop('technician_directory_error',None)
                except RemoteError:
                    draft.update(technician_picker=True,direct_recipient=True,technician_directory_error=True)
                self.store.set_session(user,draft)
                self.prompt(user,draft)
                return
            if text in {TO_PERSON,tr(TO_PERSON,'uz'),'👤 Конкретному сотруднику','👤 Аниқ ходимга юбориш'}:
                draft.update(direct_recipient=True,recipient_page=0)
                self.store.set_session(user,draft)
                self.prompt(user,draft)
                return
            if text in {TO_POOL,tr(TO_POOL,'uz')}:
                recipient = None
                draft.pop('technician',None)
            elif draft.get('technician_picker') and self.sync:
                technician = technician_picker.select(self,user,text,draft)
                if technician is None:
                    self.store.set_session(user,draft)
                    self.prompt(user,draft)
                    return
                draft['technician'] = technician
                recipient = self.store.connected_technician(technician['bitrix_id'])
                regions = technician['regions']
                draft['queue'] = 'r1' if regions == [1] else 'r234' if 1 not in regions else 'dispatch'
            else:
                recipient = self.pick(text,draft['recipients'])['telegram_id']
            if recipient is not None and (recipient == user or not self.store.role(recipient)):
                raise InboxError(tr('Бу ходимнинг кириши ўзгарган. /request_cancel орқали қайта бошланг.'))
            draft.update(recipient=recipient,step='confirm' if draft.get('web_intake') else 'title')
        elif step in {'title','description'}:
            minimum,maximum = (3,100) if step=='title' else (5,1200)
            if not minimum<=len(text)<=maximum or text.startswith('/'):
                raise InboxError(''.join([format(minimum, ''), '–', format(maximum, ''), tr(' белги ёзинг.')]))
            draft[step] = text
            draft['step'] = 'description' if step=='title' else 'confirm'
        elif step=='confirm':
            if draft.get('recipient') == user:
                draft.update(step='recipient',direct_recipient=False)
                self.store.set_session(user,draft)
                self.prompt(user,draft)
                return
            regions = {'🗺 R1 ҳудуди':'r1','🗺 R2–R3–R4 ҳудудлари':'r234','🧭 Тақсимловчи':'dispatch'}
            if self.sync and text in regions:
                draft['queue'] = regions[text]
                self.store.set_session(user,draft)
                self.prompt(user,draft)
                return
            if text != SEND:
                raise InboxError(phrase('choice'))
            self.say(user,intake_saving(),[[SEND]],cache=False)
            if draft.get('technician') and self.sync:
                try:
                    draft['technician'] = self.sync.technicians.employee(draft['technician']['bitrix_id'])
                except RemoteError:
                    draft.update(step='recipient',direct_recipient=False,technician_picker=False)
                    draft.pop('technician',None)
                    self.store.set_session(user,draft)
                    self.prompt(user,draft,error=tr('Этот техник больше недоступен. Выберите другого техника или оставьте заявку в очереди.'))
                    return
            # Confirm the current CRM origin and responsible employee again before delivery.
            link = self.intake.submit(user,draft) if draft.get('web_intake') else self.link(user,draft['link']['id'])
            if link is None:
                return
            row = self.store.create(user,draft['recipient'],link,draft['title'],draft['description'],draft['request_id'],draft.get('queue','dispatch'),support_details=draft.get('support_details'),contact_details=draft.get('contact_details'),technician_details=draft.get('technician'))
            if self.sync:
                self.sync.flush(row['id'])
            self.say(user,(tr('✅ Заявка #')+str(row['id'])+' '+tr('сохранена. Проверка Б24 — ниже.')) if row.get('technician_details') else
                (tr('✅ Заявка #')+str(row['id'])+' '+tr('отправлена на распределение.')) if not row['initial_recipient'] else
                ''.join([tr('✅ Заявка #'), format(row['id'], ''), tr(' сақланди. Қабул қилувчига хабарнинг натижаси инбоксда кўринади.')]))
            self.card(user,row)
            return
        self.store.set_session(user,draft)
        self.prompt(user,draft)

    def flush(self):
        if self.background:
            self.background.wake()
            return
        if self.sync:
            self.sync.flush()
            self.sync.poll()
        pending = list(self.store.db.execute('''SELECT o.*,e.ticket,e.action,e.version FROM fom_request_outbox o
            JOIN fom_request_events e ON e.id=o.event WHERE o.state='pending' ORDER BY o.event,o.recipient'''))
        for event in pending:
            recipient = event['recipient']
            state,error = 'sent',None
            row = self.store.ticket(event['ticket'])
            if not self.store.visible(row,recipient):
                with self.store.db:
                    self.store.db.execute("UPDATE fom_request_outbox SET state='blocked',error='ACCESS_REVOKED' WHERE event=? AND recipient=?", (event['event'],recipient))
                continue
            with self.store.db:
                claimed = self.store.db.execute("UPDATE fom_request_outbox SET state='sending' WHERE event=? AND recipient=? AND state='pending'", (event['event'],recipient)).rowcount
            if not claimed:
                continue
            try:
                self.card(recipient,row,cache=False,heading_action=(event['action'],event['version']))
            except RemoteError as exc:
                if exc.code=='TELEGRAM_401':
                    raise
                state,error = ('uncertain' if exc.uncertain else 'failed'),exc.code
            with self.store.db:
                self.store.db.execute('UPDATE fom_request_outbox SET state=?,error=? WHERE event=? AND recipient=?', (state,error,event['event'],recipient))

    def delivery_note(self, user, ident):
        row = self.store.ticket(ident)
        if not self.store.visible(row,user):
            raise InboxError(tr('Заявка сизга очиқ эмас.'))
        failed = list(self.store.db.execute('''SELECT o.recipient,o.state FROM fom_request_outbox o
            JOIN fom_request_events e ON e.id=o.event WHERE e.ticket=? AND o.state!='sent' ''', (ident,)))
        if self.background:
            failed = [item for item in failed if item['state'] not in {'pending', 'sending'}]
        if failed:
            self.say(user,tr('⚠️ Хабар етказилиши тасдиқланмаган: ')+', '.join(
                person_label(self.store.person(item['recipient']))+' ('+item['state']+')' for item in failed)
                +tr('. Заявка сақланган. Малик /requests_resend орқали такрор юбориши мумкин.'))

    def callback(self, user, callback):
        data = str(callback.get('data',''))
        if match := re.fullmatch(r'wr:role:([1-9]\d*):(fom_sales|tech|trainer|dispatcher|off)',data):
            if not self.registration.user(user).admin:
                raise InboxError(tr('Ролларни Малик белгилайди.'))
            self.store.grant(int(match[1]),match[2],user)
            self.registration.refresh_commands(int(match[1]))
            self.answer(callback['id'],tr('Рол сақланди.'))
            self.say(user,tr('✅ Рол сақланди: ')+person_label(self.store.person(int(match[1]))))
            return
        if match := re.fullmatch(r'wr:from:([1-9]\d*)',data):
            self.answer(callback['id'])
            self.start(user,int(match[1]))
            return
        match = re.fullmatch(r'wr:(card|history|take|triage|forward|assign|pool|close|recover|wait|resume|r1|r234|sync|reconcile):([1-9]\d*):(\d+)(?::([1-9]\d*))?',data)
        if not match:
            raise InboxError(tr('Тугма нотўғри.'))
        action,ident,version,target = match.groups()
        ident,version = int(ident),int(version)
        row = self.store.ticket(ident)
        if not self.store.visible(row,user):
            raise InboxError(tr('Заявка сизга очиқ эмас.'))
        if action=='sync':
            if not self.sync:
                raise InboxError(tr('Б24 техник ёрдам заявкаси ҳали созланмаган.'))
            self.sync.retry(ident,user)
            self.answer(callback['id'])
            self.card(user,self.store.ticket(ident))
            return
        if action=='reconcile':
            if not self.sync or row['version']!=version:
                raise InboxError(tr('Карточка ўзгарди. Янгиланг.'))
            self.sync.reconcile(ident,user)
            self.answer(callback['id'])
            self.card(user,self.store.ticket(ident))
            return
        if self.sync:
            self.sync.pull(ident)
            row = self.store.ticket(ident)
        if action=='forward':
            if row['owner']!=user or row['version']!=version or row['status'] in TERMINAL:
                raise InboxError(tr('Ҳозирги масъул карточкани янгиласин.'))
            self.answer(callback['id'])
            self.recipient_page(user,row,0)
            return
        if action=='history':
            self.answer(callback['id'])
            lines = [''.join([tr('🕘 <b>Заявка #'), format(ident, ''), tr(' · охирги 10 та амал</b>')])]
            for event in self.store.history(ident):
                line = html.escape(event['at'])+' UTC · '+tr(ACTIONS[event['action']])+' · '+person_label(self.store.person(event['actor']))
                if event['new_owner']:
                    line += ' → '+person_label(self.store.person(event['new_owner']))
                lines.append(line)
            self.say(user,'\n'.join(lines))
            return
        if action=='card':
            self.answer(callback['id'])
        else:
            result = self.store.change(user,ident,version,action,int(target) if target else None,admin=self.registration.user(user).admin)
            if self.sync and result['changed']:
                self.sync.flush(ident)
            if result.get('busy'):
                owner = person_label(self.store.person(result['busy']),markup=False)
                self.answer(callback['id'],(tr('Заявка тайинланган: ') if result['ticket']['status']=='assigned' else tr('Заявкани аллақачон олган: '))+owner)
            else:
                self.answer(callback['id'],tr('✅ Сақланди.'))
            row = result['ticket']
        self.card(user,row)
        self.flush()
        self.delivery_note(user,ident)

    def recipient_page(self, user, row, page):
        members = [member for member in self.store.members() if member['telegram_id']!=user]
        if page<0 or page*8>=len(members):
            self.say(user,tr('📭 Бошқа тасдиқланган ходим йўқ. Улар /start орқали улансин; Малик /inbox_team да ролни белгилайди.'))
            return
        buttons = [[{'text':member['name'][:70]+' · '+tr(ROLES[member['role']]),
                     'callback_data':f"wr:assign:{row['id']}:{row['version']}:{member['telegram_id']}"}]
                   for member in members[page*8:(page+1)*8]]
        if page:
            buttons.append([{'text':'⬅️','callback_data':f"wr:people:{row['id']}:{row['version']}:{page-1}"}])
        if (page+1)*8<len(members):
            buttons.append([{'text':'➡️','callback_data':f"wr:people:{row['id']}:{row['version']}:{page+1}"}])
        self.inline(user,''.join([tr('👤 Заявка #'), format(row['id'], ''), tr(' · кимга бериш керак?\nБ24 #'), format(row['deal_id'], ''), tr(' боғланиши сақланади.')]),buttons)

    def team(self, user):
        if not self.registration.user(user).admin:
            raise InboxError(tr('Ролларни Малик белгилайди.'))
        self.say(user,tr('👥 ФОМ заявкалари: роллар. /inbox_role TelegramID fom_sales|tech|trainer|dispatcher|off\nЯнги ходим аввал /start орқали рўйхатдан ўтиб, тасдиқ олади.'))
        for row in self.registration.store.registrations('approved'):
            role = self.store.role(row['telegram_id'])
            self.inline(user,person_label(self.store.person(row['telegram_id']))+' · '+tr(ROLES.get(role,'Рол берилмаган')),
                [[{'text':label,'callback_data':f"wr:role:{row['telegram_id']}:{role}"} for role,label in ROLES.items()],
                 [{'text':tr('Киришни ўчириш'),'callback_data':f"wr:role:{row['telegram_id']}:off"}]])

    def handle(self, update):
        self.reply_update = None
        try:
            return self._handle(update)
        finally:
            self.reply_update = None

    def _handle(self, update):
        callback = update.get('callback_query')
        message = callback.get('message',{}) if isinstance(callback,dict) else update.get('message',{})
        sender = callback.get('from',{}) if isinstance(callback,dict) else message.get('from',{})
        chat = message.get('chat',{})
        if chat.get('type')!='private' or sender.get('is_bot') or not sender.get('id') or sender['id']!=chat.get('id'):
            return False
        user = int(sender['id'])
        text = message.get('text','') if not callback else ''
        text = text.strip() if isinstance(text,str) else ''
        text = SUPPORT if text in SUPPORT_LABELS else OLD_LABELS.get(text,text)
        command = text.split(maxsplit=1)[0].split('@',1)[0].lower() if text.startswith('/') else text
        commands = {'/requests','/request','/support',NEW,SUPPORT,INBOX,MINE,SENT,
                    '/requests_mine','/requests_sent','/request_ticket','/request_cancel',CANCEL,
                    '/inbox_team','/inbox_role','/requests_stats',STATS,'/requests_resend','/diagnostics',DIAGNOSTICS,
                    '/request_next','/request_prev'}
        if callback and not str(callback.get('data','')).startswith('wr:'):
            return False
        # Offline simulations and the existing explicit B24 support test keep their own routes.
        if not callback and ((self.simulation and self.simulation.active(user))
                or (self.test_inbox and self.test_inbox.store.mode(user))):
            return False
        if not callback and command in {'/support',SUPPORT} and self.live.bot.test_mode(user) and not self.store.session(user):
            return False
        draft = self.store.session(user)
        if draft and command in {'/cancel','Отмена'}:
            command='/request_cancel'
        web_support = None
        web_data = message.get('web_app_data')
        if isinstance(web_data,dict) and isinstance(web_data.get('data'),str) and len(web_data['data'].encode('utf-8'))<=4096:
            try:
                parsed=json.loads(web_data['data'])
                if isinstance(parsed,dict) and parsed.get('mode')=='support':
                    web_support=parsed
            except ValueError:
                pass
        if web_data and web_support is None:
            return False
        if not callback and command not in commands and not draft and web_support is None:
            return False
        from .registration import PROFILE_LABELS
        from .live_deals import MY_CRM_LABELS
        if command in {'/profile',*PROFILE_LABELS,*MY_CRM_LABELS,'/my_crm','/members','/registrations','/approve','/reject','/revoke','/register'}:
            return False
        member = self.registration.user(user)
        if not member:
            self.telegram.send(user,tr('🔒 Аввал /start орқали уланинг ва тасдиқ олинг.'),self.registration.keyboard(user))
            return True
        self.registration.store.remember_identity(sender)
        self.live.bot.config.users[user]=member
        try:
            if command in {'/inbox_team','/inbox_role'} or (callback and str(callback.get('data','')).startswith('wr:role:')):
                if not member.admin:
                    raise InboxError(tr('Ролларни Малик белгилайди.'))
            elif not self.store.role(user):
                raise InboxError(tr('ФОМ заявкалари учун рол берилмаган. Малик /inbox_team да ролни белгилайди.'))
            if command in {'/requests_stats',STATS} or (callback and str(callback.get('data','')).startswith('wr:stats:')):
                from .support_statistics import show
                from .personal_statistics import PERIODS
                period=str(callback['data']).removeprefix('wr:stats:') if callback else 'month'
                if period not in PERIODS:
                    raise InboxError(tr('Нотўғри тугма'))
                if callback:
                    self.answer(callback['id'])
                show(self,user,period,message.get('message_id') if callback else None,message=message if callback else None)
                return True
            if callback:
                if page := re.fullmatch(r'wr:people:([1-9]\d*):(\d+):(\d+)',str(callback.get('data',''))):
                    row = self.store.ticket(int(page[1]))
                    if row['owner']!=user or row['version']!=int(page[2]) or row['status']=='closed':
                        raise InboxError(tr('Карточка ўзгарди. /requests орқали янгиланг.'))
                    self.answer(callback['id'])
                    self.recipient_page(user,row,int(page[3]))
                else:
                    self.callback(user,callback)
                return True
            if not isinstance(update.get('update_id'),int):
                return False
            previous = self.store.begin_reply(update['update_id'],user)
            if previous is not None:
                for output in previous:
                    if output['kind']=='text':
                        self.say(user,output['text'],output['keyboard'],cache=False)
                    else:
                        self.card(user,self.store.ticket(output['id']),cache=False)
                if not previous:
                    self.say(user,tr('⏳ Аввалги хабар қабул қилинган. /requests орқали натижани текширинг.'),cache=False)
                    if draft:
                        self.prompt(user,draft)
                self.flush()
                return True
            self.reply_update = update['update_id']
            if web_support is not None:
                self.intake.receive(user,web_support)
            elif command=='/inbox_team':
                self.team(user)
            elif command=='/inbox_role':
                parts = text.split()
                if len(parts)!=3 or not parts[1].isascii() or not parts[1].isdigit():
                    raise InboxError('/inbox_role TelegramID fom_sales|tech|trainer|dispatcher|off')
                self.store.grant(int(parts[1]),parts[2],user)
                self.registration.refresh_commands(int(parts[1]))
                self.say(user,tr('✅ Рол сақланди.'))
            elif command in {'/request','/support',NEW,SUPPORT}:
                parts = text.split()
                if command in {'/request','/support'} and len(parts)>1:
                    if len(parts)!=2 or not parts[1].isascii() or not parts[1].isdigit() or int(parts[1])<1:
                        raise InboxError(tr('/request Б24_ID · масалан: /request 123'))
                    self.start(user,int(parts[1]))
                else:
                    self.intake.start(user) if command in {'/support',NEW,SUPPORT} else self.start(user)
            elif command in {'/diagnostics',DIAGNOSTICS}:
                from .help_content import HELP_TEXTS
                self.inline(user,tr(HELP_TEXTS['diagnostics']['uz']),
                    [[{'text':label,'url':url}] for label,url in DIAGNOSTIC_LINKS])
            elif command in {'/request_cancel',CANCEL}:
                if draft and draft.get('pharmacy_form') and self.registration.store.operation(draft['pharmacy_form']['request_id']):
                    operation=self.registration.store.operation(draft['pharmacy_form']['request_id'])
                    if operation['status'] not in {'succeeded','existing','prepared','cancelled','rejected'}:
                        raise InboxError(tr('Сохранение уже началось. Нажмите «Отправить заявку», чтобы проверить и продолжить без дублей.'))
                self.store.set_session(user,None)
                self.say(user,menu_text(self.registration,user,cancelled=True),self.registration.keyboard(user))
            elif command in {'/request_next','/request_prev'}:
                if not draft or draft['step']!='recipient':
                    raise InboxError(tr('Аввал қабул қилувчилар рўйхатини очинг.'))
                page = draft.get('recipient_page',0)+(1 if command=='/request_next' else -1)
                choices = draft.get('technician_choices',[]) if draft.get('technician_picker') else draft['recipients']
                if page<0 or page*8>=len(choices):
                    raise InboxError(tr('Бундай саҳифа йўқ.'))
                draft['recipient_page'] = page
                self.store.set_session(user,draft)
                self.prompt(user,draft)
            elif command in {'/requests',INBOX,MINE,SENT,'/requests_mine','/requests_sent','/request_ticket'}:
                scope = 'mine' if command in {MINE,'/requests_mine'} else 'sent' if command in {SENT,'/requests_sent'} else 'all'
                parts = text.split()
                if command=='/request_ticket':
                    if len(parts)!=2 or not parts[1].isascii() or not parts[1].isdigit():
                        raise InboxError(tr('/request_ticket рақам'))
                    rows = [self.store.ticket(int(parts[1]))]
                else:
                    before = int(parts[1]) if len(parts)==2 and parts[1].isascii() and parts[1].isdigit() else None
                    rows = self.store.listing(user,scope,before)
                self.say(user,tr('📭 Заявкалар йўқ.') if not rows else tr('📥 Заявкалар. Номер ва Б24 ҳаволаси ҳар бир карточкада.'))
                for row in rows:
                    self.card(user,row)
                    self.delivery_note(user,row['id'])
                if len(rows)==10 and command!='/request_ticket':
                    next_command = {'all':'/requests','mine':'/requests_mine','sent':'/requests_sent'}[scope]
                    self.say(user,''.join([tr('➡️ Кейингилари: '), format(next_command, ''), ' ', format(rows[-1]['id'], '')]))
            elif command=='/requests_resend':
                if not member.admin:
                    raise InboxError(tr('Хабарни такрор юборишни Малик тасдиқлайди.'))
                with self.store.db:
                    self.store.db.execute("UPDATE fom_request_outbox SET state='pending',error=NULL WHERE state IN ('failed','uncertain','sending')")
                self.say(user,tr('🔔 Такрор юбориш қабул қилинди. Олдинги хабар етган бўлса, нусхаси келиши мумкин.'))
            elif draft:
                if self.store.role(user)!='fom_sales':
                    raise InboxError(tr('Техник ёрдам заявкасини ФОМ сотув менежери юборади. Ролни Малик белгилайди.'))
                if text.startswith('/'):
                    self.say(user,tr('▶️ Заявка қораламасини тугатинг ёки /request_cancel ни босинг.'))
                else:
                    self.form(user,text,draft)
            self.flush()
            if command in {'/request','/support',NEW,SUPPORT} or draft:
                last = self.store.db.execute('SELECT id FROM fom_requests WHERE creator=? ORDER BY id DESC LIMIT 1',(user,)).fetchone()
                if last and not self.store.session(user):
                    self.delivery_note(user,last['id'])
        except (InboxError,ConfigError,ValueError) as exc:
            if callback:
                self.answer(callback.get('id',''),str(exc))
            else:
                current = self.store.session(user)
                if current:
                    self.prompt(user,current,error=exc)
                else:
                    self.say(user,'⚠️ '+html.escape(str(exc)))
        except RemoteError as exc:
            if exc.code in {'TELEGRAM_401','TELEGRAM_409'}:
                raise
            # State is durable; no automatic repeat of an ambiguous Telegram send.
            self.say(user,tr('⏳ Натижа тасдиқланмади (')+html.escape(exc.code)+tr('). /requests орқали заявкани текширинг.'),cache=False)
        return True
