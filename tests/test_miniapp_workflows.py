"""User workflows: direct launch, existing FOM ID, consent, compact list and support."""
from copy import deepcopy
from dataclasses import replace
import json
from urllib.parse import urlsplit,parse_qs
import unittest
from unittest.mock import patch

import test_live_deals as deals
import test_work_sync as support
from test_intake_forms import payload
from datfo_crm_bot.input_forms import launch_button,launch_matches
from datfo_crm_bot.sales_intake import FOM_ID_FIELD
from datfo_crm_bot.service import DEAL,CONFIRM
from datfo_crm_bot.live_deals import MY_CRM
from datfo_crm_bot.okb_crm import OkbCrm,OkbSettings
from datfo_crm_bot.communications import InboxError
from datfo_crm_bot.api import RemoteError
from datfo_crm_bot.work_inbox import SEND
from datfo_crm_bot.simulation import SimulationCrm


class DealFormTests(unittest.TestCase):
    setUp,tearDown=deals.LiveDealTests.setUp,deals.LiveDealTests.tearDown
    message,say=deals.LiveDealTests.message,deals.LiveDealTests.say
    use_offline_pharmacy_components=deals.LiveDealTests.use_offline_pharmacy_components

    def send_form(self,data,user=100):
        if data.get('mode')=='deal':
            data={'discussion_programs':['5246'],**data}
        update=self.message('',user)
        update['message'].pop('text')
        update['message']['web_app_data']={'data':json.dumps(data,ensure_ascii=False)}
        self.live.handle(update)
        return update

    def token(self,user=100):
        button=launch_button(self.store,user,'deal',DEAL)
        return parse_qs(urlsplit(button['web_app']['url']).query)['token'][0]

    def test_menu_opens_form_directly_and_existing_fom_skips_pharmacy_company(self):
        token=self.token()
        self.assertIsNone(self.store.session(100))
        original=deepcopy(self.api.pharmacies)
        self.send_form({'token':token,'mode':'deal','pharmacy_kind':'existing','fom_id':'00081'})
        state=self.store.session(100)
        self.assertEqual(state['step'],'confirm')
        self.assertEqual(state['pharmacy']['id'],81)
        self.assertEqual(state['company']['id'],7)
        self.assertFalse(launch_matches(self.store,100,'deal',token))
        self.assertIn('Компания / Фирма',self.telegram.messages[-1][1])
        self.assertIn('🔔',self.telegram.messages[-1][1])
        original_call=self.api.call
        def call(method,data):
            if method=='crm.item.add':
                self.assertTrue(self.telegram.messages[-1][1].startswith('⏳ Подождите'))
            return original_call(method,data)
        self.api.call=call
        self.say(CONFIRM)
        self.say(CONFIRM)
        self.assertEqual(len(self.api.rows),1)
        self.assertEqual(self.api.rows[501]['parentId1034'],81)
        self.assertEqual(self.api.pharmacies,original)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM operations WHERE kind='pharmacy'").fetchone()[0],0)
        self.assertTrue(any(isinstance(b,dict) and 'web_app' in b for row in self.telegram.messages[-1][2] for b in row))

    def test_fom_is_exact_not_bitrix_id_and_duplicates_are_not_auto_selected(self):
        sales=self.live.bot.sales
        self.assertEqual(sales.lookup_fom('00081')['id'],81)
        for bad in ('81','0008','bad id',''):
            with self.subTest(bad=bad),self.assertRaises(ValueError):sales.lookup_fom(bad)
        self.api.pharmacies[82]={**self.api.pharmacies[81],'id':82}
        with self.assertRaises(ValueError):sales.lookup_fom('00081')
        self.assertFalse(any(m=='crm.item.add' for m,_ in self.api.calls))

    def test_progress_precedes_first_crm_read_and_replay_does_not_repeat_lookup(self):
        self.api.calls.clear()
        original = self.api.call
        def call(method, data):
            if not self.api.calls:
                self.assertIn('Подождите, проверяем аптеку и готовим сделку', self.telegram.messages[-1][1])
            return original(method, data)
        self.api.call = call
        update = self.send_form({'token':self.token(),'mode':'deal','pharmacy_kind':'existing','fom_id':'00081'})
        reads = len(self.api.calls)
        self.live.handle(update)
        self.assertEqual(len(self.api.calls), reads)
        self.assertEqual(sum('Подождите, проверяем' in message[1] for message in self.telegram.messages), 1)

    def test_missing_fom_has_clear_retry_and_same_form_accepts_corrected_id(self):
        data = {'token':self.token(),'mode':'deal','pharmacy_kind':'existing','fom_id':'81'}
        self.send_form(data)
        self.assertIn('Аптека с таким FOM ID не найдена', self.telegram.messages[-1][1])
        self.assertIn('Проверьте ID и введите другой', self.telegram.messages[-1][1])
        self.assertEqual(self.store.session(100)['step'], 'sales_bulk_input')
        self.assertEqual(self.api.rows, {})
        self.send_form({**data,'fom_id':'00081'})
        self.assertEqual(self.store.session(100)['step'], 'confirm')
        self.assertEqual(self.api.rows, {})

    def test_wrong_user_token_and_active_draft_are_rejected(self):
        token=self.token(100)
        self.send_form({'token':token,'mode':'deal','pharmacy_kind':'existing','fom_id':'00081'},200)
        self.assertIsNone(self.store.session(200))
        self.store.set_session(100,{'step':'confirm','workflow':'okb','request_id':'active'})
        self.send_form({'token':token,'mode':'deal','pharmacy_kind':'existing','fom_id':'00081'})
        self.assertEqual(self.store.session(100)['request_id'],'active')
        self.assertEqual(self.api.rows,{})

    def test_fom_or_company_change_after_preview_blocks_creation(self):
        token=self.token()
        self.send_form({'token':token,'mode':'deal','pharmacy_kind':'existing','fom_id':'00081'})
        self.api.pharmacies[81][FOM_ID_FIELD]='changed'
        self.say(CONFIRM)
        self.assertEqual(self.api.rows,{})

    def test_unchecked_contact_uses_phone_only_and_does_not_search_or_create_person(self):
        crm=self.use_offline_pharmacy_components()
        contacts=deepcopy(crm.records['contacts'])
        token=self.token()
        crm.contacts=lambda phone: self.fail('Unchecked contact must not search for a person')
        self.send_form(payload(crm,token=token,mode='deal',pharmacy_kind='new',contact_policy='optional',phone='909876543'))
        state=self.store.session(100)
        self.assertEqual(state['step'],'confirm')
        self.assertTrue(state['pharmacy_form']['phone_only'])
        self.assertIn('Телефон аптеки: +998909876543',self.telegram.messages[-1][1])
        self.assertNotIn('👤',self.telegram.messages[-1][1])
        self.say(CONFIRM)
        self.assertEqual(crm.records['contacts'],contacts)
        self.assertEqual(len(crm.items),1)

    def test_compact_list_has_eight_rows_and_one_message_per_page(self):
        for n in range(11):
            ident=501+n;key='page-'+str(n)
            state={'workflow':'live_deal','kind':'deal','request_id':key}
            self.store.prepare(100,state,'2026-10-07T00:00:00Z')
            self.store.status(key,'succeeded',{'id':ident,'url':f'https://example.test/crm/deal/details/{ident}/'})
            self.api.rows[ident]={'id':ident,'title':'Аптека '+str(n),'originatorId':'datfo-sales-telegram','originId':key,
                'assignedById':132,'categoryId':45,'stageId':'C45:PLAN'}
        self.say(MY_CRM)
        hub=self.telegram.calls[-1][1]
        self.assertEqual([row[0]['callback_data'] for row in hub['reply_markup']['inline_keyboard']],['ld:list:deal:0','ld:list:support:0'])
        self.live.handle({'update_id':499,'callback_query':{'id':'list','from':{'id':100},'message':{'message_id':123,'chat':{'id':100,'type':'private'}},'data':'ld:list:deal:0'}})
        page=self.telegram.calls[-1][1]
        self.assertEqual(sum(m=='sendMessage' for m,_ in self.telegram.calls),1)
        self.assertIn('1/2',page['text'])
        self.assertEqual(page['text'].count(' · <a href='),8)
        self.assertNotIn('Следующий шаг:',page['text'])
        self.live.handle({'update_id':500,'callback_query':{'id':'page','from':{'id':100},'message':{'message_id':123,'chat':{'id':100,'type':'private'}},'data':'ld:list:deal:1'}})
        self.assertEqual(self.telegram.calls[-1][0],'editMessageText')
        self.assertIn('2/2',self.telegram.calls[-1][1]['text'])
        self.assertEqual(self.telegram.calls[-1][1]['text'].count(' · <a href='),3)

    def test_actual_pharmacy_payload_has_number_without_contact_binding(self):
        calls=[]
        class Api:
            def call(self,method,data):
                calls.append((method,deepcopy(data)))
                return {'result':{'item':{'id':81,**data['fields']}}}
        settings=OkbSettings.load()
        config=replace(self.config,targets={'pharmacy':settings.pharmacy})
        crm=OkbCrm(config,Api(),settings)
        state={'title':'Аптека','request_id':'phone-only','address':'Адрес','business_region':{'id':'1'},'city':{'id':'2'},
            'current_program':{'id':'5919'},'phone_only':True,'phone':'+998909876543'}
        crm.create_okb_pharmacy(state,{'id':7},132,None)
        fields=calls[0][1]['fields']
        self.assertEqual(fields[settings.pharmacy.fields['phone']],state['phone'])
        self.assertNotIn(settings.pharmacy.fields['contact_ids'],fields)


