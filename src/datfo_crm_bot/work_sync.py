"""Journalled, source-checked FOM support cards in the existing technical pipeline."""
from __future__ import annotations
from .i18n import tr

from dataclasses import dataclass
import html
import json
from pathlib import Path
import re

from .api import RemoteError
from .config import ConfigError, ROOT
from .communications import InboxError
from .work_inbox import elapsed, duration, timestamp
from .support_types import revalidate as validate_support, native_fields, matches as support_matches
from .support_contact import SupportContact
from .technicians import TechnicianDirectory

SOURCE = 'fom-support-telegram'
START = '--- FOM BOT TIME BEGIN ---'
END = '--- FOM BOT TIME END ---'


@dataclass(frozen=True)
class SupportSettings:
    category_id: int
    stages: dict
    native_queue_routing: bool = False

    @classmethod
    def load(cls, path=None):
        path = Path(path or ROOT/'fom-support.json')
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if type(value.get('category_id')) is not int or value['category_id'] < 0:
            raise ConfigError('ФОМ: неверная воронка техобслуживания')
        stages = value.get('stages', {})
        if set(stages) != {'dispatch','r1','r234','working','waiting','closed','failed'}:
            raise ConfigError('ФОМ: требуется полная карта стадий')
        if any(not isinstance(row,dict) or not re.fullmatch(r'[A-Z0-9_:]+',str(row.get('id','')))
               or not isinstance(row.get('title'),str) or not row['title'] for row in stages.values()):
            raise ConfigError('ФОМ: неверная карта стадий')
        if len({row['id'] for row in stages.values()}) != len(stages):
            raise ConfigError('ФОМ: стадии должны различаться')
        routing = value.get('native_queue_routing',False)
        if type(routing) is not bool:
            raise ConfigError('ФОМ: native_queue_routing должен быть true или false')
        return cls(value['category_id'], stages, routing)


