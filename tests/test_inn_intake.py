"""Real handlers against fake CRM: INN binding, company targets and uncertain writes."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from datfo_crm_bot.api import RemoteError
from datfo_crm_bot.config import ConfigError
from datfo_crm_bot.inn_lookup import InnCatalogue, ReadOnlyBitrix
from datfo_crm_bot.miniapp_api import authorize, AccessDenied, LookupService
from datfo_crm_bot.service import CONFIRM
from datfo_crm_bot.work_inbox import SEND, TO_POOL, WorkStore
from datfo_crm_bot.inn_target import selection
from datfo_crm_bot.okb_crm import OkbSettings
import test_miniapp_workflows as workflows


class DealTests(unittest.TestCase):
    setUp,tearDown = workflows.DealFormTests.setUp,workflows.DealFormTests.tearDown
    say,message,send_form,token = workflows.DealFormTests.say,workflows.DealFormTests.message,workflows.DealFormTests.send_form,workflows.DealFormTests.token
    use_offline_pharmacy_components = workflows.DealFormTests.use_offline_pharmacy_components

    def value(self, **changes):
        return {'token':self.token(),'mode':'deal','intake_version':2,'pharmacy_kind':'company',
                'inn':'123456789','company_id':7,'reminder':False,**changes}

    def test_company_deal_waits_for_confirmation_and_has_no_pharmacy(self):
        before = deepcopy(self.api.pharmacies)
        self.send_form(self.value())
        state = self.store.session(100)
        self.assertEqual(state['step'],'confirm')
        self.assertTrue(state['company_only'])
        self.assertIn('ИНН: 123456789',self.telegram.messages[-1][1])
        self.assertNotIn('FOM ID',self.telegram.messages[-1][1])
        self.assertEqual(self.api.rows,{})
        self.say(CONFIRM)
        self.say(CONFIRM)
        self.assertEqual(len(self.api.rows),1)
        row = self.api.rows[501]
        self.assertEqual(row['companyId'],7)
        self.assertNotIn('parentId1034',row)
        self.assertEqual(self.api.pharmacies,before)
        self.assertEqual(self.store.operation(state['request_id'])['status'],'succeeded')

    def test_selected_pharmacy_without_fom_id_is_linked_by_internal_id(self):
        self.api.pharmacies[81].pop('UF_CRM_8_1719398102513')
        self.send_form(self.value(pharmacy_kind='existing',pharmacy_id=81))
        self.assertEqual(self.store.session(100)['step'],'confirm')
        self.say(CONFIRM)
        self.assertEqual(self.api.rows[501]['parentId1034'],81)
        self.assertEqual(self.api.rows[501]['companyId'],7)

    def test_forged_company_or_pharmacy_bindings_do_not_write(self):
        sales = self.live.bot.sales
        for value in (self.value(company_id=8),self.value(inn='000000081'),
                      self.value(company_id='',create_company=True,company_name='Подмена')):
            with self.subTest(value=value),self.assertRaises(ValueError):
                selection(sales,value)
        self.api.pharmacies[81]['companyId'] = 8
        with self.assertRaises(ValueError):
            selection(sales,self.value(pharmacy_kind='existing',pharmacy_id=81))
        self.assertFalse(any(method.endswith('.add') for method,_ in self.api.calls))

    def test_company_inn_change_between_preview_and_confirmation_blocks_deal(self):
        self.send_form(self.value())
        self.live.bot.sales.pharmacy_crm.companies = lambda _: []
        self.say(CONFIRM)
        self.assertEqual(self.api.rows,{})
        self.assertIn('Связь ИНН',self.telegram.messages[-1][1])

    def new_crm(self):
        crm = self.use_offline_pharmacy_components()
        crm.settings = OkbSettings.load()
        required = {'TITLE','ASSIGNED_BY_ID','ORIGINATOR_ID','ORIGIN_ID','OPENED','ENTITY_TYPE_ID',
                    'ENTITY_ID','PRESET_ID','NAME','RQ_INN','RQ_COMPANY_NAME','XML_ID','ACTIVE'}
        class Api:
            def call(_,method,data=None):
                if method == 'crm.requisite.preset.field.list':
                    return {'result':[{'FIELD_NAME':key} for key in ('RQ_INN','RQ_COMPANY_NAME')]}
                return {'result':{key:{'isReadOnly':False} for key in required}}
            def list_all(_,method,data):
                return [{'ID':crm.settings.preset_id,'ENTITY_TYPE_ID':8}]
        crm.api = Api()
        return crm

    def test_new_company_requires_consent_and_only_creates_company_requisite_deal(self):
        crm = self.new_crm()
        value = self.value(inn='111222333',company_id='',create_company=True,company_name='ООО Новая фирма')
        with self.assertRaises(ValueError):
            selection(self.live.bot.sales,{**value,'create_company':False})
        self.send_form(value)
        self.assertEqual(crm.records['companies'],{})
        self.say(CONFIRM)
        self.say(CONFIRM)
        self.assertEqual(len(crm.records['companies']),1)
        self.assertEqual(len(crm.records['requisites']),1)
        self.assertEqual(crm.items,{})
        self.assertEqual(len(self.api.rows),1)
        self.assertNotIn('parentId1034',self.api.rows[501])

    def test_lost_company_create_response_recovers_by_origin_without_second_add(self):
        crm = self.new_crm()
        create = crm.create_company
        writes = []
        def lost(state, manager):
            writes.append(state['request_id'])
            create(state,manager)
            raise RemoteError('CONNECTION_ERROR',uncertain=True)
        crm.create_company = lost
        self.send_form(self.value(inn='111222333',company_id='',create_company=True,company_name='ООО Новая фирма'))
        self.say(CONFIRM)
        self.assertEqual(self.api.rows,{})
        self.say(CONFIRM)
        self.assertEqual(len(writes),1)
        self.assertEqual(len(self.api.rows),1)

    def test_unconfirmed_company_origin_is_never_repeated(self):
        crm = self.new_crm()
        writes = []
        def lost(state, manager):
            writes.append(state['request_id'])
            raise RemoteError('CONNECTION_ERROR',uncertain=True)
        crm.create_company = lost
        self.send_form(self.value(inn='111222333',company_id='',create_company=True,company_name='ООО Новая фирма'))
        self.say(CONFIRM)
        self.say(CONFIRM)
        self.assertEqual(len(writes),1)
        self.assertEqual(self.api.rows,{})

    def test_other_pending_company_creation_blocks_shared_inn(self):
        crm = self.new_crm()
        self.store.prepare(200,{'kind':'company','workflow':'company_only','request_id':'other-company',
                           'inn':'111222333'},'2026-10-14T00:00:00Z')
        self.store.status('other-company','uncertain')
        self.send_form(self.value(inn='111222333',company_id='',create_company=True,company_name='ООО Новая фирма'))
        self.say(CONFIRM)
        self.assertEqual(crm.records['companies'],{})
        self.assertEqual(self.api.rows,{})


class SupportTests(unittest.TestCase):
    setUpBase = workflows.SupportFormTests.setUpBase
    setUp,tearDown,say,send_form,token = (workflows.SupportFormTests.setUp,workflows.SupportFormTests.tearDown,
                                       workflows.SupportFormTests.say,workflows.SupportFormTests.send_form,workflows.SupportFormTests.token)

    def value(self, **changes):
        return {'token':self.token(),'mode':'support','intake_version':2,'pharmacy_kind':'company',
            'inn':'123456789','company_id':7,'request_title':'Нужна помощь','request_description':'Программа не открывается',**changes}

    def test_company_request_keeps_distribution_and_has_no_pharmacy_link(self):
        self.send_form(self.value())
        self.assertEqual(self.store.session(200)['step'],'recipient')
        self.say(TO_POOL)
        self.assertIn('ИНН: 123456789',self.telegram.messages[-1][1])
        self.say(SEND)
        self.say(SEND)
        rows = self.store.listing(200)
        self.assertEqual(len(rows),1)
        row = rows[0]
        self.assertEqual(row['link_kind'],'company')
        self.assertEqual(row['company_id'],7)
        self.assertEqual(row['company_inn'],'123456789')
        self.assertIsNone(row['pharmacy_id'])
        self.work.sync.flush()
        binding = self.work.sync.binding(row['id'])
        self.assertEqual(binding['state'],'confirmed')
        remote = self.api.rows[binding['crm_id']]
        self.assertEqual(remote['companyId'],7)
        self.assertNotIn('parentId1034',remote)
        self.assertEqual(len(self.api.rows),2)  # Fixture deal plus the single support request.

    def test_foreign_actor_cannot_reuse_company_reference(self):
        self.send_form(self.value())
        draft = self.store.session(200)
        link = self.work.intake.submit(200,draft)
        with self.assertRaises(ValueError):
            self.store.create(500,None,link,'Помощь','Программа не работает','forged-company')

    def test_company_removed_from_inn_before_worker_write_blocks_request(self):
        self.send_form(self.value())
        self.say(TO_POOL)
        self.work.sync.crm.companies = lambda _: []
        self.say(SEND)
        self.work.sync.flush()
        row = self.store.listing(200)[0]
        self.assertEqual(self.work.sync.binding(row['id'])['error'],'FOM_COMPANY_CHANGED')
        self.assertEqual(len(self.api.rows),1)

    def test_new_company_for_support_recovers_lost_response_before_single_request(self):
        from datfo_crm_bot.simulation import SimulationCrm
        crm = SimulationCrm(self.primary)
        # Use the schema fixture and the real phase/recovery methods.
        holder = SimpleNamespace(use_offline_pharmacy_components=lambda:crm)
        crm = DealTests.new_crm(holder)
        sales = self.work.intake.sales
        sales.pharmacy_crm = sales.okb.crm = sales.form.crm = crm
        self.work.sync.crm.companies = crm.companies
        original = crm.create_company
        writes = []
        def lost(state,manager):
            writes.append(state['request_id'])
            original(state,manager)
            raise RemoteError('CONNECTION_ERROR',uncertain=True)
        crm.create_company = lost
        self.send_form(self.value(inn='111222333',company_id='',create_company=True,company_name='ООО Новая фирма'))
        self.say(TO_POOL)
        self.say(SEND)
        self.assertEqual(self.store.listing(200),[])
        self.assertEqual(self.store.session(200)['step'],'confirm')
        self.say(SEND)
        self.say(SEND)
        rows = self.store.listing(200)
        self.assertEqual(len(rows),1)
        self.assertEqual(len(writes),1)
        self.assertEqual(len(crm.records['requisites']),1)
        self.assertEqual(rows[0]['link_kind'],'company')
        self.assertEqual(self.work.sync.binding(rows[0]['id'])['state'],'confirmed')
        self.assertNotIn('parentId1034',self.api.rows[self.work.sync.binding(rows[0]['id'])['crm_id']])


class ApiTests(unittest.TestCase):
    setUp,tearDown = workflows.DealFormTests.setUp,workflows.DealFormTests.tearDown
    token = workflows.DealFormTests.token

    def access(self):
        work = WorkStore(self.store,{100})
        work.grant(100,'fom_sales',100)
        return work

    def test_token_is_bound_to_role_status_mode_and_active_launch(self):
        work = self.access()
        token = self.token()
        self.assertEqual(authorize(self.path,token,'deal'),100)
        for mode, value in (('support',token),('deal','datfo-form-'+'0'*32)):
            with self.assertRaises(AccessDenied):
                authorize(self.path,value,mode)
        work.grant(100,'tech',100)
        with self.assertRaises(AccessDenied):
            authorize(self.path,token,'deal')
        work.grant(100,'fom_sales',100)
        with self.store.db:
            self.store.db.execute("UPDATE registrations SET status='rejected' WHERE telegram_id=100")
        with self.assertRaises(AccessDenied):
            authorize(self.path,token,'deal')

    def test_used_or_expired_launch_does_not_authorize_lookup(self):
        self.access()
        token = self.token()
        with self.store.db:
            self.store.db.execute('UPDATE settings SET value=? WHERE key=?',
                (json.dumps({'token':token,'used':False,'issued_at':1}),'webapp-launch:deal:100'))
        with self.assertRaises(AccessDenied):
            authorize(self.path,token,'deal')
        with self.store.db:
            self.store.db.execute('UPDATE settings SET value=? WHERE key=?',
                (json.dumps({'token':token,'used':True}),'webapp-launch:deal:100'))
        with self.assertRaises(AccessDenied):
            authorize(self.path,token,'deal')

    def test_lookup_failure_never_becomes_missing_company(self):
        self.access()
        token = self.token()
        catalogue = SimpleNamespace(lookup=lambda _:(_ for _ in ()).throw(RemoteError('CONNECTION_ERROR')))
        service = LookupService(self.path,catalogue)
        with self.assertRaises(RemoteError):
            service.lookup({'token':token,'mode':'deal','inn':'123456789'})
        self.assertEqual(service.cache,{})

    def test_gateway_rejects_all_mutations_without_network(self):
        api = ReadOnlyBitrix('https://example.invalid/rest/1/readonly/')
        with patch('datfo_crm_bot.inn_lookup.Bitrix.call') as call:
            for method in ('crm.company.add','crm.item.add','crm.requisite.add','crm.contact.add','crm.item.update'):
                with self.assertRaises(ValueError):
                    api.call(method,{})
            call.assert_not_called()

    def test_revocation_during_crm_lookup_discards_response(self):
        self.access()
        token = self.token()
        def revoke(_):
            with self.store.db:
                self.store.db.execute("UPDATE registrations SET status='rejected' WHERE telegram_id=100")
            return [{'id':7,'title':'Firm'}]
        service = LookupService(self.path,SimpleNamespace(lookup=revoke))
        with self.assertRaises(AccessDenied):
            service.lookup({'token':token,'mode':'deal','inn':'123456789'})
        self.assertEqual(service.cache,{})

    def test_catalogue_filters_unrelated_items_and_preserves_missing_fom(self):
        class Api:
            base = 'https://example.invalid/rest/1/readonly/'
            def list_all(_,method,data,**options):
                if method == 'crm.requisite.list':
                    return [{'ENTITY_TYPE_ID':4,'ENTITY_ID':7,'RQ_INN':'000000081'}]*2
                if method == 'crm.company.list':
                    return [{'ID':7,'TITLE':'Firm'}]
                self.assertEqual(data['filter'],{'categoryId':17,'companyId':7})
                self.assertEqual(data['select'],['*'])
                self.assertEqual(options['key'],'items')
                return [{'id':81,'companyId':7,'categoryId':17,'title':'Pharmacy','status':9},
                    {'id':81,'companyId':7,'categoryId':17,'title':'Duplicate','status':9},
                    {'id':82,'companyId':8,'categoryId':17,'title':'Wrong company','status':9},
                    {'id':83,'companyId':7,'categoryId':18,'title':'Wrong category','status':9}]
            def call(_,method,data):
                return {'result':{'fields':{'status':{'items':[{'ID':9,'VALUE':'Закрыта'}]}}}}
        settings = SimpleNamespace(pharmacy=SimpleNamespace(entity_type_id=1034,category_id=17,
            fields={'status':'status','address':'address'}))
        result = InnCatalogue(Api(),settings).lookup('000000081')
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['pharmacy_count'],1)
        self.assertEqual(result[0]['pharmacies'][0]['fom_id'],'')
        self.assertTrue(result[0]['pharmacies'][0]['inactive'])


if __name__ == '__main__':
    unittest.main()