class SupportFormTests(unittest.TestCase):
    setUpBase=support.SupportSyncTests.setUpBase
    setUp,tearDown=support.SupportSyncTests.setUp,support.SupportSyncTests.tearDown
    say=support.SupportSyncTests.say

    def send_form(self,data,user=200,ident=None):
        self.sequence+=1
        data = {'support_type':'5890','support_program':'',**data}
        update={'update_id':ident or self.sequence,'message':{'from':{'id':user},'chat':{'id':user,'type':'private'},'web_app_data':{'data':json.dumps(data)}}}
        self.work.handle(update)
        return update

    def token(self,user=200):
        button=launch_button(self.primary,user,'support','Заявка')
        return parse_qs(urlsplit(button['web_app']['url']).query)['token'][0]

    def choose_recipient(self):
        draft=self.store.session(200)
        self.say(str(next(n for n,row in enumerate(draft['recipients'],1) if row['telegram_id']==300)))

    def test_existing_pharmacy_support_creates_only_technical_request_with_same_distribution(self):
        original=deepcopy(self.api.pharmacies)
        data={'token':self.token(),'mode':'support','pharmacy_kind':'existing','fom_id':'00081',
            'request_title':'Не работает программа','request_description':'Нужна помощь с подключением'}
        update=self.send_form(data)
        self.work.handle(update)
        self.choose_recipient()
        self.assertEqual(self.store.session(200)['step'],'confirm')
        self.say(SEND)
        self.say(SEND)
        row=self.store.listing(200)[0]
        self.assertEqual((row['link_kind'],row['pharmacy_id'],row['deal_id'],row['owner']),('pharmacy',81,0,300))
        remote=self.api.rows[self.sync.binding(row['id'])['crm_id']]
        self.assertEqual((remote['categoryId'],remote['parentId1034'],remote['companyId']),(47,81,7))
        self.assertEqual(sum(m=='crm.item.add' for m,_ in self.api.calls),1)
        self.assertEqual(self.api.pharmacies,original)
        self.work.card(200,row)
        self.assertNotIn('#0',self.telegram.calls[-1][1]['text'])
        working=self.store.change(300,row['id'],row['version'],'take')['ticket']
        self.sync.flush()
        self.assertEqual(working['status'],'working')
        self.assertEqual(self.sync.binding(row['id'])['state'],'confirmed')

    def test_missing_id_or_unauthorized_user_does_not_create_anything(self):
        token=self.token()
        self.send_form({'token':token,'mode':'support','pharmacy_kind':'existing','fom_id':'81',
            'request_title':'Проверить','request_description':'Помощь нужна'})
        self.assertEqual(self.store.session(200)['step'],'web_form')
        self.assertEqual(self.store.listing(200),[])
        self.send_form({'token':token,'mode':'support','pharmacy_kind':'existing','fom_id':'00081',
            'request_title':'Проверить','request_description':'Помощь нужна'},300)
        self.assertIsNone(self.store.session(300))
        self.assertFalse(any(m=='crm.item.add' for m,_ in self.api.calls))

    def test_support_progress_precedes_lookup_and_missing_id_can_be_corrected(self):
        original = self.work.intake.sales.lookup_fom
        def lookup(value):
            self.assertIn('Подождите, проверяем аптеку и готовим заявку', self.telegram.messages[-1][1])
            return original(value)
        self.work.intake.sales.lookup_fom = lookup
        data = {'token':self.token(),'mode':'support','pharmacy_kind':'existing','fom_id':'81',
                'request_title':'Проверить','request_description':'Помощь нужна'}
        self.send_form(data)
        self.assertIn('Проверьте ID и введите другой', self.telegram.messages[-1][1])
        self.assertEqual(self.store.session(200)['step'], 'web_form')
        self.send_form({**data,'fom_id':'00081'})
        self.assertEqual(self.store.session(200)['step'], 'recipient')
        self.assertEqual(self.store.listing(200), [])

    def test_company_change_after_support_preview_stops_request(self):
        self.send_form({'token':self.token(),'mode':'support','pharmacy_kind':'existing','fom_id':'00081',
            'request_title':'Проверить','request_description':'Помощь нужна'})
        self.choose_recipient()
        self.api.pharmacies[81]['companyId']=9
        self.say(SEND)
        self.assertEqual(self.store.listing(200),[])

    def test_forged_pharmacy_link_is_rejected_by_store(self):
        with self.assertRaises(InboxError):
            self.store.create(200,300,{'id':0,'link_kind':'pharmacy','pharmacy_id':81,'company_id':7,
                'request_id':'pharmacy:200:81','url':'https://forged.test'},'Проверить','Помощь нужна','forged')

    def test_read_failure_before_support_creation_is_retryable_without_duplicates(self):
        pharmacy=self.work.intake.sales.lookup_fom('00081')
        link=self.work.intake.link(200,pharmacy)
        row=self.store.create(200,300,link,'Проверить','Помощь нужна','read-failure')
        call=self.api.call
        def fail(method,data):
            if method=='crm.item.get' and data['entityTypeId']==1034:
                raise RemoteError('CONNECTION_ERROR',uncertain=True)
            return call(method,data)
        self.api.call=fail
        self.sync.flush()
        self.assertEqual(self.sync.binding(row['id'])['state'],'failed')
        self.assertFalse(any(m=='crm.item.add' for m,_ in self.api.calls))
        self.api.call=call
        self.sync.retry(row['id'],200)
        self.assertEqual(self.sync.binding(row['id'])['state'],'confirmed')
        self.assertEqual(sum(m=='crm.item.add' for m,_ in self.api.calls),1)

    def test_new_pharmacy_support_creates_pharmacy_then_one_support_and_no_sales_deal(self):
        sales=self.live.bot.sales
        crm=SimulationCrm(self.primary)
        contacts=deepcopy(crm.records['contacts'])
        choices=crm.choices
        crm.choices=lambda key: [{'id':'5919','title':'1С'}] if key=='current_program' else choices(key)
        crm.write_id=int
        sales.form.crm=sales.okb.crm=crm
        original=crm.create_okb_pharmacy
        def create(state,company,manager,contact):
            result=original(state,company,manager,contact)
            self.api.pharmacies[result['id']]={'id':result['id'],'title':state['title'],'companyId':company['id'],'categoryId':17}
            return result
        crm.create_okb_pharmacy=create
        self.send_form(payload(crm,token=self.token(),mode='support',pharmacy_kind='new',contact_policy='optional',phone='909876543',
            request_title='Не работает программа',request_description='Нужна помощь с подключением'))
        self.choose_recipient()
        self.assertEqual(crm.items,{})
        # A restarted worker has no cached member config; every message restores it.
        self.live.bot.config.users.clear()
        self.say(SEND)
        row=self.store.listing(200)[0]
        self.assertEqual(row['link_kind'],'pharmacy')
        self.assertEqual(len(crm.items),1)
        self.assertEqual(crm.records['contacts'],contacts)
        self.assertEqual(sum(m=='crm.item.add' for m,_ in self.api.calls),1)
        self.assertEqual(self.api.rows[self.sync.binding(row['id'])['crm_id']]['parentId1034'],row['pharmacy_id'])
