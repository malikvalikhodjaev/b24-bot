from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from datfo_crm_bot.app import dispatch
from datfo_crm_bot.guidance import Guide, GUIDE, TOPICS, FORM_HINTS
from datfo_crm_bot.i18n import LocalizedTelegram, language_context, stored_language, tr
from datfo_crm_bot.language_ui import LanguageUI, RU, UZ, SETTINGS
from datfo_crm_bot.service import DEAL, SUPPORT
from datfo_crm_bot.live_deals import MY_CRM
from datfo_crm_bot.storage import Store
from datfo_crm_bot.work_inbox import SEND
import test_work_inbox as work_fixture


class LanguageFlowTests(unittest.TestCase):
    def setUp(self):
        work_fixture.WorkInboxTests.setUp(self)
        self.raw = self.telegram
        self.telegram = LocalizedTelegram(self.raw, self.primary)
        self.registration.telegram = self.telegram
        self.work.telegram = self.telegram
        self.live.telegram = self.telegram
        self.live.bot.telegram.telegram = self.telegram
        self.ui = LanguageUI(self.registration, self.telegram)
        self.guide = Guide(self.registration, self.telegram, work_inbox=self.work)

    tearDown = work_fixture.WorkInboxTests.tearDown
    ticket = work_fixture.WorkInboxTests.ticket

    def update(self, text, user=200):
        self.sequence += 1
        return {'update_id':self.sequence, 'message':{'from':{'id':user},'chat':{'id':user,'type':'private'},'text':text}}

    def say(self, text, user=200):
        dispatch(self.update(text,user),self.ui,[self.guide.handle,self.work.handle,self.live.handle,self.registration.handle])

    def set_language(self, user, language):
        with self.primary.db:
            self.primary.db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',('language:'+str(user),language))

    def test_first_language_before_registration_and_persistence(self):
        self.say('/start',600)
        self.assertIsNone(self.primary.registration_session(600))
        self.assertIsNone(self.registration.user(600))
        self.assertEqual(self.raw.messages[-1][2],[[RU,UZ]])
        self.say(RU,600)
        self.assertEqual(stored_language(self.primary,600),'ru')
        self.assertIn(['Зарегистрироваться'],self.raw.messages[-1][2])
        other=Store(self.path)
        try:
            self.assertEqual(stored_language(other,600),'ru')
        finally:
            other.close()
        self.say('Зарегистрироваться',600)
        self.assertIn('Введите свои имя',self.raw.messages[-1][1])
        self.assertEqual(self.primary.registration_session(600),{'step':'name'})

    def test_switch_preserves_registration_sales_and_support_drafts(self):
        self.set_language(200,'ru')
        registration={'step':'choice','candidates':[{'id':133,'name':'Менеджер ФОМ'}]}
        sales={'workflow':'live_deal','step':'title','title':'Ҳолат:','request_id':'draft-only','kind':'deal'}
        support={'step':'title','title':'Муаллиф:','request_id':'draft-request'}
        self.primary.set_registration_session(200,registration)
        self.primary.set_session(200,sales)
        self.store.set_session(200,support)
        self.say(SETTINGS)
        self.say(UZ)
        self.assertEqual(self.primary.registration_session(200),registration)
        self.assertEqual(self.primary.session(200),sales)
        self.assertEqual(self.store.session(200),support)
        self.assertEqual(stored_language(self.primary,200),'uz')
        self.say('/next')
        self.assertIn('Заявка мавзусини',self.raw.calls[-1][1]['text'])
        self.assertEqual(self.store.session(200),support)
        self.assertIn([RU, UZ], self.raw.messages[-1][2])
        self.assertNotIn(['/start'], self.raw.messages[-1][2])

    def test_translated_support_buttons_complete_once_in_russian_and_uzbek(self):
        for lang in ('ru','uz'):
            self.set_language(200,lang)
            self.say('/request')
            self.assertEqual(self.store.session(200)['step'],'deal')
            self.say('1')
            self.say('2')
            self.say('Ҳолат:')
            self.say('Муаллиф: user text remains raw')
            self.say(tr(SEND,lang))
            row=self.store.listing(200)[0]
            self.assertEqual(row['title'],'Ҳолат:')
            self.assertEqual(row['description'],'Муаллиф: user text remains raw')
            self.assertEqual(row['deal_id'],501)
            self.assertIsNone(self.store.session(200))
        self.assertEqual(len(self.store.listing(200)),2)
        self.assertFalse(any(method in {'crm.item.add','crm.item.update'} for method,_ in self.api.calls))

    def test_outbox_each_recipient_language_and_user_text_preserved(self):
        self.set_language(200,'uz')
        self.set_language(300,'ru')
        row=self.store.create(200,300,self.work.link(200,501),'Ҳолат:','Ишланмоқда user body','mixed')
        with language_context('uz'):
            self.work.flush()
        delivery=[p for m,p in self.raw.calls if m=='sendMessage'][-1]
        self.assertEqual(delivery['chat_id'],300)
        self.assertIn('Статус: Ожидает принятия',delivery['text'])
        self.assertIn('Ҳолат:',delivery['text'])
        self.assertIn('Ишланмоқда user body',delivery['text'])
        self.assertIn('Сделка Б24 #501',delivery['text'])
        self.assertIn('до текущего момента',delivery['text'])
        self.assertNotIn('қуйида',delivery['text'])
        self.work.card(200,row)
        self.assertIn('Ҳолат: Қабул қилиш кутилмоқда',self.raw.calls[-1][1]['text'])

    def test_role_menu_help_and_old_buttons_remain_usable(self):
        self.set_language(200,'ru'); self.set_language(300,'uz')
        self.say('/start')
        labels=[b['text'] if isinstance(b,dict) else b for row in self.raw.messages[-1][2] for b in row]
        self.assertIn('➕ Открыть сделку',labels)
        self.assertIn('🛠 Открыть заявку в техподдержку',labels)
        self.assertIn('📂 Мои сделки и заявки',labels)
        self.say('/start',300)
        labels=[b['text'] if isinstance(b,dict) else b for row in self.raw.messages[-1][2] for b in row]
        self.assertNotIn(DEAL,labels); self.assertNotIn(SUPPORT,labels)
        for user,topic,expected in ((200,'sales','Что может менеджер продаж?'),(300,'tech','Техник нима қила олади?')):
            self.sequence += 1
            update={'update_id':self.sequence,'callback_query':{'id':'guide-'+str(self.sequence),'from':{'id':user},
                'message':{'chat':{'id':user,'type':'private'}},'data':'guide:'+topic}}
            dispatch(update,self.ui,[self.guide.handle])
            self.assertIn(expected,self.raw.calls[-1][1]['text'])
        self.say('🛠 Техник ёрдам сделкаси')
        self.assertIsNotNone(self.store.session(200))
        before=deepcopy(self.store.session(200))
        self.say('📖 Йўриқнома')
        self.assertEqual(self.store.session(200),before)
        self.assertIn('Помощь по боту',self.raw.calls[-1][1]['text'])

    def test_my_tasks_lists_are_separate_and_exclude_archive(self):
        self.set_language(200,'ru')
        row=self.ticket(300)
        self.say(tr(MY_CRM,'ru'))
        self.assertIn('Мои сделки и заявки',self.raw.calls[-1][1]['text'])
        self.live.handle({'callback_query':{'id':'deals','from':{'id':200},'message':{'chat':{'id':200,'type':'private'}},'data':'ld:list:deal:0'}})
        self.assertTrue(any(m=='crm.item.get' for m,_ in self.api.calls))
        self.live.handle({'callback_query':{'id':'requests','from':{'id':200},'message':{'chat':{'id':200,'type':'private'}},'data':'ld:list:support:0'}})
        self.assertTrue(any('Заявка #'+str(row['id']) in p['text'] for m,p in self.raw.calls if m=='sendMessage'))
        with self.primary.db:
            self.primary.db.execute('UPDATE fom_requests SET archived=1 WHERE id=?',(row['id'],))
        self.raw.calls.clear()
        self.say('📂 Б24 ёзувларим')
        self.live.handle({'callback_query':{'id':'requests-archived','from':{'id':200},'message':{'chat':{'id':200,'type':'private'}},'data':'ld:list:support:0'}})
        self.assertFalse(any('Заявка #'+str(row['id']) in p['text'] for m,p in self.raw.calls if m=='sendMessage'))

    def test_public_help_hints_and_commands_are_translated_without_test_topics(self):
        from datfo_crm_bot.app import COMMANDS
        for source in [*TOPICS.values(),*FORM_HINTS.values(),*[r['description'] for r in COMMANDS]]:
            self.assertNotEqual(tr(source,'ru'),tr(source,'uz'),source)
        self.assertNotIn('guide:inbox',str(self.guide.registration.keyboard(200)))

    def test_topic_navigation_preserves_forms_and_request_opens_technician_help(self):
        row = self.ticket(300)
        registration = {'step':'name'}
        sales = {'workflow':'okb','step':'okb_inn','request_id':'help-pharmacy'}
        support = {'step':'description','request_id':'help-request'}
        self.primary.set_registration_session(200,registration)
        self.primary.set_session(200,sales)
        self.store.set_session(200,support)

        def open_topic(user, topic):
            self.sequence += 1
            update = {'update_id':self.sequence,'callback_query':{
                'id':'help-'+str(self.sequence),'from':{'id':user},
                'message':{'chat':{'id':user,'type':'private'}},'data':'guide:'+topic}}
            dispatch(update,self.ui,[self.guide.handle])
            return self.raw.calls[-1][1]

        for language in ('ru','uz'):
            self.set_language(200,language)
            self.set_language(300,language)
            page = open_topic(200,'sales')
            links = {button['callback_data'] for buttons in page['reply_markup']['inline_keyboard'] for button in buttons}
            self.assertTrue({'guide:deal','guide:okb','guide:work_inbox','guide:crm','guide:statistics'} <= links)
            for topic in ('deal','okb','work_inbox','crm','statistics','home'):
                page = open_topic(200,topic)
                self.assertEqual(page['text'],tr(TOPICS[topic],language))
                self.assertEqual(self.primary.registration_session(200),registration)
                self.assertEqual(self.primary.session(200),sales)
                self.assertEqual(self.store.session(200),support)
            self.work.card(300,row)
            links = {button['callback_data'] for buttons in self.raw.calls[-1][1]['reply_markup']['inline_keyboard'] for button in buttons}
            self.assertIn('guide:tech',links)
            self.assertNotIn('guide:work_inbox',links)
            page = open_topic(300,'tech')
            self.assertEqual(page['text'],tr(TOPICS['tech'],language))
            self.assertEqual(self.store.ticket(row['id'])['status'],'assigned')
        self.assertFalse(any(method in {'crm.item.add','crm.item.update'} for method,_ in self.api.calls))

    def test_group_language_selection_does_not_change_preferences(self):
        update=self.update(RU)
        update['message']['chat']={'id':-10,'type':'group'}
        self.assertFalse(self.ui.handle(update))
        self.assertIsNone(stored_language(self.primary,200))


if __name__=='__main__':
    unittest.main()
