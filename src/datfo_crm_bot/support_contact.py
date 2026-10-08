"""Optional pharmacy contact for a support request; each CRM write has a journal."""
import html
import json
import re

from .api import RemoteError
from .config import positive_id
from .i18n import tr
from .service import normalize_phone

SOURCE = 'datfo_telegram_okb'


def phone(value):
    if not isinstance(value, str) or len(value) > 30:
        raise ValueError(tr('Укажите телефон контактного лица. Можно без +998.'))
    value = normalize_phone(value)
    return '+' + re.sub(r'\D', '', value)


def collect(data):
    choice = data.get('support_contact', 'pharmacy')
    if choice == 'pharmacy':
        return None  # Ignore fields left over after switching the choice back.
    if choice != 'person':
        raise ValueError(tr('Выберите контакты аптеки или укажите контактное лицо.'))
    name = data.get('support_contact_name')
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100 or not any(char.isalpha() for char in name):
        raise ValueError(tr('Укажите имя контактного лица. Фамилия необязательна.'))
    return {'name': name.strip(), 'phone': phone(data.get('support_contact_phone'))}


def preview(details, *, pending=True):
    if not details:
        return ''
    return (tr('👤 <b>Контактное лицо аптеки</b>') + '\n' + html.escape(details['name'])
            + '\n📞 ' + html.escape(details['phone'])
            + ('\n'+tr('Найдём контакт по телефону или создадим новый на вас в Б24 и привяжем к заявке.') if pending else ''))


