"""Preview catalogue: exact INN lookup and read-only Bitrix methods only."""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import json
import re
import sys
import threading
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from datfo_crm_bot.api import Bitrix, RemoteError
from datfo_crm_bot.config import load_env, connection_from_env
from datfo_crm_bot.crm import Crm
from datfo_crm_bot.okb_crm import OkbSettings
from datfo_crm_bot.sales_intake import FOM_ID_FIELD
from datfo_crm_bot.service import normalize_phone

REFERENCE = json.loads((ROOT / 'webapp/reference.json').read_text(encoding='utf-8'))
READ_METHODS = frozenset({'crm.requisite.list', 'crm.company.list', 'crm.item.list', 'crm.item.fields'})


def inn_value(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{9}', value.strip()):
        raise ValueError('Введите ИНН из 9 цифр.')
    return value.strip()


class ReadOnlyBitrix(Bitrix):
    def call(self, method, payload=None):
        if method not in READ_METHODS:
            raise ValueError('В тестовом поиске доступны только методы чтения Б24.')
        return super().call(method, payload)


class LiveCatalogue:
    def __init__(self, api, settings):
        self.api, self.settings = api, settings
        self.crm = Crm(SimpleNamespace(webhook=api.base), api)
        self.statuses = None

    @classmethod
    def local_connection(cls, private_root):
        load_env(private_root / '.env')
        _, webhook = connection_from_env(live=True)
        return cls(ReadOnlyBitrix(webhook, read_timeout=12), OkbSettings.load(private_root / 'okb-settings.json'))

    def lookup(self, inn):
        companies = self.crm.companies(inn_value(inn))
        target = self.settings.pharmacy
        if companies and self.statuses is None:
            result = self.api.call('crm.item.fields', {'entityTypeId':target.entity_type_id, 'useOriginalUfNames':'Y'})['result']
            meta = result.get('fields', {}).get(target.fields['status'], {})
            self.statuses = {str(item['ID'] if 'ID' in item else item['id']):str(item.get('VALUE',item.get('value','')))
                             for item in meta.get('items', [])}
        results = []
        for company in companies:
            rows = self.api.list_all('crm.item.list', {'entityTypeId':target.entity_type_id,
                'useOriginalUfNames':'Y', 'filter':{'categoryId':target.category_id,'companyId':company['id']},
                'select':['*'], 'order':{'id':'ASC'}}, key='items')
            pharmacies = []
            seen = set()
            for row in rows:
                # Do not count unrelated entries if the portal ignores a filter.
                if str(row.get('companyId')) != str(company['id']) or str(row.get('categoryId')) != str(target.category_id):
                    continue
                ident = int(row['id'])
                if ident in seen:
                    continue
                seen.add(ident)
                value = row.get(target.fields['status'])
                if isinstance(value,list):
                    value = value[0] if value else ''
                status = (self.statuses or {}).get(str(value), 'Статус не указан')
                pharmacies.append({'id':ident,'fom_id':str(row.get(FOM_ID_FIELD) or ''),
                    'title':str(row.get('title') or 'Аптека'), 'address':str(row.get(target.fields['address']) or ''),
                    'status':status, 'inactive':bool(re.search(r'закрыт|не работ',status,re.I))})
            results.append({**company,'inn':inn,'pharmacies':pharmacies,'pharmacy_count':len(pharmacies)})
        return results


class DemoCatalogue:
    def lookup(self, inn):
        inn = inn_value(inn)
        pharmacy = lambda ident, fom, title, address, status: {'id':ident,'fom_id':fom,'title':title,
            'address':address,'status':status,'inactive':status in {'Закрыта','Не работаем'}}
        examples = {
            '300000101':[{'id':101,'title':'ООО «Саломат Фарм»','inn':inn,'pharmacies':[
                pharmacy(10101,'00081','Саломат Фарм — Мирабад','Ташкент, Мирабадский район, ул. Мирабад, 12','Работает'),
                pharmacy(10102,'00125','Саломат Фарм — Юнусабад','Ташкент, Юнусабадский район, квартал 4','Потенциальная'),
                pharmacy(10103,'00160','Саломат Фарм — Чиланзар','Ташкент, Чиланзарский район, квартал 9','Закрыта')]}],
            '300000102':[{'id':102,'title':'ИП «Здоровье»','inn':inn,'pharmacies':[]}],
            '300000103':[{'id':103,'title':'ООО «Мед Сервис»','inn':inn,'pharmacies':[
                pharmacy(10301,'00315','Мед Сервис — Центр','Самарканд, ул. Навои, 18','Работает')]},
                {'id':104,'title':'ООО «Мед Сервис — филиал»','inn':inn,'pharmacies':[]}],
        }
        return [{**company,'pharmacy_count':len(company['pharmacies'])} for company in deepcopy(examples.get(inn, []))]


class PreviewCatalogue:
    def __init__(self, live=None):
        self.providers = {'demo':DemoCatalogue()}
        if live:
            self.providers['crm'] = live
        self.cache = {}
        self.lock = threading.Lock()

    def lookup(self, source, inn):
        inn = inn_value(inn)
        if source not in self.providers:
            raise ValueError('Подключение к Б24 недоступно. Можно проверить учебные примеры.')
        with self.lock:
            cached = self.cache.get((source,inn))
            if not cached or time.monotonic() - cached[0] > 60:
                companies = self.providers[source].lookup(inn)
                self.cache[(source,inn)] = (time.monotonic(),companies)
            else:
                companies = cached[1]
        return deepcopy(companies)

    def preview(self, data):
        """Validate the proposed binding without creating any CRM entities."""
        if not isinstance(data,dict) or data.get('mode') not in {'deal','support'}:
            raise ValueError('Выберите сделку или заявку.')
        inn = inn_value(data.get('inn'))
        companies = self.lookup(data.get('source'),inn)
        target = data.get('target')
        if target not in {'company','pharmacy','new_pharmacy'}:
            raise ValueError('Выберите фирму или аптеку.')
        if companies:
            company = next((row for row in companies if str(row['id']) == str(data.get('company_id'))),None)
            if not company:
                raise ValueError('Фирма не найдена среди результатов этого ИНН. Повторите поиск.')
            create_company = False
        else:
            name = data.get('company_name')
            if data.get('create_company') is not True or not isinstance(name,str) or not 2 <= len(name.strip()) <= 120:
                raise ValueError('Подтвердите создание фирмы и заполните её наименование.')
            company = {'id':None,'title':name.strip(),'inn':inn,'pharmacies':[]}
            create_company = True
        pharmacy = None
        if target == 'pharmacy':
            pharmacy = next((row for row in company['pharmacies'] if str(row['id']) == str(data.get('pharmacy_id'))),None)
            if not pharmacy:
                raise ValueError('Эта аптека не относится к выбранной фирме. Выберите аптеку из списка.')
            if data['mode']=='deal' and pharmacy['inactive'] and data.get('confirm_inactive') is not True:
                raise ValueError('Подтвердите открытие сделки на закрытую или неработающую аптеку.')
        elif target == 'new_pharmacy':
            value = data.get('new_pharmacy')
            if not isinstance(value,dict):
                raise ValueError('Заполните данные новой аптеки.')
            for key,label in (('title','Название аптеки'),('address','Адрес')):
                if not isinstance(value.get(key),str) or not 2 <= len(value[key].strip()) <= 500:
                    raise ValueError('Заполните поле: '+label)
            region = next((row for row in REFERENCE['regions'] if str(row['id'])==str(value.get('region'))),None)
            city = next((row for row in REFERENCE['cities'].get(str(value.get('region')),[]) if str(row['id'])==str(value.get('city'))),None)
            program = next((row for row in REFERENCE['programs'] if str(row['id'])==str(value.get('program'))),None)
            if not region or not city or not program:
                raise ValueError('Выберите регион, город/район и текущую программу новой аптеки.')
            pharmacy = {'id':None,'fom_id':'','title':value['title'].strip(),'address':value['address'].strip(),
                'region':region['title'],'city':city['title'],'program':program['title'],'status':'Потенциальная'}
        result = {'mode':data['mode'],'target':target,'inn':inn,
            'company':{'id':company['id'],'title':company['title'],'create':create_company},
            'pharmacy':pharmacy,'crm_writes':False}
        if data['mode']=='deal':
            chosen = data.get('discussion_programs',[])
            if not isinstance(chosen,list) or any(not isinstance(item,str) for item in chosen):
                raise ValueError('Выберите программы для обсуждения с клиентом.')
            programs = [row for row in REFERENCE['discussion_programs'] if row['id'] in chosen]
            if not isinstance(chosen,list) or not chosen or len(programs)!=len(set(chosen)):
                raise ValueError('Выберите программы для обсуждения с клиентом.')
            result.update(title='План: '+(pharmacy or company)['title'],programs=[row['title'] for row in programs],
                          pipeline='Продажи и онбординг [KG]',stage='План')
            if data.get('reminder'):
                reminder = data['reminder']
                if not isinstance(reminder,dict) or not isinstance(reminder.get('action'),str) or not 1<=len(reminder['action'].strip())<=500:
                    raise ValueError('Напишите, о чём напомнить.')
                try:
                    due = datetime.fromisoformat(reminder['deadline'])
                    if due.tzinfo or due.minute not in {0,30} or due.second or due.microsecond:
                        raise ValueError
                    due = due.replace(tzinfo=ZoneInfo('Asia/Tashkent'))
                except (ValueError,TypeError,KeyError):
                    raise ValueError('Выберите дату и время напоминания с шагом 30 минут.') from None
                if due<=datetime.now(ZoneInfo('Asia/Tashkent')):
                    raise ValueError('Напоминание должно быть в будущем. Выберите другую дату или время.')
                result['reminder'] = {'action':reminder['action'].strip(),'deadline':due.isoformat(),
                                      'display':due.strftime('%d.%m.%Y · %H:%M')+' · Ташкент'}
        else:
            support_type = next((row for row in REFERENCE['support']['types'] if row['id']==data.get('support_type')),None)
            if not support_type:
                raise ValueError('Выберите тип обращения.')
            program = data.get('support_program')
            if support_type.get('requires_program') and program not in support_type.get('program_ids',[]):
                raise ValueError('Выберите программу, к которой относится обращение.')
            title,description = data.get('request_title'),data.get('request_description')
            if not isinstance(title,str) or not 3<=len(title.strip())<=100 or not isinstance(description,str) or not 5<=len(description.strip())<=1200:
                raise ValueError('Заполните тему заявки и опишите, какая помощь нужна.')
            result.update(title=title.strip(),description=description.strip(),support_type=support_type['title'],
                          pipeline='Техобслуживание [KG]',stage='Распределитель')
        if data.get('contact'):
            contact = data['contact']
            if not isinstance(contact,dict) or not isinstance(contact.get('name'),str) or not 1<=len(contact['name'].strip())<=100:
                raise ValueError('Введите имя контактного лица.')
            result['contact'] = {'name':contact['name'].strip(),'phone':normalize_phone(str(contact.get('phone','')))}
        return result
