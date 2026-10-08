from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1] / 'src'))
from datfo_crm_bot.api import RemoteError, Telegram
from datfo_crm_bot.communications import InboxStore, InboxTest, InboxError, NOTICE, person_label, SALES, TECH
from datfo_crm_bot.config import User
from datfo_crm_bot.registration import Registration, RegistrationSettings
from datfo_crm_bot.storage import Store
from datfo_crm_bot.simulation import Simulation


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.calls = []
        self.fail_reply = False
        self.fail_inline = False
        self.fail_answer = False

    def send(self, chat_id, text, keyboard=None):
        if self.fail_reply and not text.startswith('⏳ Подождите') and not text.startswith('⏳ Кутинг'):
            self.fail_reply = False
            raise RemoteError('CONNECTION_ERROR',uncertain=True)
        self.messages.append((chat_id,text,keyboard))

    def call(self, method, payload=None, **kwargs):
        self.calls.append((method,payload))
        if method == 'sendMessage' and self.fail_inline:
            self.fail_inline = False
            raise RemoteError('CONNECTION_ERROR',uncertain=True)
        if method == 'answerCallbackQuery' and self.fail_answer:
            self.fail_answer = False
            raise RemoteError('TELEGRAM_400')
        return {'message_id':len(self.calls)} if method == 'sendMessage' else True