class SupportContact:
    def __init__(self, sync):
        self.sync, self.db, self.api = sync, sync.db, sync.crm.api
        self.db.executescript('''CREATE TABLE IF NOT EXISTS fom_support_contact_steps(
            ticket INTEGER NOT NULL, phase TEXT NOT NULL, phone TEXT NOT NULL,
            state TEXT NOT NULL, result TEXT, PRIMARY KEY(ticket,phase));
            CREATE INDEX IF NOT EXISTS fom_support_contact_phone ON fom_support_contact_steps(phone,state);
        ''')
        from .background import TicketLocks
        self.locks = TicketLocks()

    def contacts(self, number):
        result = self.api.call('crm.duplicate.findbycomm',
            {'entity_type':'CONTACT','type':'PHONE','values':[number]})['result']
        if result == []:
            return []
        if not isinstance(result, dict) or not isinstance(result.get('CONTACT', []), list):
            raise RemoteError('INVALID_DUPLICATE_RESPONSE')
        if len(result.get('CONTACT', [])) > 20:
            raise RemoteError('TOO_MANY_CONTACT_DUPLICATES')
        ids = {positive_id(value, 'Контакт') for value in result.get('CONTACT', [])}
        if not ids:
            return []
        rows = self.api.list_all('crm.contact.list', {'filter':{'@ID':sorted(ids)},
            'select':['ID','NAME','LAST_NAME','PHONE']})
        if {positive_id(row['ID'], 'Контакт') for row in rows} != ids:
            raise RemoteError('CONTACT_LOOKUP_INCOMPLETE')
        return [self.value(row) for row in rows if self.has_phone(row, number)]

    @staticmethod
    def has_phone(row, number):
        values = row.get('PHONE', [])
        if not isinstance(values, list):
            raise RemoteError('INVALID_CONTACT_PHONE')
        for entry in values:
            if not isinstance(entry,dict):
                raise RemoteError('INVALID_CONTACT_PHONE')
            try:
                if phone(entry.get('VALUE')) == number:
                    return True
            except ValueError:
                continue
        return False

    @staticmethod
    def value(row):
        return {'id':positive_id(row['ID'], 'Контакт'),
                'title':' '.join(str(row.get(key) or '') for key in ('NAME','LAST_NAME')).strip() or 'Контакт'}

    def step(self, ticket, phase):
        row = self.db.execute('SELECT * FROM fom_support_contact_steps WHERE ticket=? AND phase=?',
                              (ticket,phase)).fetchone()
        return dict(row) if row else None

    def state(self, row, phase, state, result=None):
        with self.db:
            self.db.execute('''INSERT INTO fom_support_contact_steps(ticket,phase,phone,state,result)
                VALUES(?,?,?,?,?) ON CONFLICT(ticket,phase) DO UPDATE SET state=excluded.state,result=excluded.result''',
                (row['id'],phase,row['contact_details']['phone'],state,
                 json.dumps(result,ensure_ascii=False) if result else None))

    def phase(self, row, name, find, create, *, preflight=None):
        previous = self.step(row['id'], name)
        if previous and previous['state'] == 'confirmed':
            return json.loads(previous['result'])
        found = find()
        if found is not None:
            self.state(row, name, 'confirmed', found)
            return found
        if previous and previous['state'] in {'sending','uncertain'}:
            raise RemoteError('FOM_CONTACT_CHECK_ONLY', uncertain=True)
        if preflight:
            found=preflight()
            if found is not None:
                self.state(row,name,'confirmed',found)
                return found
        self.state(row, name, 'sending')
        try:
            value = create()
        except RemoteError as exc:
            self.state(row, name, 'uncertain' if exc.uncertain else 'rejected')
            if exc.uncertain:
                raise RemoteError('FOM_CONTACT_CHECK_ONLY', uncertain=True) from exc
            raise
        self.state(row, name, 'confirmed', value)
        return value

    def find(self, row):
        origin = row['request_id']+'-contact'
        values = self.api.list_all('crm.contact.list', {'filter':{'ORIGINATOR_ID':SOURCE,'ORIGIN_ID':origin},
            'select':['ID','NAME','LAST_NAME','PHONE','ORIGINATOR_ID','ORIGIN_ID']})
        values = [value for value in values if value.get('ORIGINATOR_ID') == SOURCE and value.get('ORIGIN_ID') == origin]
        if len(values) > 1:
            raise RemoteError('FOM_DUPLICATE_CONTACT_ORIGIN')
        if values:
            if not self.has_phone(values[0], row['contact_details']['phone']):
                raise RemoteError('FOM_CONTACT_CHANGED')
            return {**self.value(values[0]), 'created':True}
        # A lost contact-add response is checked by origin; never repeat that add.
        previous = self.step(row['id'], 'contact')
        if previous and previous['state'] in {'sending','uncertain'}:
            return None
        matches = self.contacts(row['contact_details']['phone'])
        if len(matches) > 1:
            raise RemoteError('FOM_CONTACT_AMBIGUOUS')
        return {**matches[0], 'created':False} if matches else None

    def prepare_contact(self, row):
        fields = self.api.call('crm.contact.fields')['result']
        required = {'NAME','PHONE','ASSIGNED_BY_ID','ORIGINATOR_ID','ORIGIN_ID','OPENED'}
        if not isinstance(fields, dict) or any(not isinstance(fields.get(key), dict) or fields[key].get('isReadOnly') for key in required):
            raise RemoteError('FOM_CONTACT_FIELDS_CHANGED')
        registration = self.sync.store.primary.registration(row['creator'])
        if not registration or registration['status'] != 'approved' or self.sync.store.role(row['creator']) != 'fom_sales':
            raise RemoteError('FOM_EMPLOYEE_REVOKED')
        # Recheck phone immediately before the add, including a concurrent external creation.
        matches = self.contacts(row['contact_details']['phone'])
        if len(matches) > 1:
            raise RemoteError('FOM_CONTACT_AMBIGUOUS')
        if matches:
            return {**matches[0], 'created':False}
        return None

    def create(self, row):
        registration = self.sync.store.primary.registration(row['creator'])
        details = row['contact_details']
        result = self.api.call('crm.contact.add', {'fields':{'NAME':details['name'],
            'PHONE':[{'VALUE':details['phone'],'VALUE_TYPE':'WORK'}],
            'ASSIGNED_BY_ID':int(registration['bitrix_id']),'OPENED':'N',
            'ORIGINATOR_ID':SOURCE,'ORIGIN_ID':row['request_id']+'-contact'}})['result']
        if isinstance(result, bool) or not str(result).isascii() or not str(result).isdigit() or int(result) <= 0:
            raise RemoteError('FOM_INVALID_CONTACT_CREATE', uncertain=True)
        return {'id':int(result),'title':details['name'],'created':True}

    def company_link(self, ident, company):
        values = self.api.call('crm.contact.company.items.get', {'id':ident})['result']
        if not isinstance(values, list):
            raise RemoteError('INVALID_CONTACT_BINDINGS')
        return {'linked':True} if any(str(value.get('COMPANY_ID')) == str(company) for value in values) else None

    def add_company(self, ident, company):
        result = self.api.call('crm.contact.company.add', {'id':ident,'fields':{'COMPANY_ID':company,'IS_PRIMARY':'N'}})['result']
        if result is not True:
            raise RemoteError('INVALID_LINK_RESPONSE', uncertain=True)
        return {'linked':True}

    def save(self, row, source):
        details = row.get('contact_details')
        if not details:
            return None
        registration = self.sync.store.primary.registration(row['creator'])
        if not registration or registration['status'] != 'approved' or self.sync.store.role(row['creator']) != 'fom_sales':
            raise RemoteError('FOM_EMPLOYEE_REVOKED')
        if collect({'support_contact':'person','support_contact_name':details.get('name'),
                    'support_contact_phone':details.get('phone')}) != details:
            raise RemoteError('FOM_CONTACT_DETAILS_CHANGED')
        with self.locks.get(details['phone']):
            pending = self.db.execute('''SELECT 1 FROM fom_support_contact_steps
                WHERE ticket<>? AND phone=? AND phase='contact' AND state IN ('sending','uncertain') LIMIT 1''',
                (row['id'],details['phone'])).fetchone()
            if pending:
                raise RemoteError('FOM_CONTACT_OTHER_PENDING')
            if self.sync.store.primary.other_inflight(row['request_id'], 'phone', details['phone'], phase='contact'):
                raise RemoteError('FOM_CONTACT_OTHER_PENDING')
            item_fields = self.api.call('crm.item.fields', {'entityTypeId':2,'useOriginalUfNames':'Y'})['result'].get('fields', {})
            if not isinstance(item_fields.get('contactIds'),dict) or item_fields['contactIds'].get('isReadOnly'):
                raise RemoteError('FOM_CONTACT_FIELDS_CHANGED')
            contact = self.phase(row, 'contact', lambda:self.find(row), lambda:self.create(row),
                                 preflight=lambda:self.prepare_contact(row))
            current = self.api.call('crm.contact.get', {'id':contact['id']})['result']
            if not isinstance(current,dict) or str(current.get('ID')) != str(contact['id']) or not self.has_phone(current, details['phone']):
                raise RemoteError('FOM_CONTACT_CHANGED')
            if contact['created'] and (current.get('ORIGINATOR_ID'),current.get('ORIGIN_ID')) != (SOURCE,row['request_id']+'-contact'):
                raise RemoteError('FOM_CONTACT_CHANGED')
            contact['title'] = self.value(current)['title']
            company = int(source.get('companyId') or 0)
            if company:
                self.phase(row, 'company', lambda:self.company_link(contact['id'],company),
                           lambda:self.add_company(contact['id'],company))
            return contact

    def matches(self, remote, row):
        if not row.get('contact_details') or row['version'] != 0:
            return True
        step = self.step(row['id'], 'contact')
        if not step or step['state'] != 'confirmed':
            return False
        ident = json.loads(step['result'])['id']
        values = remote.get('contactIds') or []
        return str(ident) == str(remote.get('contactId')) or (isinstance(values,list) and str(ident) in {str(value) for value in values})
