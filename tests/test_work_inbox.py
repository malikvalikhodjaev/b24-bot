from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from datfo_crm_bot.api import RemoteError
from datfo_crm_bot.communications import InboxError
from datfo_crm_bot.config import User, ConfigError
from datfo_crm_bot.demo import demo_config
from datfo_crm_bot.guidance import Guide
from datfo_crm_bot.live_deals import LiveDeals, SOURCE
from datfo_crm_bot.registration import Registration, RegistrationSettings
from datfo_crm_bot.storage import Store
from datfo_crm_bot.work_inbox import WorkInbox, WorkStore, SEND, NEW, INBOX, DIAGNOSTICS
from test_communications import FakeTelegram, NoDirectory
from test_live_deals import DealApi


class WorkInboxTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name)/'primary.sqlite3'
        self.primary = Store(self.path)
        for tg,b24,name in ((100,132,'Малик'),(200,133,'Менеджер ФОМ'),(300,134,'Техник'),(400,135,'Обучающий'),(500,136,'Другой менеджер')):
            self.primary.request_registration(tg,tg,{'id':b24,'name':name})
            self.primary.approve_registration(tg,100,{'id':b24,'name':name})
        self.telegram = FakeTelegram()
        self.registration = Registration(RegistrationSettings({100:User(132,True,'Малик')}),self.primary,NoDirectory(),self.telegram)
        self.api = DealApi()
        self.live = LiveDeals(self.registration,replace(demo_config(self.path),targets={}),self.api,self.telegram)
        self.work = WorkInbox(self.registration,self.telegram,self.live)
        self.store = self.work.store
        for user,role in ((200,'fom_sales'),(300,'tech'),(400,'trainer'),(500,'fom_sales')):
            self.store.grant(user,role,100)
        self.state = {'workflow':'live_deal','kind':'deal','request_id':'verified-deal','category_id':43,
                      'initial_stage':'C43:BASE','title':'Аптека тест','step':'confirm'}
        self.primary.prepare(200,self.state,'2026-10-06T00:00:00Z')
        self.primary.status('verified-deal','succeeded',{'id':501,'url':'https://example.test/crm/deal/details/501/'})
        self.api.rows[501] = {'id':501,'title':'Аптека тест','originatorId':SOURCE,'originId':'verified-deal',
                              'categoryId':43,'stageId':'C43:BASE','assignedById':133,'updatedTime':'1'}
        self.sequence = 0

    def tearDown(self):
        self.primary.close()
        self.folder.cleanup()

    def ticket(self, recipient=100, request='work-1'):
        return self.store.create(200,recipient,self.work.link(200,501),'Нужно подключить','Проверить подключение компьютера',request)

    def say(self, text, user=200):
        self.sequence += 1
        update = {'update_id':self.sequence,'message':{'from':{'id':user},'chat':{'id':user,'type':'private'},'text':text}}
        return self.work.handle(update),update

    def callback(self, row, action, user, target=None, version=None):
        self.sequence += 1
        data = f"wr:{action}:{row['id']}:{row['version'] if version is None else version}"
        if target is not None:
            data += ':'+str(target)
        return {'update_id':self.sequence,'callback_query':{'id':'q'+str(self.sequence),'from':{'id':user},
            'message':{'chat':{'id':user,'type':'private'}},'data':data}}

    def test_direct_delivery_has_ticket_number_actual_deal_and_optional_username(self):
        self.primary.remember_identity({'id':300,'username':'tech_fom'})
        row = self.ticket(300)
        self.assertEqual((row['owner'],row['initial_recipient'],row['status']),(300,300,'assigned'))
        self.work.flush()
        deliveries = [payload for method,payload in self.telegram.calls if method=='sendMessage']
        self.assertEqual(len(deliveries),1)
        self.assertEqual(deliveries[0]['chat_id'],300)
        self.assertIn('Заявка #'+str(row['id']),deliveries[0]['text'])
        self.assertIn('Б24 сделкаси #501',deliveries[0]['text'])
        self.assertIn('https://example.test/crm/deal/details/501/',deliveries[0]['text'])
        self.assertIn('@tech_fom',deliveries[0]['text'])
        self.work.card(200,row)
        self.assertIn('tg://user?id=200',self.telegram.calls[-1][1]['text'])

    def test_known_manager_accepts_and_forwards_preserving_identity_and_crm(self):
        row = self.ticket()
        after = self.store.change(100,row['id'],0,'take')['ticket']
        after = self.store.change(100,row['id'],after['version'],'assign',300)['ticket']
        after = self.store.change(300,row['id'],after['version'],'take')['ticket']
        after = self.store.change(300,row['id'],after['version'],'assign',400)['ticket']
        self.assertEqual((after['id'],after['deal_id'],after['deal_url'],after['initial_recipient']),
                         (row['id'],501,row['deal_url'],100))
        self.assertEqual((after['owner'],after['status']),(400,'assigned'))
        self.assertEqual([event['action'] for event in self.store.history(row['id'])],['created','take','assign','take','assign'])
        self.assertTrue(self.store.visible(after,200))
        self.assertFalse(self.store.visible(after,500))
        self.assertFalse(any(method in {'crm.item.add','crm.item.update'} for method,payload in self.api.calls))

    def test_pool_claim_race_across_real_accounts_only_one_wins(self):
        row = self.ticket(300)
        pool = self.store.change(300,row['id'],0,'pool')['ticket']
        barrier = threading.Barrier(2)
        def claim(actor):
            primary = Store(self.path)
            store = WorkStore(primary,{100})
            try:
                barrier.wait(timeout=10)
                return store.change(actor,pool['id'],pool['version'],'take')
            finally:
                primary.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(claim,user) for user in (100,400)]
            results = [future.result(timeout=20) for future in futures]
        self.assertEqual(sum(result['changed'] for result in results),1)
        loser = next(result for result in results if not result['changed'])
        self.assertIn(loser['busy'],{100,400})
        self.assertEqual(len(self.store.history(row['id'])),3)

    def test_second_claim_displays_real_owner_and_no_user_cannot_overwrite(self):
        self.primary.remember_identity({'id':300,'username':'fom_tech'})
        row = self.ticket(300)
        after = self.store.change(300,row['id'],0,'take')['ticket']
        self.work.handle(self.callback(row,'take',200))
        answers = [p for method,p in self.telegram.calls if method=='answerCallbackQuery']
        self.assertIn('@fom_tech',answers[-1]['text'])
        self.assertEqual(self.store.ticket(row['id'])['owner'],300)
        for action in ('close','pool','assign'):
            with self.subTest(action=action),self.assertRaises(InboxError):
                self.store.change(200,row['id'],after['version'],action,400)

    def test_old_forward_close_release_buttons_cannot_mutate_new_assignment(self):
        row = self.ticket(300)
        first = self.store.change(300,row['id'],0,'take')['ticket']
        latest = self.store.change(300,row['id'],first['version'],'assign',400)['ticket']
        for action in ('close','pool','assign'):
            with self.subTest(action=action),self.assertRaises(InboxError):
                self.store.change(300,row['id'],first['version'],action,100)
        self.assertEqual(self.store.ticket(row['id'])['version'],latest['version'])

    def test_tech_cannot_create_fom_request_and_other_employee_needs_role(self):
        with self.assertRaises(InboxError):
            self.store.create(300,100,self.work.link(100,501),'Новая заявка','Проверить компьютер','tech-created')
        self.store.grant(500,'off',100)
        self.say('/request',500)
        self.assertIn('рол берилмаган',self.telegram.messages[-1][1])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM fom_requests').fetchone()[0],0)

    def test_private_ticket_foreign_deal_and_group_chat_are_rejected(self):
        row = self.ticket()
        with self.assertRaises(InboxError):
            self.store.change(500,row['id'],0,'take')
        with self.assertRaises(ConfigError):
            self.work.link(500,501)
        update = {'message':{'from':{'id':200},'chat':{'id':-100,'type':'group'},'text':'/requests'}}
        self.assertFalse(self.work.handle(update))

    def test_admin_marker_in_forged_link_cannot_bypass_creator_check(self):
        link = dict(self.work.link(200,501),admin_checked=True)
        with self.assertRaises(InboxError):
            self.store.create(500,300,link,'Чужая заявка','Нельзя брать чужую сделку','forged')
        with self.assertRaises(InboxError):
            self.store.grant(500,'tech',200)

    def test_revoked_target_cannot_accept_or_receive_and_admin_recovers(self):
        row = self.ticket(300)
        self.primary.deny_registration(300,100,'revoked')
        with self.assertRaises(InboxError):
            self.store.change(300,row['id'],0,'take')
        self.work.flush()
        self.assertFalse(any(method=='sendMessage' for method,p in self.telegram.calls))
        self.assertEqual(self.store.db.execute('SELECT state FROM fom_request_outbox').fetchone()[0],'blocked')
        recovered = self.store.change(100,row['id'],0,'recover',admin=True)['ticket']
        self.assertEqual((recovered['owner'],recovered['status']),(None,'new'))
        other = self.ticket(400,'active-owner')
        with self.assertRaises(InboxError):
            self.store.change(100,other['id'],0,'recover',admin=True)

    def test_idempotent_create_and_restart_keep_owner_link_and_role_off(self):
        row = self.ticket()
        second = self.ticket()
        self.assertEqual(row['id'],second['id'])
        self.assertEqual(len(self.store.history(row['id'])),1)
        self.store.change(100,row['id'],0,'assign',300)
        self.store.grant(100,'off',100)
        self.primary.close()
        self.primary = Store(self.path)
        self.registration.store = self.primary
        self.live = LiveDeals(self.registration,replace(demo_config(self.path),targets={}),self.api,self.telegram)
        self.work = WorkInbox(self.registration,self.telegram,self.live)
        self.store = self.work.store
        after = self.store.ticket(row['id'])
        self.assertEqual((after['owner'],after['deal_id']),(300,501))
        self.assertIsNone(self.store.role(100))

    def test_delivery_uncertainty_is_not_automatically_retried(self):
        row = self.ticket(300)
        self.telegram.fail_inline = True
        self.work.flush()
        self.assertEqual(self.store.db.execute('SELECT state FROM fom_request_outbox').fetchone()[0],'uncertain')
        calls = len(self.telegram.calls)
        self.work.flush()
        self.assertEqual(len(self.telegram.calls),calls)
        self.work.delivery_note(200,row['id'])
        self.assertIn('тасдиқланмаган',self.telegram.messages[-1][1])
        self.say('/requests_resend',100)
        self.assertEqual(self.store.db.execute('SELECT state FROM fom_request_outbox').fetchone()[0],'sent')

    def test_complete_form_replay_and_lost_sender_response_make_one_request(self):
        self.say('/request')
        self.say('1')
        recipients = self.store.session(200)['recipients']
        choice = next(i for i,item in enumerate(recipients,1) if item['telegram_id']==300)
        self.say(str(choice))
        self.say('Проблема соединения')
        self.say('Компьютер не соединяется с сервером')
        self.telegram.fail_reply = True
        _,update = self.say(SEND)
        self.work.handle(update)
        rows = self.store.listing(200)
        self.assertEqual(len(rows),1)
        self.assertEqual((rows[0]['owner'],rows[0]['deal_id']),(300,501))
        self.assertIsNone(self.store.session(200))
        self.assertEqual(len(self.store.history(rows[0]['id'])),1)

    def test_changed_bitrix_origin_or_owner_at_confirmation_prevents_delivery(self):
        self.say('/request')
        self.say('1')
        self.say('1')
        self.say('Нужно проверить')
        self.say('Не открывается программа')
        self.api.rows[501]['assignedById'] = 134
        self.say(SEND)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM fom_requests').fetchone()[0],0)
        self.assertEqual(self.store.session(200)['step'],'confirm')
        self.assertIn('масъул',self.telegram.messages[-1][1])

    def test_help_and_inbox_view_preserve_both_drafts(self):
        self.primary.set_session(200,{'step':'okb_title','workflow':'okb'})
        self.say('/request')
        self.assertIsNone(self.store.session(200))
        self.assertEqual(self.primary.session(200)['step'],'okb_title')
        self.primary.set_session(200,None)
        self.say('/request')
        before = deepcopy(self.store.session(200))
        guide = Guide(self.registration,self.telegram,work_inbox=self.work)
        update = {'message':{'from':{'id':200},'chat':{'id':200,'type':'private'},'text':'/next'}}
        self.assertTrue(guide.handle(update))
        self.assertIn('Техник ёрдам заявкаси',self.telegram.calls[-1][1]['text'])
        self.say('/requests')
        self.assertEqual(before,self.store.session(200))

    def test_actual_crm_card_has_request_button_and_diagnostics_links(self):
        operation = self.primary.operation('verified-deal')
        self.live.card(200,operation,self.registration.user(200))
        callbacks = [button['callback_data'] for row in self.telegram.calls[-1][1]['reply_markup']['inline_keyboard'] for button in row]
        self.assertIn('wr:from:501',callbacks)
        self.work.handle({'callback_query':{'id':'source','from':{'id':200},'message':{'chat':{'id':200,'type':'private'}},'data':'wr:from:501'}})
        self.assertEqual(self.store.session(200)['link']['id'],501)
        self.say(DIAGNOSTICS)
        self.assertEqual(self.store.session(200)['step'],'recipient')
        buttons = self.telegram.calls[-1][1]['reply_markup']['inline_keyboard']
        self.assertEqual(len(buttons),2)
        self.assertTrue(all(button[0]['url'].startswith('https://docs.superhuman.com/') for button in buttons))

    def test_working_support_alias_and_explicit_b24_test_keep_distinct_workflows(self):
        for label in ('/support','🛠 Техник ёрдам заявкаси очиш','🛠 Заявка в поддержку'):
            self.say(label)
            self.assertEqual(self.store.session(200)['step'],'web_form')
            self.say('/request_cancel')
        with self.primary.db:
            self.primary.db.execute("INSERT INTO settings VALUES('b24-test:100','1')")
        handled,_ = self.say('/support',100)
        self.assertFalse(handled)
        self.assertIsNone(self.store.session(100))

    def test_stats_exclude_test_deal_requests_and_only_count_personal_work(self):
        row = self.ticket(300)
        work = self.store.change(300,row['id'],0,'take')['ticket']
        self.store.change(300,row['id'],work['version'],'close')
        self.state.update(live_test=True,request_id='test-deal')
        self.primary.prepare(200,self.state,'2026-10-06T00:00:00Z')
        self.primary.status('test-deal','succeeded',{'id':502,'url':'https://example.test/crm/deal/details/502/'})
        self.api.rows[502] = dict(self.api.rows[501],id=502,originId='test-deal')
        self.store.create(200,300,self.work.link(200,502),'Тестовая заявка','Нужно проверить тест','test-request')
        self.assertEqual(self.store.stats(200)['sent'],1)
        self.assertEqual(self.store.stats(300)['closed'],1)
        self.assertEqual(self.store.stats(300)['assigned'],0)

    def test_preexisting_own_crm_deal_is_linked_without_creation_or_stage_write(self):
        self.api.rows[601] = dict(self.api.rows[501],id=601,originId=None,originatorId=None,title='Уже существующая аптека')
        self.say('/request 601')
        draft = self.store.session(200)
        self.assertEqual(draft['step'],'recipient')
        self.assertEqual(draft['link']['id'],601)
        link = self.work.link(200,601)
        row = self.store.create(200,300,link,'Заявка по существующей сделке','Проверить компьютер клиента','existing-request')
        self.assertEqual((row['deal_id'],row['deal_request']),(601,'b24-existing:200:601'))
        self.assertEqual(self.primary.db.execute('SELECT count(*) FROM operations').fetchone()[0],1)
        self.assertFalse(any(method in {'crm.item.add','crm.item.update'} for method,payload in self.api.calls))

    def test_foreign_existing_deal_is_not_linked_even_by_admin(self):
        self.api.rows[601] = dict(self.api.rows[501],id=601,originId=None,originatorId=None,assignedById=134)
        for actor in (100,200,500):
            with self.subTest(actor=actor),self.assertRaises(ConfigError):
                self.work.link(actor,601)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM fom_deal_references').fetchone()[0],0)

    def test_existing_deal_owner_change_at_send_prevents_request(self):
        self.api.rows[601] = dict(self.api.rows[501],id=601,originId=None,originatorId=None)
        self.say('/request 601')
        self.say('1')
        self.say('Проверить настройку')
        self.say('Не открывается приложение')
        self.api.rows[601]['assignedById'] = 134
        self.say(SEND)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM fom_requests').fetchone()[0],0)

    def test_recipient_pagination_keeps_original_numeric_choices(self):
        for user in range(600,612):
            employee = {'id':user+1000,'name':'Сотрудник '+str(user)}
            self.primary.request_registration(user,user,employee)
            self.primary.approve_registration(user,100,employee)
            self.store.grant(user,'tech',100)
        self.say('/request')
        self.say('1')
        self.say('👤 Конкретному сотруднику')
        recipients = deepcopy(self.store.session(200)['recipients'])
        self.assertIn(['/request_next'],self.telegram.messages[-1][2])
        self.say('/request_next')
        self.assertEqual(self.store.session(200)['recipient_page'],1)
        self.assertEqual(self.store.session(200)['recipients'],recipients)
        self.say('9')
        self.assertEqual(self.store.session(200)['recipient'],recipients[8]['telegram_id'])

    def test_admin_can_find_ticket_with_revoked_executor_in_inbox(self):
        row = self.ticket(300)
        self.primary.deny_registration(300,100,'revoked')
        self.assertEqual(self.store.listing(100)[0]['id'],row['id'])

    def test_no_bot_created_deals_can_start_existing_id_form_without_orphan_request(self):
        self.say('/request',500)
        self.assertEqual(self.store.session(500)['step'],'deal')
        self.say('ID 501',500)
        self.assertEqual(self.store.session(500)['step'],'deal')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM fom_requests').fetchone()[0],0)

    def test_deal_recovery_fallback_checks_original_marker_and_preserves_one_record(self):
        original = self.api.list_all
        def fallback(method,payload,**kwargs):
            if method=='crm.item.list':
                raise RemoteError('INVALID_LIST')
            if method=='crm.deal.list':
                self.assertEqual(payload['filter'],{'ORIGIN_ID':'verified-deal','ORIGINATOR_ID':SOURCE})
                return [{'ID':'501'}]
            return original(method,payload,**kwargs)
        self.api.list_all = fallback
        self.live.bot._configure(self.state)
        result = self.live.crm.find_request('deal','verified-deal')
        self.assertEqual(result['id'],501)
        self.assertFalse(any(method=='crm.item.add' for method,payload in self.api.calls))


if __name__=='__main__':
    unittest.main()
