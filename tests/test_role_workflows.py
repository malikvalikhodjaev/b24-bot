from datetime import datetime
from types import SimpleNamespace
import json
import unittest

import test_work_inbox as fixture
from datfo_crm_bot.app import COMMANDS, dispatch
from datfo_crm_bot.crm_list import hub, show
from datfo_crm_bot.i18n import LocalizedTelegram, language_context, tr
from datfo_crm_bot.language_ui import LanguageUI
from datfo_crm_bot.personal_statistics import MY_STATS
from datfo_crm_bot.registration import PROFILE
from datfo_crm_bot.role_access import RoleAccess
from datfo_crm_bot.service import DEAL, SUPPORT
from datfo_crm_bot.support_statistics import show as support_stats
from datfo_crm_bot.work_inbox import STATS as TECH_STATS


class RoleWorkflowTests(unittest.TestCase):
    setUp=fixture.WorkInboxTests.setUp
    tearDown=fixture.WorkInboxTests.tearDown
    ticket=fixture.WorkInboxTests.ticket

    def message(self, text, user=300, **extra):
        self.sequence+=1
        return {'update_id':self.sequence,'message':{'from':{'id':user},'chat':{'id':user,'type':'private'},'text':text,**extra}}

    def callback(self,data,user=300):
        self.sequence+=1
        return {'update_id':self.sequence,'callback_query':{'id':'q'+str(self.sequence),'from':{'id':user},
            'message':{'message_id':55,'chat':{'id':user,'type':'private'}},'data':data}}

    def labels(self,user):
        return [button if isinstance(button,str) else button['text'] for row in self.registration.keyboard(user) for button in row]

    def test_role_menus_and_commands_are_separate_in_both_languages(self):
        self.registration.okb_enabled=self.registration.personal_statistics_enabled=True
        for language in ('ru','uz'):
            with language_context(language):
                self.assertIn(MY_STATS,self.labels(200))
                self.assertNotIn(TECH_STATS,self.labels(200))
                self.assertIn(TECH_STATS,self.labels(300))
                self.assertNotIn(MY_STATS,self.labels(300))
                self.assertNotIn(DEAL,self.labels(300))
                self.assertNotIn(SUPPORT,self.labels(300))
                self.assertIn(PROFILE,self.labels(300))
        manager={row['command'] for row in self.registration.command_menu(200,COMMANDS)}
        technician={row['command'] for row in self.registration.command_menu(300,COMMANDS)}
        self.assertTrue({'deal','okb','my_stats'}<=manager)
        self.assertNotIn('requests_stats',manager)
        self.assertTrue({'requests','requests_stats','profile'}<=technician)
        self.assertFalse({'deal','okb','my_stats','stats','support'}&technician)
        self.store.grant(200,'off',100)
        self.assertFalse(self.registration.sales_allowed(200))
        self.assertNotIn(MY_STATS,self.labels(200))

    def test_old_sales_buttons_forms_and_callbacks_are_blocked_before_crm_and_replay(self):
        guard=RoleAccess(self.registration,self.telegram)
        original={'workflow':'live_deal','kind':'deal','request_id':'old-draft','step':'confirm'}
        self.primary.set_session(300,original)
        self.api.calls.clear()
        for text in ('/deal','/okb','/pharmacy','/support','/stats','/my_stats','/crm_deal 501','Подтвердить'):
            self.assertTrue(guard.handle(self.message(text)))
        for mode in ('deal','pharmacy','support'):
            self.assertTrue(guard.handle(self.message('',web_app_data={'data':json.dumps({'mode':mode,'token':'old'})})))
        for data in ('ld:card:501','ld:stages:501','personal_stats:all'):
            self.assertTrue(guard.handle(self.callback(data)))
        self.assertFalse(self.api.calls)
        self.assertEqual(self.primary.session(300),original)
        self.assertFalse(guard.handle(self.message('/profile')))
        self.assertFalse(guard.handle(self.callback('ld:list:support:0')))

    def test_stale_reply_keyboard_filters_actions_for_current_role(self):
        raw=[[DEAL,SUPPORT],[MY_STATS,TECH_STATS],['🏪 Добавить аптеку в ОКБ']]
        tech=self.registration.direct_form_keyboard(300,raw)
        self.assertEqual(tech,[[TECH_STATS]])
        sales=self.registration.direct_form_keyboard(200,raw)
        self.assertNotIn(TECH_STATS,str(sales))

    def test_old_profile_alias_works_in_an_active_request_without_consuming_draft(self):
        raw=self.telegram
        self.registration.telegram=LocalizedTelegram(raw,self.primary)
        ui=LanguageUI(self.registration,self.registration.telegram)
        self.store.set_session(300,{'step':'title','request_id':'preserve'})
        for language in ('ru','uz'):
            with self.primary.db:
                self.primary.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('language:300',language))
            dispatch(self.message(tr('Моя регистрация',language)),ui,[self.work.handle,self.live.handle,self.registration.handle])
            self.assertIn(tr(PROFILE,language).removeprefix('👤 '),raw.messages[-1][1])
            self.assertIn('#134',raw.messages[-1][1])
        self.assertEqual(self.store.session(300),{'step':'title','request_id':'preserve'})

    def test_registration_selects_role_before_approval_and_assigns_atomically(self):
        person={'id':777,'name':'Новый сотрудник'}
        self.primary.request_registration(700,700,person)
        self.registration.directory=SimpleNamespace(employee=lambda ident:person if ident==777 else None)
        body,buttons=self.registration.admin_route(100,'/approve','/approve 700')
        self.assertIn('Выберите рабочую роль',body)
        self.assertEqual(self.primary.registration(700)['status'],'pending')
        self.assertIsNone(self.store.role(700))
        tech=next(row[0] for row in buttons if 'системный администратор' in str(row[0]))
        self.registration.handle(self.message(tech,100))
        self.assertEqual(self.primary.registration(700)['status'],'approved')
        self.assertEqual(self.store.role(700),'tech')
        self.assertNotIn(DEAL,self.labels(700))

    def test_conflicting_b24_registration_does_not_assign_role(self):
        person={'id':133,'name':'Дубликат'}
        self.primary.request_registration(700,700,person)
        self.registration.directory=SimpleNamespace(employee=lambda ident:person)
        self.registration.admin_route(100,'/approve','/approve 700 tech')
        self.assertEqual(self.primary.registration(700)['status'],'pending')
        self.assertIsNone(self.store.role(700))

    def test_hub_has_two_rows_for_manager_and_only_requests_for_technician(self):
        hub(self.live,200)
        self.assertEqual([row[0]['callback_data'] for row in self.telegram.calls[-1][1]['reply_markup']['inline_keyboard']],['ld:list:deal:0','ld:list:support:0'])
        hub(self.live,300)
        self.assertEqual([row[0]['callback_data'] for row in self.telegram.calls[-1][1]['reply_markup']['inline_keyboard']],['ld:list:support:0'])

    def test_request_list_is_personal_and_does_not_include_admins_entire_inbox(self):
        own=self.ticket(100,'own')
        other=self.ticket(300,'other')
        show(self.live,100,self.registration.user(100),scope='support')
        body=self.telegram.calls[-1][1]['text']
        self.assertIn('Заявка #'+str(own['id'])+' ·',body)
        self.assertNotIn('Заявка #'+str(other['id'])+' ·',body)
        self.assertNotIn('#501',body)
        show(self.live,200,self.registration.user(200),scope='deal')
        self.assertIn('#501',self.telegram.calls[-1][1]['text'])
        self.assertNotIn('Заявка #',self.telegram.calls[-1][1]['text'])

    def test_support_statistics_periods_respect_tashkent_boundary_and_current_owner(self):
        for key,created in (('old','2026-09-30T20:00:00Z'),('week','2026-10-05T01:00:00Z'),('today','2026-10-07T20:00:00Z')):
            row=self.ticket(300,key)
            with self.primary.db:
                self.primary.db.execute('UPDATE fom_requests SET created_at=? WHERE id=?',(created,row['id']))
        # Sep 30 UTC 20:00 is already Oct 1 in Tashkent, so it belongs to the current month.
        now=datetime.fromisoformat('2026-10-08T16:14:00+05:00')
        self.api.calls.clear()
        for period,total in (('today',1),('week',2),('month',3),('all',3)):
            with language_context('ru'):
                support_stats(self.work,300,period,55,now=now)
            method,card=self.telegram.calls[-1]
            self.assertEqual(method,'editMessageText')
            self.assertIn('Назначено мне: '+str(total),card['text'])
            self.assertIn('На 08.10 · 16:14 · Ташкент',card['text'])
            self.assertEqual(len(card['reply_markup']['inline_keyboard']),3)
        self.assertFalse(any(method.startswith('crm.') for method,_ in self.api.calls))

    def test_manager_old_support_stats_callback_cannot_read_technician_stats(self):
        self.assertTrue(RoleAccess(self.registration,self.telegram).handle(self.callback('wr:stats:month',200)))
        self.assertEqual(self.telegram.calls[-1][0],'answerCallbackQuery')

    def test_command_publication_is_cached_and_role_change_refreshes_all_languages(self):
        self.registration.commands=COMMANDS
        self.registration.refresh_commands(300)
        initial=[payload for method,payload in self.telegram.calls if method=='setMyCommands']
        self.assertEqual(len(initial),3)
        self.registration.refresh_commands(300)
        self.assertEqual(sum(method=='setMyCommands' for method,_ in self.telegram.calls),3)
        self.store.grant(300,'fom_sales',100)
        self.registration.refresh_commands(300)
        current=[payload for method,payload in self.telegram.calls if method=='setMyCommands']
        self.assertEqual(len(current),6)
        self.assertIn('deal',{row['command'] for row in current[-1]['commands']})
        self.assertNotIn('requests_stats',{row['command'] for row in current[-1]['commands']})

    def test_technical_stats_buttons_edit_same_message_and_use_selected_language(self):
        self.ticket(300,'period-buttons')
        for language in ('ru','uz'):
            with language_context(language):
                self.assertTrue(self.work.handle(self.callback('wr:stats:week')))
            method,payload=self.telegram.calls[-1]
            self.assertEqual(method,'editMessageText')
            self.assertEqual(payload['message_id'],55)
            self.assertIn('Статистика за текущую неделю' if language=='ru' else 'Жорий ҳафта статистикаси',payload['text'])

    def test_identical_stats_refresh_does_not_edit_or_duplicate_message(self):
        now=datetime.fromisoformat('2026-10-08T16:14:00+05:00')
        support_stats(self.work,300,'week',55,now=now)
        card=self.telegram.calls[-1][1]
        count=len(self.telegram.calls)
        support_stats(self.work,300,'week',55,now=now,message=card)
        self.assertEqual(len(self.telegram.calls),count)


if __name__=='__main__':
    unittest.main()
