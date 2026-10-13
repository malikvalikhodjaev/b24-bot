"""Registration delivery, role decisions and recovery use only fake Telegram/CRM."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import unittest

import test_registration as fixture
from datfo_crm_bot.api import RemoteError
from datfo_crm_bot.app import COMMANDS, dispatch
from datfo_crm_bot.i18n import LocalizedTelegram, tr
from datfo_crm_bot.language_ui import LanguageUI
from datfo_crm_bot.registration import Registration, REQUESTS, MEMBERS
from datfo_crm_bot.config import User
from datfo_crm_bot.storage import Store
from datfo_crm_bot.work_inbox import WorkStore


class Telegram(fixture.Telegram):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.fail_recipient = None
        self.edit_failure = False

    def call(self, method, params):
        if method=='sendMessage' and params.get('chat_id')==self.fail_recipient:
            self.fail_recipient=None
            raise RemoteError('CONNECTION_ERROR',uncertain=True)
        if method=='editMessageText' and self.edit_failure:
            self.edit_failure=False
            raise RemoteError('CONNECTION_ERROR',uncertain=True)
        self.calls.append((method,deepcopy(params)))
        return {'message_id':len(self.calls)+100}


class RegistrationNoticeTests(unittest.TestCase):
    message = fixture.RegistrationTests.message
    say = fixture.RegistrationTests.say
    request = fixture.RegistrationTests.request
    tearDown = fixture.RegistrationTests.tearDown

    def setUp(self):
        fixture.RegistrationTests.setUp(self)
        self.telegram=Telegram()
        self.configure()
        with self.store.db:
            self.store.db.execute("INSERT OR REPLACE INTO settings VALUES('language:1','ru')")

    def configure(self):
        telegram=LocalizedTelegram(self.telegram,self.store)
        self.registration=Registration(self.settings,self.store,self.directory,telegram)
        self.registration.bootstrap()
        work=WorkStore(self.store,self.settings.admins)
        work.grant(1,'fom_sales',1)
        self.registration.work_inbox=SimpleNamespace(store=work,simulation=None,test_inbox=None)
        self.registration.live_deals_enabled=True

    def review(self, user=2):
        return self.registration.notices.latest(user)['id']

    def press(self, data, user=1, **changes):
        callback={'id':'button-'+str(len(self.telegram.calls)), 'from':{'id':user},
                  'message':{'message_id':50,'chat':{'id':user,'type':'private'}},'data':data}
        callback.update(changes)
        return self.registration.notices.handle_callback({'callback_query':callback})

    def notices(self, user=1):
        return [params for method,params in self.telegram.calls if method=='sendMessage' and params['chat_id']==user]

    def test_new_request_goes_only_to_admin_with_inline_accept_and_reject(self):
        self.request()
        self.registration.notices.flush(now=100)
        self.assertEqual(len(self.notices()),1)
        card=self.notices()[0]
        self.assertIn('Новая регистрация',card['text'])
        self.assertIn('Менеджер',card['text'])
        buttons=card['reply_markup']['inline_keyboard'][0]
        self.assertEqual([button['text'] for button in buttons],['✅ Принять','❌ Отклонить'])
        self.assertEqual(buttons[0]['callback_data'],'reg:choose:'+str(self.review()))
        self.assertEqual(self.notices(2),[])
        self.assertEqual(self.store.registration(2)['status'],'pending')
        self.assertIn('Администратор получит заявку в Telegram',self.telegram.messages[-1][1])

    def test_role_is_chosen_before_atomic_approval_and_user_receives_their_menu(self):
        self.request()
        ident=str(self.review())
        self.press('reg:choose:'+ident)
        self.assertEqual(self.store.registration(2)['status'],'pending')
        roles=self.telegram.calls[-1][1]['reply_markup']['inline_keyboard']
        self.assertTrue(any('reg:approve:'+ident+':tech'==b['callback_data'] for row in roles for b in row))
        self.press('reg:approve:'+ident+':tech')
        self.assertEqual(self.store.registration(2)['status'],'approved')
        self.assertEqual(self.registration.work_inbox.store.role(2),'tech')
        self.assertEqual(self.telegram.calls[-1][1]['reply_markup']['inline_keyboard'],[])
        self.registration.notices.flush(now=100)
        notice=self.notices(2)[0]
        self.assertIn('регистрация подтверждена',notice['text'])
        self.assertIn('системный администратор',notice['text'])
        self.assertNotIn('Открыть сделку',str(notice['reply_markup']))
        self.assertEqual(self.notices(1),[])

    def test_copied_group_mismatched_and_bot_callbacks_cannot_approve(self):
        self.request()
        data='reg:approve:'+str(self.review())+':fom_sales'
        for user,changes in ((2,{}),(3,{}),(1,{'message':{'message_id':50,'chat':{'id':-1,'type':'group'}}}),
                             (1,{'message':{'message_id':50,'chat':{'id':2,'type':'private'}}}),
                             (1,{'from':{'id':1,'is_bot':True}})):
            self.assertTrue(self.press(data,user,**changes))
            self.assertTrue(self.telegram.calls[-1][1]['show_alert'])
        self.assertEqual(self.store.registration(2)['status'],'pending')
        self.assertIsNone(self.registration.work_inbox.store.role(2))

    def test_reject_notifies_user_and_old_card_cannot_approve_reapplication(self):
        self.request()
        old=self.review()
        self.press('reg:reject:'+str(old))
        self.registration.notices.flush(now=100)
        self.assertEqual(self.store.registration(2)['status'],'rejected')
        self.assertIn('отклонена администратором',self.notices(2)[0]['text'])
        self.request()
        self.assertGreater(self.review(),old)
        self.press('reg:approve:'+str(old)+':tech')
        self.assertEqual(self.store.registration(2)['status'],'pending')
        self.assertTrue(self.telegram.calls[-1][1]['show_alert'])
        self.press('reg:approve:'+str(self.review())+':fom_sales')
        self.assertEqual(self.registration.work_inbox.store.role(2),'fom_sales')

    def test_rejected_card_can_be_accepted_after_selecting_role_without_reapplying(self):
        self.request()
        ident=str(self.review())
        self.press('reg:reject:'+ident)
        card=self.telegram.calls[-1][1]
        self.assertIn('Регистрация отклонена',card['text'])
        self.assertEqual(card['reply_markup']['inline_keyboard'],[[{'text':'✅ Принять','callback_data':'reg:restore:'+ident}]])
        self.registration.notices.flush(now=100)
        self.press('reg:restore:'+ident)
        self.assertEqual(self.store.registration(2)['status'],'rejected')
        buttons=self.telegram.calls[-1][1]['reply_markup']['inline_keyboard']
        self.assertTrue(any(b['callback_data']=='reg:restore_approve:'+ident+':fom_sales' for row in buttons for b in row))
        self.press('reg:restore_approve:'+ident+':fom_sales')
        self.registration.notices.flush(now=100)
        self.assertEqual(self.store.registration(2)['status'],'approved')
        self.assertEqual(self.registration.work_inbox.store.role(2),'fom_sales')
        self.assertEqual(self.review(),int(ident))
        self.assertEqual(len(self.notices(2)),2)
        self.assertIn('подтверждена',self.notices(2)[-1]['text'])

    def test_stale_pending_approval_cannot_restore_rejection_and_repeat_cannot_change_role(self):
        self.request()
        ident=str(self.review())
        self.press('reg:reject:'+ident)
        self.press('reg:approve:'+ident+':tech')
        self.assertEqual(self.store.registration(2)['status'],'rejected')
        self.press('reg:restore_approve:'+ident+':tech')
        self.press('reg:restore_approve:'+ident+':fom_sales')
        self.registration.notices.flush(now=100)
        self.assertEqual(self.registration.work_inbox.store.role(2),'tech')
        self.assertEqual(len(self.notices(2)),1)

    def test_restore_is_admin_only_and_old_restore_card_cannot_approve_new_application(self):
        self.request()
        ident=str(self.review())
        self.press('reg:reject:'+ident)
        self.press('reg:restore_approve:'+ident+':fom_sales',user=2)
        self.assertEqual(self.store.registration(2)['status'],'rejected')
        self.request()
        self.press('reg:restore_approve:'+ident+':tech')
        self.assertEqual(self.store.registration(2)['status'],'pending')
        self.assertIsNone(self.registration.work_inbox.store.role(2))

    def test_restore_still_checks_active_employee_and_conflicting_binding(self):
        self.request()
        ident=str(self.review())
        self.press('reg:reject:'+ident)
        employee=self.directory.rows.pop(20)
        self.press('reg:restore_approve:'+ident+':tech')
        self.assertEqual(self.store.registration(2)['status'],'rejected')
        self.directory.rows[20]=employee
        self.store.request_registration(3,3,employee)
        self.assertTrue(self.store.approve_registration(3,1,employee,work_role='tech'))
        self.press('reg:restore_approve:'+ident+':tech')
        self.assertEqual(self.store.registration(2)['status'],'rejected')
        self.assertIsNone(self.registration.work_inbox.store.role(2))

    def test_admin_list_and_legacy_approval_include_rejected_but_do_not_restore_revoked(self):
        self.request()
        self.press('reg:reject:'+str(self.review()))
        text,buttons=self.registration.admin_route(1,'/registrations','/registrations')
        self.assertIn('Регистрация отклонена',text)
        self.assertIn(['Подтвердить 2'],buttons)
        self.registration.admin_route(1,'/approve','/approve 2 tech')
        self.assertEqual(self.registration.work_inbox.store.role(2),'tech')
        self.registration.admin_route(1,'/revoke','/revoke 2')
        self.registration.admin_route(1,'/approve','/approve 2 fom_sales')
        self.assertEqual(self.store.registration(2)['status'],'revoked')
        self.assertIsNone(self.registration.user(2))

    def test_storage_restore_requires_expected_status_and_preserves_atomic_role_binding(self):
        self.request()
        self.press('reg:reject:'+str(self.review()))
        self.assertFalse(self.store.approve_registration(2,1,self.directory.rows[20],work_role='tech'))
        self.assertIsNone(self.registration.work_inbox.store.role(2))
        self.assertTrue(self.store.approve_registration(2,1,self.directory.rows[20],work_role='tech',expected_status='rejected'))
        self.assertFalse(self.store.approve_registration(2,1,self.directory.rows[20],work_role='fom_sales',expected_status='rejected'))
        self.assertEqual(self.registration.work_inbox.store.role(2),'tech')
        with self.assertRaises(ValueError):
            self.store.approve_registration(2,1,self.directory.rows[20],expected_status='revoked')

    def test_failed_delivery_is_persistent_retried_and_not_repeated_after_restart(self):
        self.request()
        ident=self.review()
        self.telegram.fail_recipient=1
        self.registration.notices.flush(now=100)
        self.assertEqual(self.notices(),[])
        notice=self.store.db.execute('SELECT * FROM registration_notices').fetchone()
        self.assertEqual((notice['state'],notice['attempts'],notice['retry_at']),('pending',1,130))
        self.store.close()
        self.store=Store(self.path)
        self.configure()
        self.assertEqual(self.review(),ident)
        self.registration.notices.flush(now=129)
        self.assertEqual(self.notices(),[])
        self.registration.notices.flush(now=130)
        self.registration.notices.recover()
        self.registration.notices.flush(now=200)
        self.assertEqual(len(self.notices()),1)

    def test_existing_pending_registration_is_backfilled_without_rewriting_it(self):
        employee={'id':20,'name':'Менеджер'}
        self.store.request_registration(2,2,employee)
        before=self.store.registration(2)
        self.registration.notices.recover()
        self.registration.notices.recover()
        self.registration.notices.flush(now=100)
        self.assertEqual(len(self.notices()),1)
        self.assertEqual(self.store.registration(2),before)

    def test_one_admin_delivery_failure_does_not_hide_notice_from_another_admin(self):
        self.directory.rows[30]={'id':30,'name':'Второй администратор'}
        self.registration.settings=replace(self.settings,admins={**self.settings.admins,4:User(30,True,'Второй администратор')})
        self.registration.bootstrap()
        self.request()
        self.telegram.fail_recipient=1
        self.registration.notices.flush(now=100)
        self.assertEqual(self.notices(1),[])
        self.assertEqual(len(self.notices(4)),1)
        self.registration.notices.flush(now=130)
        self.assertEqual(len(self.notices(1)),1)
        self.assertEqual(len(self.notices(4)),1)

    def test_interrupted_decision_is_recovered_and_stale_admin_notification_cancelled(self):
        self.request()
        self.assertTrue(self.store.approve_registration(2,1,self.directory.rows[20],work_role='tech'))
        self.registration.notices.recover()
        self.registration.notices.flush(now=100)
        self.assertEqual(self.notices(1),[])
        self.assertEqual(len(self.notices(2)),1)
        self.assertIn('системный администратор',self.notices(2)[0]['text'])

    def test_invalid_callback_and_nonadmin_commands_do_not_expose_registrations(self):
        self.request()
        for data in ('reg:choose:0','reg:approve:999','reg:approve:'+str(self.review())+':admin',
                     'reg:approve:'+str(self.review())):
            self.press(data)
        self.assertEqual(self.store.registration(2)['status'],'pending')
        self.assertIsNone(self.registration.work_inbox.store.role(2))
        self.assertFalse({'registrations','members'} & {r['command'] for r in self.registration.command_menu(2,COMMANDS)})
        self.assertFalse({'registrations','members'} & {r['command'] for r in self.registration.command_menu(None,COMMANDS)})

    def test_repeated_click_or_lost_card_edit_does_not_change_assigned_role(self):
        self.request()
        ident=str(self.review())
        self.telegram.edit_failure=True
        with self.assertRaises(RemoteError):
            self.press('reg:approve:'+ident+':tech')
        self.press('reg:approve:'+ident+':fom_sales')
        self.registration.notices.flush(now=100)
        self.assertEqual(self.registration.work_inbox.store.role(2),'tech')
        self.assertEqual(len(self.notices(2)),1)

    def test_b24_failure_inactive_employee_and_duplicate_employee_do_not_grant_role(self):
        self.request()
        ident=str(self.review())
        original=self.directory.employee
        def unavailable(employee_id):
            raise RemoteError('CONNECTION_ERROR')
        self.directory.employee=unavailable
        self.press('reg:approve:'+ident+':tech')
        self.assertIn('Б24 сейчас недоступен',self.telegram.calls[-1][1]['text'])
        self.directory.employee=lambda employee_id:None
        self.press('reg:approve:'+ident+':tech')
        self.assertEqual(self.store.registration(2)['status'],'pending')
        self.directory.employee=original
        self.store.request_registration(3,3,self.directory.rows[20])
        self.assertTrue(self.store.approve_registration(3,1,self.directory.rows[20],work_role='tech'))
        self.press('reg:approve:'+ident+':fom_sales')
        self.assertEqual(self.store.registration(2)['status'],'pending')
        self.assertIsNone(self.registration.work_inbox.store.role(2))

    def test_language_and_escaping_use_recipient_language_not_requesters_language(self):
        self.directory.rows[20]['name']='<b>Менеджер</b>'
        with self.store.db:
            self.store.db.execute("INSERT OR REPLACE INTO settings VALUES('language:2','uz')")
        self.request(name='Менеджер')
        self.registration.notices.flush(now=100)
        self.assertIn('&lt;b&gt;Менеджер&lt;/b&gt;',self.notices()[0]['text'])
        self.assertIn('Новая регистрация',self.notices()[0]['text'])
        self.press('reg:approve:'+str(self.review())+':fom_sales')
        self.registration.notices.flush(now=100)
        self.assertIn('тасдиқланди',self.notices(2)[0]['text'])
        self.assertIn('Сотув менежери',self.notices(2)[0]['text'])

    def test_callbacks_and_profile_admin_buttons_do_not_consume_working_drafts(self):
        self.request()
        self.store.set_session(1,{'step':'title','request_id':'sales-draft'})
        self.registration.work_inbox.store.set_session(1,{'step':'description','request_id':'support-draft'})
        ui=LanguageUI(self.registration,self.registration.telegram)
        update={'callback_query':{'id':'admin','from':{'id':1},'message':{'message_id':50,'chat':{'id':1,'type':'private'}},
                                'data':'reg:choose:'+str(self.review())}}
        invoked=[]
        dispatch(update,ui,[lambda value:invoked.append(value)])
        self.assertEqual(invoked,[])
        self.assertEqual(self.store.session(1)['request_id'],'sales-draft')
        self.assertEqual(self.registration.work_inbox.store.session(1)['request_id'],'support-draft')
        self.assertIn([REQUESTS,MEMBERS],self.registration.profile_keyboard(1))
        self.assertNotIn([REQUESTS,MEMBERS],self.registration.profile_keyboard(2))
        self.assertTrue({'registrations','members'} <= {r['command'] for r in self.registration.command_menu(1,COMMANDS)})
        for language in ('ru','uz'):
            self.assertEqual(ui.normalize(self.message(1,tr(REQUESTS,language)))['message']['text'],'/registrations')


if __name__=='__main__':
    unittest.main()
