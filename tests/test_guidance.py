from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from datfo_crm_bot.api import RemoteError
from datfo_crm_bot.communications import InboxStore, InboxTest, NEW, SEND, CANCEL_REQUEST, GUIDE
from datfo_crm_bot.config import User
from datfo_crm_bot.guidance import Guide
from datfo_crm_bot.okb_access import OkbTelegram
from datfo_crm_bot.registration import Registration, RegistrationSettings
from datfo_crm_bot.simulation import Simulation
from datfo_crm_bot.storage import Store
from test_communications import FakeTelegram, NoDirectory


class GuidanceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.primary = Store(root / 'primary.sqlite3')
        self.primary.request_registration(100,100,{'id':132,'name':'Администратор'})
        self.primary.approve_registration(100,100,{'id':132,'name':'Администратор'})
        self.telegram = FakeTelegram()
        self.registration = Registration(RegistrationSettings({100:User(132,True,'Администратор')}),self.primary,NoDirectory(),self.telegram)
        self.inbox_store = InboxStore(root / 'inbox.sqlite3')
        self.sim_store = Store(root / 'simulation.sqlite3')
        self.simulation = Simulation(self.registration,self.sim_store,self.telegram)
        self.inbox = InboxTest(self.registration,self.inbox_store,self.telegram,self.simulation)
        self.guide = Guide(self.registration,self.telegram,self.inbox,self.simulation)
        self.sequence = 0

    def tearDown(self):
        self.inbox_store.close()
        self.sim_store.close()
        self.primary.close()
        self.folder.cleanup()

    def message(self,text,user=100):
        self.sequence += 1
        return {'update_id':self.sequence,'message':{'from':{'id':user},'chat':{'id':user,'type':'private'},'text':text}}

    def dispatch(self,text):
        update = self.message(text)
        if not self.guide.handle(update) and not self.inbox.handle(update) and not self.simulation.handle(update):
            self.registration.handle(update)

    def help_callback(self,topic='next',user=100):
        self.sequence += 1
        return {'update_id':self.sequence,'callback_query':{'id':'guide-'+str(self.sequence),'from':{'id':user},'message':{'chat':{'id':user,'type':'private'}},'data':'guide:'+topic}}

    def test_guide_at_each_inbox_step_preserves_form_and_uzbek_flow_creates_once(self):
        self.dispatch('/inbox_test')
        self.dispatch(NEW)
        for number,text in enumerate(('Умумий','Дастур очилмаяпти','Дорихонада дастурни текшириб бериш керак',SEND),1):
            before = self.inbox_store.mode(100)
            self.dispatch(GUIDE)
            self.guide.handle(self.help_callback())
            self.assertEqual(self.inbox_store.mode(100),before)
            self.assertIn(str(number)+'/4',self.telegram.calls[-1][1]['text'])
            # Inline help preserves the form's existing reply keyboard.
            self.assertIn('inline_keyboard',self.telegram.calls[-1][1]['reply_markup'])
            self.assertNotIn('keyboard',self.telegram.calls[-1][1]['reply_markup'])
            self.dispatch(text)
        rows = self.inbox_store.listing(100,'sales')
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['queue'],'general')
        self.assertEqual(self.primary.db.execute('select count(*) from operations').fetchone()[0],0)

    def test_old_russian_buttons_continue_and_cancel_is_available_after_error(self):
        for text in ('/inbox_test','📝 Новая заявка','Общая','Тестовый компьютер','Нужно настроить приложение','Отправить заявку'):
            self.dispatch(text)
        self.assertEqual(len(self.inbox_store.listing(100,'sales')),1)
        self.dispatch(NEW)
        self.dispatch('неверная очередь')
        self.assertIn([CANCEL_REQUEST],self.telegram.messages[-1][2])
        self.dispatch(CANCEL_REQUEST)
        self.assertIsNone(self.inbox_store.mode(100)['draft'])

    def test_help_during_registration_preserves_candidates_without_directory_calls(self):
        state = {'step':'choice','candidates':[{'id':20,'name':'Менежер <А>'}]}
        self.primary.set_registration_session(200,state)
        self.guide.handle(self.message('/help@fom_bitrix_bot',200))
        self.guide.handle(self.help_callback(user=200))
        self.assertEqual(self.primary.registration_session(200),state)
        self.assertIn('&lt;А&gt;',self.telegram.calls[-1][1]['text'])
        self.assertIn([GUIDE],self.registration.keyboard(200))

    def test_help_in_old_simulation_does_not_exit_or_advance_form(self):
        self.dispatch('/test')
        self.dispatch('/okb')
        self.sim_store.set_session(100, {**self.sim_store.session(100), 'step': 'okb_inn'})
        before = self.sim_store.session(100)
        self.dispatch('/guide')
        self.dispatch('/next')
        self.assertTrue(self.simulation.active(100))
        self.assertEqual(self.sim_store.session(100),before)
        self.assertIn('ИНН',self.telegram.calls[-1][1]['text'])
        self.assertEqual(self.primary.db.execute('select count(*) from operations').fetchone()[0],0)

    def test_okb_help_reads_existing_choices_and_uncertain_save_does_not_suggest_confirm(self):
        state = {'workflow':'okb','kind':'pharmacy','step':'okb_company','request_id':'guide-okb',
                 'candidates':[{'id':1,'title':'Фирма <А>'}]}
        self.primary.set_session(100,state)
        self.dispatch('/next')
        self.assertIn('&lt;А&gt;',self.telegram.calls[-1][1]['text'])
        self.assertEqual(self.primary.session(100),state)
        state['step'] = 'confirm'
        self.primary.prepare(100,state,'2026-10-04T10:00:00+00:00')
        self.primary.status('guide-okb','uncertain')
        self.primary.set_session(100,state)
        self.dispatch('/next')
        self.assertIn('/pending',self.telegram.calls[-1][1]['text'])
        self.assertNotIn('Подтвердить',self.telegram.calls[-1][1]['text'])
        OkbTelegram(self.registration,self.telegram).send(100,'Сохранение не подтверждено',[['Проверить сохранение']])
        self.assertIn('/pending',self.telegram.messages[-1][1])
        self.assertNotIn('Подтвердить',self.telegram.messages[-1][1])
        self.assertEqual(self.primary.operation('guide-okb')['status'],'uncertain')

    def test_revoked_access_and_groups_do_not_expose_saved_form_through_help(self):
        self.primary.set_session(100,{'step':'okb_company','candidates':[{'title':'Скрытая фирма'}]})
        self.primary.deny_registration(100,100,'revoked')
        self.guide.handle(self.message('/next'))
        self.assertNotIn('Скрытая фирма',self.telegram.calls[-1][1]['text'])
        self.assertIn('Кириш ҳуқуқи',self.telegram.calls[-1][1]['text'])
        update = self.message('/guide')
        update['message']['chat'] = {'id':-1,'type':'group'}
        self.assertFalse(self.guide.handle(update))

    def test_expired_guide_callback_still_shows_help_without_writing(self):
        self.dispatch('/inbox_test')
        self.dispatch(NEW)
        before = self.inbox_store.mode(100)
        self.telegram.fail_answer = True
        self.assertTrue(self.guide.handle(self.help_callback('inbox')))
        self.assertEqual(self.inbox_store.mode(100),before)
        self.assertEqual(len(self.inbox_store.listing(100,'sales')),0)

    def test_next_step_after_real_test_entry_points_to_real_form_without_advancing(self):
        with self.primary.db:
            self.primary.db.execute("INSERT INTO settings VALUES('b24-test:100','1')")
        self.registration.live_deals_enabled = True
        self.dispatch('/next')
        self.assertIn('/deal',self.telegram.calls[-1][1]['text'])
        self.assertIn('Ҳақиқий Б24',self.telegram.calls[-1][1]['text'])
        self.assertIsNone(self.primary.session(100))


if __name__ == '__main__':
    unittest.main()
