import unittest
from types import SimpleNamespace
from unittest.mock import patch

from catalog import DemoCatalogue,LiveCatalogue,PreviewCatalogue,ReadOnlyBitrix,REFERENCE,inn_value,RemoteError


def payload(**changes):
    return {'mode':'deal','source':'demo','inn':'300000101','target':'company','company_id':101,
            'discussion_programs':[REFERENCE['discussion_programs'][0]['id']],**changes}


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.catalogue=PreviewCatalogue()

    def test_title_only_does_not_need_inn_or_query_any_catalogue(self):
        result=self.catalogue.preview({'mode':'deal','target':'title','deal_title':'Обсудить программу',
            'discussion_programs':[REFERENCE['discussion_programs'][0]['id']]})
        self.assertEqual(result['title'],'Обсудить программу')
        self.assertIsNone(result['company'])
        self.assertIsNone(result['pharmacy'])
        self.assertEqual(result['inn'],'')
        self.assertFalse(result['crm_writes'])

    def test_title_only_rejects_invalid_name_and_support_mode(self):
        for mode,name in (('support','Помощь'),('deal',''),('deal','x'),('deal','/cancel'),('deal','x'*121)):
            with self.assertRaises(ValueError):
                self.catalogue.preview({'mode':mode,'target':'title','deal_title':name,
                    'discussion_programs':[REFERENCE['discussion_programs'][0]['id']]})

    def test_exact_inn_and_number_of_pharmacies(self):
        self.assertEqual(self.catalogue.lookup('demo','300000101')[0]['pharmacy_count'],3)
        self.assertEqual(self.catalogue.lookup('demo','300000102')[0]['pharmacy_count'],0)
        self.assertEqual(self.catalogue.lookup('demo','300000199'),[])
        self.assertEqual(len(self.catalogue.lookup('demo','300000103')),2)
        self.assertEqual(inn_value(' 000000081 '),'000000081')
        for value in ('81','30000010x',300000101):
            with self.assertRaises(ValueError):
                inn_value(value)

    def test_company_target_has_no_pharmacy_and_inactive_pharmacy_requires_consent(self):
        result=self.catalogue.preview(payload())
        self.assertEqual(result['company']['id'],101)
        self.assertIsNone(result['pharmacy'])
        self.assertFalse(result['crm_writes'])
        with self.assertRaises(ValueError):
            self.catalogue.preview(payload(target='pharmacy',pharmacy_id=10103))
        result=self.catalogue.preview(payload(target='pharmacy',pharmacy_id=10103,confirm_inactive=True))
        self.assertEqual(result['pharmacy']['fom_id'],'00160')

    def test_wrong_company_and_wrong_pharmacy_cannot_be_bound_to_the_inn(self):
        for value in (payload(company_id=102),payload(target='pharmacy',pharmacy_id=10301),
                      payload(inn='300000103',company_id=None),payload(source='missing')):
            with self.assertRaises(ValueError):
                self.catalogue.preview(value)

    def test_missing_company_requires_explicit_consent_and_name(self):
        for changes in ({},{'create_company':False},{'create_company':True,'company_name':''}):
            with self.assertRaises(ValueError):
                self.catalogue.preview(payload(inn='300000199',company_id=None,**changes))
        result=self.catalogue.preview(payload(inn='300000199',company_id=None,create_company=True,company_name='ООО Новая фирма'))
        self.assertTrue(result['company']['create'])
        self.assertIsNone(result['company']['id'])
        self.assertIsNone(result['pharmacy'])

    def test_support_accepts_company_and_pharmacy_without_requiring_sales_programs(self):
        for target,ident in (('company',None),('pharmacy',10103)):
            result=self.catalogue.preview(payload(mode='support',target=target,pharmacy_id=ident,
                support_type=REFERENCE['support']['unknown_id'],request_title='Не открывается программа',
                request_description='При запуске появляется ошибка подключения.'))
            self.assertEqual(result['mode'],'support')
            self.assertNotIn('programs',result)

    def test_new_pharmacy_validates_linked_region_city_and_program(self):
        region=next(row for row in REFERENCE['regions'] if REFERENCE['cities'].get(row['id']))
        pharmacy={'title':'Новая аптека','address':'Улица, 12','region':region['id'],
            'city':REFERENCE['cities'][region['id']][0]['id'],'program':REFERENCE['programs'][0]['id']}
        result=self.catalogue.preview(payload(target='new_pharmacy',new_pharmacy=pharmacy))
        self.assertEqual(result['pharmacy']['status'],'Потенциальная')
        with self.assertRaises(ValueError):
            self.catalogue.preview(payload(target='new_pharmacy',new_pharmacy={**pharmacy,'city':'invalid'}))

    def test_reminder_half_hour_and_optional_contact_are_validated(self):
        result=self.catalogue.preview(payload(reminder={'action':'Позвонить','deadline':'2099-10-14T10:30'},
            contact={'name':'Контакт','phone':'909876543'}))
        self.assertIn('10:30',result['reminder']['display'])
        self.assertEqual(result['contact']['phone'],'+998909876543')
        for due in ('2020-01-01T10:30','2099-10-14T10:15','2099-10-14T10:30+05:00'):
            with self.assertRaises(ValueError):
                self.catalogue.preview(payload(reminder={'action':'Позвонить','deadline':due}))

    def test_gateway_blocks_all_crm_mutations_before_network(self):
        api=ReadOnlyBitrix('https://example.invalid/rest/1/readonly/')
        with patch('catalog.Bitrix.call') as call:
            for method in ('crm.company.add','crm.item.add','crm.item.update','crm.requisite.add','crm.contact.add'):
                with self.assertRaises(ValueError):
                    api.call(method,{})
            call.assert_not_called()

    def test_live_lookup_deduplicates_requisites_and_counts_every_page_for_selected_company(self):
        class Api:
            base='https://example.invalid/rest/1/readonly/'
            def list_all(self,method,params,**options):
                if method=='crm.requisite.list':
                    return [{'ENTITY_ID':'15','ENTITY_TYPE_ID':4,'RQ_INN':'000000081'}]*2
                if method=='crm.company.list':
                    return [{'ID':'15','TITLE':'Найденная фирма'}]
                return [{'id':1,'title':'Аптека 1','companyId':15,'categoryId':17,'fom':'00081','address':'Улица, 1','status':9},
                        {'id':2,'title':'Аптека 2','companyId':15,'categoryId':17,'fom':'00082','address':'Улица, 2','status':10},
                        {'id':3,'title':'Чужая аптека','companyId':16,'categoryId':17,'status':10}]
            def call(self,method,params):
                return {'result':{'fields':{'status':{'items':[{'ID':9,'VALUE':'Работает'},{'ID':10,'VALUE':'Закрыта'}]}}}}
        settings=SimpleNamespace(pharmacy=SimpleNamespace(entity_type_id=1034,category_id=17,fields={'status':'status','address':'address'}))
        rows=LiveCatalogue(Api(),settings).lookup('000000081')
        self.assertEqual(rows[0]['pharmacy_count'],2)
        self.assertTrue(rows[0]['pharmacies'][1]['inactive'])

    def test_connection_failure_is_not_reported_as_missing_company_or_zero_pharmacies(self):
        class Broken:
            def lookup(self,inn):
                raise RemoteError('CONNECTION_ERROR')
        with self.assertRaises(RemoteError):
            PreviewCatalogue(Broken()).lookup('crm','300000101')


if __name__=='__main__':
    unittest.main()