class WorkSync:
    def __init__(self, store, crm, settings):
        self.store, self.db, self.crm, self.settings = store,store.db,crm,settings
        self.technicians = TechnicianDirectory(crm.api)
        store.technicians = self.technicians
        from .background import TicketLocks
        self.ticket_locks = TicketLocks()
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS fom_request_crm(
                ticket INTEGER PRIMARY KEY,crm_id INTEGER UNIQUE,stage_id TEXT,bitrix_owner INTEGER,
                moved_time TEXT,confirmed_version INTEGER NOT NULL DEFAULT -1,
                state TEXT NOT NULL DEFAULT 'pending',error TEXT);
            CREATE TABLE IF NOT EXISTS fom_request_crm_jobs(
                ticket INTEGER NOT NULL,version INTEGER NOT NULL,snapshot TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'prepared',error TEXT,PRIMARY KEY(ticket,version));
        ''')
        if 'last_checked' not in {row['name'] for row in self.db.execute('PRAGMA table_info(fom_request_crm)')}:
            self.db.execute('ALTER TABLE fom_request_crm ADD COLUMN last_checked TEXT')
        self.db.commit()
        self.contacts = SupportContact(self)
        store.crm_enabled = True

    def validate(self):
        actual = {row['id']:row for row in self.crm.stages(self.settings.category_id)}
        for key, configured in self.settings.stages.items():
            if configured['id'] not in actual or actual[configured['id']]['title'] != configured['title']:
                raise RemoteError('FOM_STAGE_CHANGED')
            expected = 'S' if key=='closed' else 'F' if key=='failed' else None
            if (actual[configured['id']].get('semantics') or None) != expected:
                raise RemoteError('FOM_STAGE_SEMANTICS_CHANGED')

    def binding(self, ident):
        row = self.db.execute('SELECT * FROM fom_request_crm WHERE ticket=?',(ident,)).fetchone()
        return dict(row) if row else None

    def ensure(self, row):
        # Historical requests are enrolled explicitly, never by an automatic migration.
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO fom_request_crm(ticket) VALUES(?)',(row['id'],))
            self.db.execute('''INSERT OR IGNORE INTO fom_request_crm_jobs(ticket,version,snapshot)
                VALUES(?,?,?)''',(row['id'],row['version'],json.dumps(row,ensure_ascii=False)))

    def state(self, ticket, value, error=None, version=None):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO fom_request_crm(ticket) VALUES(?)',(ticket,))
            self.db.execute('UPDATE fom_request_crm SET state=?,error=? WHERE ticket=?',(value,error,ticket))
            if version is not None:
                self.db.execute('UPDATE fom_request_crm_jobs SET state=?,error=? WHERE ticket=? AND version=?',
                                (value,error,ticket,version))

    def target_stage(self, row):
        key = row['status'] if row['status'] in {'working','waiting','closed','failed'} else row['queue']
        return self.settings.stages[key]['id']

    def responsible(self, row):
        technician = row.get('technician_details')
        if technician and row['owner'] is None and row['status'] in {'assigned','working','waiting','closed','failed'}:
            creator = self.store.primary.registration(row['creator'])
            if not creator or creator['status'] != 'approved' or self.store.role(row['creator']) != 'fom_sales':
                raise RemoteError('FOM_EMPLOYEE_REVOKED')
            return int(technician['bitrix_id'])
        user = row['owner'] or row['creator']
        registration = self.store.primary.registration(user)
        if not registration or registration['status']!='approved' or not self.store.role(user):
            raise RemoteError('FOM_EMPLOYEE_REVOKED')
        return int(registration['bitrix_id'])

    def check_source(self, remote, row):
        if remote.get('originatorId') != SOURCE or remote.get('originId') != row['request_id']:
            raise RemoteError('FOM_CRM_SOURCE_CHANGED')
        if int(remote.get('categoryId',-1)) != self.settings.category_id:
            raise RemoteError('FOM_CRM_CATEGORY_CHANGED')

    def find(self, row):
        records = self.crm.api.list_all('crm.deal.list',{'filter':{'ORIGINATOR_ID':SOURCE,
            'ORIGIN_ID':row['request_id']},'select':['ID'],'order':{'ID':'ASC'}})
        if len(records)>1:
            raise RemoteError('FOM_DUPLICATE_ORIGIN')
        if not records:
            return None
        remote = self.crm.item(int(records[0]['ID']))
        self.check_source(remote,row)
        return remote

    def time_block(self, row):
        def local(value):
            return timestamp(value).astimezone(self.crm.config.timezone).strftime('%d.%m.%Y %H:%M:%S %Z') if value else 'ещё нет'
        # Open-card duration is recalculated in Telegram. Only completion fixes the total in CRM.
        return (START+f"\nСделка техподдержки #{row['id']} · версия {row['version']}"
            +f"\nСоздана: {local(row['created_at'])}\nВ работе с: {local(row['work_at'])}"
            +f"\nРешена: {local(row['resolved_at'])}"
            + ('\nОт создания до решения: '+duration(elapsed(row)) if row['resolved_at'] else
               '\nОт создания до решения: счётчик в Telegram, включая ожидание и передачи')+'\n'+END)

    def comments(self, existing, row):
        # Preserve human notes and replace only the section owned by this bot.
        value = str(existing or '')
        block = self.time_block(row)
        pattern = re.escape(START)+r'.*?'+re.escape(END)
        if re.search(pattern,value,re.S):
            return re.sub(pattern,lambda match:block,value,count=1,flags=re.S)
        # Bitrix treats bracketed prose as BBCode and stripped the first pilot's delimiters.
        legacy = r'(?:Заявка ФОМ|Сделка техподдержки) #'+str(row['id'])+r' · версия \d+\nСоздана: [^\n]*\nВ работе с: [^\n]*\nРешена: [^\n]*\nОт создания до решения: [^\n]*'
        if re.search(legacy,value):
            return re.sub(legacy,lambda match:block,value,count=1)
        return value+'\n\n'+block

    def matches(self, remote, row):
        self.check_source(remote,row)
        return (remote.get('stageId')==self.target_stage(row)
                and str(remote.get('assignedById'))==str(self.responsible(row))
                and (row['version'] != 0 or support_matches(remote, row.get('support_details')))
                and self.contacts.matches(remote,row)
                and self.time_matches(remote,row))

    def time_matches(self, remote, row):
        # Delimiters were stripped as BBCode on the first real pilot. Compare the exact
        # version and timestamp content, while retaining delimiters on subsequent writes.
        content = self.time_block(row).removeprefix(START+'\n').removesuffix('\n'+END)
        existing = str(remote.get('comments','')).replace('Заявка ФОМ #','Сделка техподдержки #')
        return content in existing

    def routed(self, remote, row):
        return (self.settings.native_queue_routing and row['status'] in {'new','assigned','triage'}
                and (not row.get('technician_details') or str(remote.get('assignedById')) == str(self.responsible(row)))
                and remote.get('stageId') in {self.settings.stages[key]['id'] for key in ('dispatch','r1','r234')}
                and (row['version'] != 0 or support_matches(remote, row.get('support_details')))
                and self.contacts.matches(remote,row)
                and self.time_matches(remote,row))

    def confirm(self, row, remote):
        self.check_source(remote,row)
        if not self.matches(remote,row) and not self.routed(remote,row):
            raise RemoteError('FOM_CRM_NOT_CONFIRMED',uncertain=True)
        with self.db:
            if self.routed(remote,row):
                queue = next(key for key in ('dispatch','r1','r234') if self.settings.stages[key]['id']==remote['stageId'])
                self.db.execute('UPDATE fom_requests SET queue=? WHERE id=? AND version=?',(queue,row['id'],row['version']))
            self.db.execute('''INSERT INTO fom_request_crm(ticket,crm_id,stage_id,bitrix_owner,moved_time,
                confirmed_version,state) VALUES(?,?,?,?,?,?,'confirmed')
                ON CONFLICT(ticket) DO UPDATE SET crm_id=excluded.crm_id,stage_id=excluded.stage_id,
                bitrix_owner=excluded.bitrix_owner,moved_time=excluded.moved_time,
                confirmed_version=excluded.confirmed_version,state='confirmed',error=NULL''',
                (row['id'],int(remote['id']),remote['stageId'],int(remote['assignedById']),
                 remote.get('movedTime'),row['version']))
            self.db.execute("UPDATE fom_request_crm_jobs SET state='confirmed',error=NULL WHERE ticket=? AND version=?",
                            (row['id'],row['version']))

    def submit(self, job):
        with self.ticket_locks.get(job['ticket']):
            # A foreground submit may have confirmed the job while this worker waited.
            current = self.db.execute('SELECT * FROM fom_request_crm_jobs WHERE ticket=? AND version=?',
                                      (job['ticket'], job['version'])).fetchone()
            if not current or current['state'] in {'confirmed', 'superseded'}:
                return
            self._submit(dict(current))

    def _submit(self, job):
        row = json.loads(job['snapshot'])
        binding = self.binding(row['id'])
        submitted=False
        try:
            self.validate()
            remote = self.crm.item(binding['crm_id']) if binding and binding['crm_id'] else self.find(row)
            if remote:
                self.check_source(remote,row)
                if not binding or not binding['crm_id']:
                    with self.db:
                        self.db.execute('INSERT OR IGNORE INTO fom_request_crm(ticket) VALUES(?)',(row['id'],))
                        self.db.execute('UPDATE fom_request_crm SET crm_id=? WHERE ticket=?',(int(remote['id']),row['id']))
                if self.matches(remote,row) or self.routed(remote,row):
                    self.confirm(row,remote)
                    return
            # A sending job after restart is a read-only recovery, never a duplicate write.
            if job['state'] in {'sending','uncertain'}:
                self.state(row['id'],'uncertain','FOM_CHECK_ONLY',row['version'])
                return
            if remote and binding and binding['crm_id']:
                if (remote.get('stageId'),int(remote.get('assignedById')),remote.get('movedTime')) != (
                        binding['stage_id'],binding['bitrix_owner'],binding['moved_time']):
                    raise RemoteError('FOM_CRM_CONFLICT')
            elif remote:
                # An existing exact-origin card with unexpected data needs human reconciliation.
                raise RemoteError('FOM_CRM_CONFLICT')
            fields = {'stageId':self.target_stage(row),'assignedById':self.responsible(row)}
            if remote:
                fields['comments'] = self.comments(remote.get('comments'),row)
                self.state(row['id'],'sending',version=row['version'])
                submitted=True
                self.crm.api.call('crm.item.update',{'entityTypeId':2,'id':int(remote['id']),
                    'useOriginalUfNames':'Y','fields':fields})
                remote = self.crm.item(int(remote['id']))
            else:
                if row.get('technician_details'):
                    self.technicians.employee(row['technician_details']['bitrix_id'])
                try:
                    validate_support(row.get('support_details'), self.crm.api)
                except ValueError as exc:
                    raise RemoteError('SUPPORT_TYPE_CHANGED') from exc
                fields.update(native_fields(row.get('support_details'), row['description']))
                if row.get('link_kind')=='pharmacy':
                    source = self.crm.api.call('crm.item.get',{'entityTypeId':1034,'id':row['pharmacy_id'],'useOriginalUfNames':'Y'})['result'].get('item')
                    if not isinstance(source,dict) or str(source.get('id'))!=str(row['pharmacy_id']) or str(source.get('categoryId'))!='17' or int(source.get('companyId') or 0)!=row['pharmacy_company_id']:
                        raise RemoteError('FOM_PHARMACY_CHANGED')
                    fields['parentId1034'] = row['pharmacy_id']
                    source_label = f"Аптека: {row['deal_url']}"
                else:
                    source = self.crm.item(row['deal_id'])
                    source_label = f"Исходная сделка #{row['deal_id']}: {row['deal_url']}"
                contact = self.contacts.save(row,source)
                first_recipient = row['technician_details']['name'] if row.get('technician_details') else self.store.person(row['initial_recipient'])['name'] if row['initial_recipient'] else 'На распределение'
                note = (f"Заявка техподдержки #{row['id']}\n{source_label}"
                    +f"\nПервый получатель: {first_recipient}\n\n{row['description']}")
                details = row.get('support_details')
                if details:
                    note += '\n\nТип обращения: '+details['title']
                    if details.get('office'):
                        note += '\nТип в новой платформе: '+details['office']['title']
                    if details.get('program'):
                        note += '\nПрограмма: '+details['program']['title']
                if contact:
                    note += '\n\nКонтактное лицо аптеки: '+contact['title']+'\nТелефон: '+row['contact_details']['phone']
                title = row['title']
                if row['test'] and not title.startswith('[ТЕСТ БОТА]'):
                    title = '[ТЕСТ БОТА] '+title
                fields.update(title=title,categoryId=self.settings.category_id,originatorId=SOURCE,
                    originId=row['request_id'],comments=self.comments(note,row))
                if source.get('companyId'):
                    fields['companyId'] = source['companyId']
                if source.get('contactId'):
                    fields['contactId'] = source['contactId']
                if source.get('contactIds'):
                    fields['contactIds'] = source['contactIds']
                if contact:
                    values = source.get('contactIds') or []
                    if not isinstance(values,list):
                        raise RemoteError('INVALID_PHARMACY_CONTACTS')
                    fields['contactIds'] = list(dict.fromkeys([contact['id']] + [int(value) for value in values]
                        + ([int(source['contactId'])] if source.get('contactId') else [])))
                    fields['contactId'] = contact['id']
                self.state(row['id'],'sending',version=row['version'])
                submitted=True
                result = self.crm.api.call('crm.item.add',{'entityTypeId':2,'useOriginalUfNames':'Y','fields':fields})
                item = result.get('result',{}).get('item',{})
                if not item.get('id'):
                    raise RemoteError('FOM_INVALID_CREATE',uncertain=True)
                # Keep the returned ID before confirmation in case the read response is lost.
                with self.db:
                    self.db.execute('UPDATE fom_request_crm SET crm_id=? WHERE ticket=?',(int(item['id']),row['id']))
                remote = self.crm.item(int(item['id']))
            self.confirm(row,remote)
        except RemoteError as exc:
            uncertain=exc.uncertain and (submitted or job['state'] in {'sending','uncertain'})
            state = 'conflict' if exc.code in {'FOM_CRM_CONFLICT','FOM_CRM_SOURCE_CHANGED','FOM_CRM_CATEGORY_CHANGED'} else 'uncertain' if uncertain else 'failed'
            self.state(row['id'],state,exc.code,row['version'])

    def flush(self, ident=None):
        jobs = list(self.db.execute('''SELECT j.* FROM fom_request_crm_jobs j JOIN fom_requests t ON t.id=j.ticket
            WHERE t.archived=0 AND j.state IN ('prepared','sending') ORDER BY j.ticket,j.version'''))
        for job in jobs:
            if ident is not None and job['ticket']!=ident:
                continue
            if self.db.execute("SELECT 1 FROM fom_request_crm_jobs WHERE ticket=? AND version<? AND state NOT IN ('confirmed','superseded')",
                               (job['ticket'],job['version'])).fetchone():
                continue
            self.submit(dict(job))

    def retry(self, ident, actor):
        with self.ticket_locks.get(ident):
            return self._retry(ident, actor)

    def _retry(self, ident, actor):
        row = self.store.ticket(ident)
        if not self.store.visible(row,actor) or actor not in {row['creator'],row['owner'],*self.store.admins}:
            raise InboxError('Б24ни текшириш учун кириш йўқ.')
        if not self.binding(ident):
            self.ensure(row)
        jobs = list(self.db.execute("SELECT * FROM fom_request_crm_jobs WHERE ticket=? AND state NOT IN ('confirmed','superseded') ORDER BY version",(ident,)))
        for job in jobs:
            if job['state']=='failed':
                self.state(ident,'prepared',version=job['version'])
                job = dict(job,state='prepared')
            self.submit(dict(job))
            if self.binding(ident)['state']!='confirmed':
                return
        self.pull(ident)

    def reconcile(self, ident, actor):
        with self.ticket_locks.get(ident):
            return self._reconcile(ident, actor)

    def _reconcile(self, ident, actor):
        row = self.store.ticket(ident)
        if not self.store.visible(row,actor) or actor not in {row['owner'],*self.store.admins}:
            raise InboxError('Б24даги ҳолатни масъул ёки администратор қабул қилади.')
        binding = self.binding(ident)
        if not binding or binding['state']!='conflict':
            raise InboxError('Аввал карточкани янгиланг.')
        self.pull(ident,accept=True)

    def poll(self):
        # Rotate through five least recently checked cards each loop. No edits or repeated
        # Telegram messages when CRM is unchanged; include closed cards for manual reopen.
        rows = list(self.db.execute('''SELECT c.ticket FROM fom_request_crm c JOIN fom_requests t ON t.id=c.ticket
            WHERE t.archived=0 AND c.state='confirmed'
            AND (c.last_checked IS NULL OR c.last_checked<datetime('now','-60 seconds'))
            ORDER BY coalesce(c.last_checked,''),c.ticket LIMIT 5'''))
        for row in rows:
            try:
                self.pull(row['ticket'])
            except (RemoteError, InboxError):
                break

    def first_work_time(self, ident, created_at):
        rows = self.crm.api.list_all('crm.stagehistory.list',{'entityTypeId':2,
            'filter':{'OWNER_ID':ident},'order':{'ID':'ASC'},
            'select':['OWNER_ID','CATEGORY_ID','STAGE_ID','CREATED_TIME']},key='items')
        times = [timestamp(row['CREATED_TIME']) for row in rows
            if str(row.get('OWNER_ID'))==str(ident) and str(row.get('CATEGORY_ID'))==str(self.settings.category_id)
            and row.get('STAGE_ID')==self.settings.stages['working']['id']
            and row.get('CREATED_TIME') and timestamp(row['CREATED_TIME'])>=timestamp(created_at)]
        return min(times).isoformat() if times else None

    def pull(self, ident, *, accept=False):
        with self.ticket_locks.get(ident):
            return self._pull(ident, accept=accept)

    def _pull(self, ident, *, accept=False):
        """Read current CRM state; never overwrite a change made by an employee in Bitrix."""
        binding = self.binding(ident)
        if not binding or not binding['crm_id'] or (binding['state']!='confirmed' and not accept):
            return
        row = self.store.ticket(ident)
        if row.get('archived'):
            return
        try:
            remote = self.crm.item(binding['crm_id'])
            with self.db:
                self.db.execute('UPDATE fom_request_crm SET last_checked=CURRENT_TIMESTAMP WHERE ticket=?',(ident,))
            self.check_source(remote,row)
            technician = row.get('technician_details')
            connected = self.store.connected_technician(technician['bitrix_id']) if technician else None
            attaching = bool(technician and row['owner'] is None and connected)
            if not accept and (remote.get('stageId'),int(remote.get('assignedById')),remote.get('movedTime')) == (
                    binding['stage_id'],binding['bitrix_owner'],binding['moved_time']) and not attaching:
                return
            if not accept and self.db.execute("SELECT 1 FROM fom_request_crm_jobs WHERE ticket=? AND state NOT IN ('confirmed','superseded')",(ident,)).fetchone():
                raise RemoteError('FOM_CRM_CONFLICT')
            keys = {value['id']:key for key,value in self.settings.stages.items()}
            key = keys.get(remote.get('stageId'))
            candidates = [member for member in self.store.members() if int(member['bitrix_id'])==int(remote['assignedById'])]
            queued = key in {'dispatch','r1','r234'} and self.settings.native_queue_routing
            external = bool(technician and str(remote.get('assignedById'))==str(technician['bitrix_id']))
            if external:
                candidates = [member for member in candidates if member['role']=='tech']
            if not key or (len(candidates)!=1 and not queued and not external):
                raise RemoteError('FOM_CRM_OWNER_NOT_CONNECTED')
            owner = candidates[0]['telegram_id'] if external and len(candidates)==1 else None if external else row['owner'] if queued else candidates[0]['telegram_id']
            status = key if key in {'working','waiting','closed','failed'} else 'assigned'
            if queued and row['owner'] is None and not external:
                status,owner = 'new',None
            if technician and not external:
                technician = None
            when = remote.get('movedTime')
            if when:
                when = timestamp(when).astimezone(timestamp(row['created_at']).tzinfo).isoformat()
            # A portal without movedTime cannot supply an exact manual completion timestamp.
            if status in {'working','closed','failed'} and not when:
                raise RemoteError('FOM_CRM_STAGE_TIME_MISSING')
            first_work = row['work_at']
            if not first_work:
                # A rapid manual work→wait→complete sequence can fall between two polls.
                # Stage history preserves the first work timestamp without estimating it.
                first_work = self.first_work_time(binding['crm_id'],row['created_at'])
            self.db.execute('BEGIN IMMEDIATE')
            try:
                current = self.store.ticket(ident)
                if current['version']!=row['version'] or (not accept and self.db.execute("SELECT 1 FROM fom_request_crm_jobs WHERE ticket=? AND state NOT IN ('confirmed','superseded')",(ident,)).fetchone()):
                    raise InboxError('Карточка ўзгарди. Янгиланг.')
                work_at = current['work_at'] or first_work or (when if status=='working' else None)
                resolved_at = when if status in {'closed','failed'} else None
                queue = key if key in {'dispatch','r1','r234'} else current['queue']
                self.db.execute('''UPDATE fom_requests SET owner=?,status=?,queue=?,work_at=?,resolved_at=?,technician_details=?,
                    version=version+1,updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                    (owner,status,queue,work_at,resolved_at,json.dumps(technician,ensure_ascii=False) if technician else None,ident))
                after = self.store.ticket(ident)
                if accept:
                    self.db.execute("UPDATE fom_request_crm_jobs SET state='superseded',error='REPLACED_BY_CRM' WHERE ticket=? AND state NOT IN ('confirmed','superseded')",(ident,))
                self.store._event(after,row['creator'] if attaching else owner or row['creator'],'crm',row['owner'],{row['creator'],row['owner'],owner})
                self.db.execute('''UPDATE fom_request_crm SET stage_id=?,bitrix_owner=?,moved_time=?,
                    confirmed_version=?,state='confirmed',error=NULL WHERE ticket=?''',(remote['stageId'],int(remote['assignedById']),remote.get('movedTime'),after['version'],ident))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        except RemoteError as exc:
            if exc.code in {'CONNECTION_ERROR','INVALID_JSON','INVALID_RESPONSE'} or exc.code.startswith('HTTP_'):
                raise
            self.state(ident,'conflict',exc.code)

    def text(self, row):
        binding = self.binding(row['id'])
        if not binding:
            return tr('\n📌 Б24 техник ёрдам заявкаси ҳали уланмаган. «Б24ни текшириш» билан уланг.')
        text = ''
        if binding['crm_id']:
            url = self.crm.portal+f"/crm/deal/details/{binding['crm_id']}/"
            text += f'\n🛠 <a href="{html.escape(url,quote=True)}">'+tr('Б24 техник ёрдам заявкаси #')+str(binding['crm_id'])+'</a>'
        if binding['state']=='confirmed':
            name = next((value['title'] for value in self.settings.stages.values() if value['id']==binding['stage_id']),binding['stage_id'])
            text += tr('\nБ24 босқичи: ')+html.escape(name)
            if row.get('contact_details'):
                step=self.contacts.step(row['id'],'contact')
                if step and step['state']=='confirmed':
                    contact=json.loads(step['result'])
                    url=self.crm.portal+f"/crm/contact/details/{contact['id']}/"
                    text+='\n👤 <a href="'+html.escape(url,quote=True)+'">'+tr('Посмотреть контакт в Б24')+'</a>'
            if row['owner'] and str(self.store.primary.registration(row['owner'])['bitrix_id'])!=str(binding['bitrix_owner']):
                text += tr('\n🧭 Б24 навбат масъули: ID ')+str(binding['bitrix_owner'])+tr('. Ботдаги қабул қилувчи алоҳида кўрсатилган.')
        else:
            contact_errors = {
                'FOM_TECHNICIAN_CHANGED':'Этот техник больше недоступен. Выберите другого техника или оставьте заявку в очереди.',
                'FOM_TECHNICIAN_STRUCTURE_CHANGED':'Не удалось загрузить техников из Б24. Обновите список или оставьте заявку в очереди.',
                'FOM_CONTACT_AMBIGUOUS':'По этому номеру в Б24 несколько контактов. Уточните дубли в Б24 и нажмите «Проверить Б24». Новый контакт не создаём.',
                'FOM_CONTACT_CHECK_ONLY':'Сохранение контактного лица ещё не подтверждено. Нажмите «Проверить Б24» позже — повторно контакт не создаём.',
                'FOM_CONTACT_CHANGED':'Телефон или связь контакта в Б24 изменились. Уточните контакт в Б24 и повторите проверку.',
                'FOM_CONTACT_OTHER_PENDING':'Другой запрос ещё проверяет создание контакта с этим номером. Дождитесь проверки и нажмите «Проверить Б24».',
            }
            if binding.get('error') in contact_errors:
                text += '\n⚠️ '+tr(contact_errors[binding['error']])
            else:
                text += tr('\n⚠️ Б24 сақлаши тасдиқланмади: ')+html.escape(binding['state'])+' · '+html.escape(binding['error'] or '')
            text += tr('\nЗаявка ботда сақланган. «Б24ни текшириш»ни босинг.')
        return text