class NoDirectory:
    def employee(self,*args):
        raise AssertionError('Inbox must not call Bitrix')
    def search(self,*args):
        raise AssertionError('Inbox must not call Bitrix')


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name)/'inbox.sqlite3'
        self.store = InboxStore(self.path)
        self.store.bootstrap(100)
        self.primary = Store(Path(self.folder.name)/'primary.sqlite3')
        self.primary.request_registration(100,100,{'id':132,'name':'Администратор'})
        self.primary.approve_registration(100,100,{'id':132,'name':'Администратор'})
        self.telegram = FakeTelegram()
        self.registration = Registration(RegistrationSettings({100:User(132,True,'Администратор')}),self.primary,NoDirectory(),self.telegram)
        self.inbox = InboxTest(self.registration,self.store,self.telegram)
        self.sequence = 0

    def tearDown(self):
        self.store.close()
        self.primary.close()
        self.folder.cleanup()

    def ticket(self, queue='general', request='request-1'):
        return self.store.create(100,'sales',queue,'Тестовая заявка','Проверить подключение программы',request)

    def say(self,text,user=100,chat=None):
        self.sequence += 1
        update = {'update_id':self.sequence,'message':{'from':{'id':user},'chat':chat or {'id':user,'type':'private'},'text':text}}
        return self.inbox.handle(update),update

    def callback(self,row,action='take',role='tech',target='-',user=100):
        self.sequence += 1
        return {'update_id':self.sequence,'callback_query':{'id':'query-'+str(self.sequence),'from':{'id':user},'message':{'chat':{'id':user,'type':'private'}},'data':f"ib:{role}:{row['id']}:{row['version']}:{action}:{target}"}}

    def test_replayed_create_uses_one_ticket_and_one_event(self):
        first = self.ticket()
        second = self.ticket()
        self.assertEqual(first['id'],second['id'])
        self.assertEqual(self.store.db.execute('select count(*) from inbox_events').fetchone()[0],1)

    def test_two_connections_simultaneously_claim_only_one_wins(self):
        row = self.ticket()
        barrier = threading.Barrier(2)
        def contender(role):
            connection = InboxStore(self.path)
            try:
                barrier.wait(timeout=10)
                return connection.change(100,role,row['id'],'take',0)
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(contender,role) for role in ('sales','tech')]
            results = [future.result(timeout=20) for future in futures]
        self.assertEqual(sum(result['changed'] for result in results),1)
        loser = next(result for result in results if not result['changed'])
        self.assertIn('уже взял',loser['message'])
        self.assertEqual(self.store.db.execute('select count(*) from inbox_events').fetchone()[0],2)

    def test_dispatcher_assigns_then_technician_accepts(self):
        row = self.ticket()
        row = self.store.change(100,'sales',row['id'],'triage',0)['ticket']
        row = self.store.change(100,'sales',row['id'],'assign',row['version'],'tech')['ticket']
        self.assertEqual((row['owner'],row['status'],row['queue']),('tech','assigned','tech'))
        row = self.store.change(100,'tech',row['id'],'take',row['version'])['ticket']
        self.assertEqual(row['status'],'working')
        self.assertTrue(self.store.visible(row,'sales'))

    def test_stale_assignment_cannot_replace_current_executor(self):
        row = self.ticket()
        triage = self.store.change(100,'sales',row['id'],'triage',0)['ticket']
        self.store.change(100,'sales',row['id'],'assign',triage['version'],'tech')
        with self.assertRaises(InboxError):
            self.store.change(100,'sales',row['id'],'assign',triage['version'],'sales')
        self.assertEqual(self.store.ticket(100,row['id'])['owner'],'tech')

    def test_only_owner_closes_or_releases_and_old_button_cannot_release_new_claim(self):
        row = self.ticket()
        first = self.store.change(100,'tech',row['id'],'take',0)['ticket']
        for action in ('close','release'):
            with self.subTest(action=action),self.assertRaises(InboxError):
                self.store.change(100,'sales',row['id'],action,first['version'])
        released = self.store.change(100,'tech',row['id'],'release',first['version'])['ticket']
        latest = self.store.change(100,'tech',row['id'],'take',released['version'])['ticket']
        with self.assertRaises(InboxError):
            self.store.change(100,'tech',row['id'],'release',first['version'])
        closed = self.store.change(100,'tech',row['id'],'close',latest['version'])['ticket']
        self.assertEqual(closed['status'],'closed')
        with self.assertRaises(InboxError):
            self.store.change(100,'tech',row['id'],'take',closed['version'])

    def test_queues_and_team_boundaries_are_checked_in_storage(self):
        row = self.ticket('tech')
        with self.assertRaises(InboxError):
            self.store.change(100,'sales',row['id'],'take',0)
        with self.assertRaises(InboxError):
            self.store.change(200,'tech',row['id'],'take',0)
        self.assertEqual(self.store.ticket(100,row['id'])['status'],'new')

    def test_restart_preserves_tickets_owner_and_profile(self):
        row = self.ticket()
        self.store.change(100,'tech',row['id'],'take',0)
        self.store.set_mode(100,'tech')
        self.store.close()
        self.store = InboxStore(self.path)
        self.assertEqual(self.store.ticket(100,row['id'])['owner'],'tech')
        self.assertEqual(self.store.mode(100)['role'],'tech')

    def test_full_bot_form_switching_conflict_and_no_bitrix_writes(self):
        self.primary.set_session(100,{'kind':'pharmacy','step':'title'})
        self.say('/inbox_test')
        for text in ('/request','Общая','Тестовый компьютер','Нужно настроить приложение','Отправить заявку'):
            self.say(text)
        row = self.store.listing(100,'sales')[0]
        self.say(TECH)
        self.inbox.handle(self.callback(row))
        self.say(SALES)
        self.inbox.handle(self.callback(row,role='sales'))
        self.assertIn('уже взял',self.telegram.calls[-2][1]['text'])
        self.assertEqual(self.store.ticket(100,row['id'])['owner'],'tech')
        self.assertEqual(self.primary.session(100),{'kind':'pharmacy','step':'title'})
        self.assertEqual(self.primary.db.execute('select count(*) from operations').fetchone()[0],0)
        self.assertTrue(all(text.startswith(NOTICE) for _,text,_ in self.telegram.messages))

    def test_lost_result_reply_does_not_create_a_second_ticket(self):
        self.say('/inbox_test')
        for text in ('/request','Общая','Тестовый компьютер','Нужно настроить приложение'):
            self.say(text)
        self.telegram.fail_reply = True
        with self.assertRaises(RemoteError):
            _,update = self.say('Отправить заявку')
        # A new button message after a lost result must not create another ticket.
        self.say('Отправить заявку')
        self.assertEqual(len(self.store.listing(100,'sales')),1)

    def test_lost_form_reply_replayed_update_does_not_advance_twice(self):
        self.say('/inbox_test')
        self.say('/request')
        self.telegram.fail_reply = True
        update = {'update_id':777,'message':{'from':{'id':100},'chat':{'id':100,'type':'private'},'text':'Общая'}}
        with self.assertRaises(RemoteError):
            self.inbox.handle(update)
        self.assertEqual(self.store.mode(100)['draft']['step'],'title')
        self.inbox.handle(update)
        self.assertEqual(self.store.mode(100)['draft']['step'],'title')
        self.assertNotIn('title',self.store.mode(100)['draft'])

    def test_update_interrupted_before_reply_is_not_applied_again(self):
        self.say('/inbox_test')
        self.say('/request')
        self.store.begin_reply(888,100)
        draft = self.store.mode(100)['draft']
        draft.update(queue='general',step='title')
        self.store.set_mode(100,'sales',draft)
        self.inbox.handle({'update_id':888,'message':{'from':{'id':100},'chat':{'id':100,'type':'private'},'text':'Общая'}})
        self.assertEqual(self.store.mode(100)['draft']['step'],'title')
        self.assertIn('2/4',self.telegram.messages[-1][1])

    def test_callback_replay_and_expired_answer_never_reassign(self):
        row = self.ticket()
        self.store.set_mode(100,'tech')
        update = self.callback(row)
        self.telegram.fail_answer = True
        self.inbox.handle(update)
        self.inbox.handle(update)
        self.assertEqual(self.store.ticket(100,row['id'])['version'],1)

    def test_wrong_profile_callback_does_not_impersonate_other_role(self):
        row = self.ticket()
        self.store.set_mode(100,'sales')
        self.inbox.handle(self.callback(row))
        self.assertIsNone(self.store.ticket(100,row['id'])['owner'])
        self.assertIn('Переключитесь',self.telegram.calls[-1][1]['text'])

    def test_revoked_nonadmin_and_group_access_are_blocked(self):
        self.say('/inbox_test')
        self.primary.deny_registration(100,100,'revoked')
        self.say('/inbox')
        self.assertIsNone(self.store.mode(100))
        self.assertIn('администратору',self.telegram.messages[-1][1])
        self.assertFalse(self.say('/inbox_test',chat={'id':-1,'type':'group'})[0])
        self.say('/inbox_test',user=200)
        self.assertIsNone(self.store.mode(200))

    def test_notification_loss_is_recorded_and_inbox_remains_available(self):
        row = self.ticket()
        self.telegram.fail_inline = True
        self.inbox.flush()
        status = self.store.db.execute('select state from inbox_outbox').fetchone()[0]
        self.assertEqual(status,'uncertain')
        count = len(self.telegram.calls)
        self.inbox.flush()
        self.assertEqual(count,len(self.telegram.calls))
        self.assertEqual(self.store.listing(100,'tech')[0]['id'],row['id'])
        self.store.set_mode(100,'sales')
        self.say('/inbox_resend')
        self.assertEqual(self.store.db.execute('select state from inbox_outbox').fetchone()[0],'sent')

    def test_switching_between_simulations_never_leaves_two_active_modes(self):
        simulation_store = Store(Path(self.folder.name)/'crm-simulation.sqlite3')
        try:
            simulation = Simulation(self.registration,simulation_store,self.telegram)
            self.inbox.simulation = simulation
            simulation.set_active(100,True)
            self.say('/inbox_test')
            self.assertFalse(simulation.active(100))
            handled, update = self.say('/test')
            self.assertFalse(handled)
            simulation.handle(update)
            self.assertTrue(simulation.active(100))
            self.assertIsNone(self.store.mode(100))
        finally:
            simulation_store.close()

    def test_exit_preserves_tickets_and_registration(self):
        self.say('/inbox_test')
        row = self.ticket()
        self.say('/end_inbox_test')
        self.assertIsNone(self.store.mode(100))
        self.assertEqual(self.store.ticket(100,row['id'])['status'],'new')
        self.assertIsNotNone(self.registration.user(100))


class TelegramContractTests(unittest.TestCase):
    def test_getupdates_requests_callback_queries(self):
        telegram = Telegram('test-token')
        calls = []
        telegram.call = lambda method,payload,**kwargs: calls.append((method,payload)) or []
        telegram.updates(25)
        self.assertEqual(calls[0][1]['allowed_updates'],['message','callback_query'])

    def test_username_optional_safe_profile_link_and_html(self):
        self.assertEqual(person_label({'username':'test_person','name':'Имя'}),'@test_person')
        self.assertEqual(person_label({'telegram_id':123,'name':'<Имя>'}),'<a href="tg://user?id=123">&lt;Имя&gt;</a>')
        self.assertEqual(person_label({'name':'<Тест>'}),'&lt;Тест&gt;')


if __name__ == '__main__':
    unittest.main()
