"""Exact, read-only company and pharmacy catalogue for authenticated Mini Apps."""
import re
from types import SimpleNamespace

from .api import Bitrix, RemoteError
from .crm import Crm
from .config import positive_id

FOM_ID_FIELD = 'UF_CRM_8_1719398102513'
READ_METHODS = frozenset({'crm.requisite.list', 'crm.company.list', 'crm.item.list', 'crm.item.fields'})


def inn_value(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{9}', value.strip()):
        raise ValueError('Введите ИНН из 9 цифр.')
    return value.strip()


class ReadOnlyBitrix(Bitrix):
    def call(self, method, payload=None):
        if method not in READ_METHODS:
            raise ValueError('Метод недоступен в поиске.')
        return super().call(method, payload)


class InnCatalogue:
    def __init__(self, api, settings):
        self.api, self.settings = api, settings
        self.crm = Crm(SimpleNamespace(webhook=api.base), api)

    def lookup(self, value):
        inn = inn_value(value)
        companies = self.crm.companies(inn)
        if len(companies) > 20:
            raise RemoteError('TOO_MANY_COMPANIES_FOR_INN')
        target = self.settings.pharmacy
        statuses = {}
        if companies:
            result = self.api.call('crm.item.fields', {'entityTypeId':target.entity_type_id,
                'useOriginalUfNames':'Y'})['result']
            if not isinstance(result, dict) or not isinstance(result.get('fields'), dict):
                raise RemoteError('INVALID_PHARMACY_FIELDS')
            meta = result['fields'].get(target.fields['status'], {})
            if not isinstance(meta,dict) or not isinstance(meta.get('items',[]),list):
                raise RemoteError('INVALID_PHARMACY_FIELDS')
            statuses = {str(item.get('ID', item.get('id'))):str(item.get('VALUE', item.get('value', '')))
                        for item in meta.get('items', [])}
        results = []
        for company in companies:
            rows = self.api.list_all('crm.item.list', {'entityTypeId':target.entity_type_id,
                'useOriginalUfNames':'Y', 'filter':{'categoryId':target.category_id,'companyId':company['id']},
                'select':['*'], 'order':{'id':'ASC'}}, key='items')
            pharmacies, seen = [], set()
            for row in rows:
                if str(row.get('companyId')) != str(company['id']) or str(row.get('categoryId')) != str(target.category_id):
                    continue
                ident = positive_id(row.get('id'), 'Аптека')
                if ident in seen:
                    continue
                seen.add(ident)
                status = statuses.get(str(row.get(target.fields['status'])), 'Статус не указан')
                pharmacies.append({'id':ident,'fom_id':str(row.get(FOM_ID_FIELD) or ''),
                    'title':str(row.get('title') or 'Аптека'), 'address':str(row.get(target.fields['address']) or ''),
                    'status':status,'inactive':bool(re.search(r'закрыт|не работ',status,re.I))})
            results.append({**company,'inn':inn,'pharmacies':pharmacies,'pharmacy_count':len(pharmacies)})
        return results
