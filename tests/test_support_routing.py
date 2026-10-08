"""Optional direct recipient, first queue submission and stable creator identity."""
import unittest

import test_miniapp_workflows as workflows
from datfo_crm_bot.communications import InboxError
from datfo_crm_bot.i18n import tr
from datfo_crm_bot.work_inbox import SEND, TO_POOL, TO_PERSON


class SupportRoutingTests(unittest.TestCase):
    setUp,tearDown=workflows.SupportFormTests.setUp,workflows.SupportFormTests.tearDown
    setUpBase=workflows.SupportFormTests.setUpBase
    send_form,token=workflows.SupportFormTests.send_form,workflows.SupportFormTests.token
    say=workflows.SupportFormTests.say

    def open(self):
        self.send_form({'token':self.token(),'mode':'support','pharmacy_kind':'existing','fom_id':'00081',
            'request_title':'Не работает программа','request_description':'Нужна помощь с подключением'})

    def test_queue_submission_has_no_owner_until_claim_and_real_stage_assignment(self):
        self.open()
        self.assertNotIn(200,[row['telegram_id'] for row in self.store.session(200)['recipients']])
        self.say(TO_POOL)
        self.say('🗺 R1 ҳудуди')
        self.say(SEND)
        row=self.store.listing(200)[0]
        self.assertEqual((row['owner'],row['status'],row['initial_recipient'],row['queue']),(None,'new',0,'r1'))
        remote=self.api.rows[self.sync.binding(row['id'])['crm_id']]
        self.assertEqual(remote['stageId'],self.settings.stages['r1']['id'])
        self.assertEqual(remote['assignedById'],self.primary.registration(200)['bitrix_id'])
        self.assertIn('Первый получатель: На распределение',remote['comments'])
        self.assertNotIn('Telegram ID',remote['comments'])
        recipients={row['recipient'] for row in self.primary.db.execute('SELECT recipient FROM fom_request_outbox')}
        self.assertIn(300,recipients)
        self.assertNotIn(200,recipients)
        self.assertNotIn(0,recipients)
        accepted=self.store.change(300,row['id'],row['version'],'take')
        self.sync.flush()
        self.assertEqual(accepted['ticket']['owner'],300)
        self.assertEqual(remote['assignedById'],self.primary.registration(300)['bitrix_id'])
        second=self.store.change(100,row['id'],row['version'],'take')
        self.assertEqual(second['ticket']['owner'],300)
        self.work.card(200,self.store.ticket(row['id']))
        self.assertNotIn('tg://user?id=0',self.telegram.calls[-1][1]['text'])

    def test_only_registered_author_can_submit_without_choosing_self(self):
        self.store.db.execute('DELETE FROM fom_request_members WHERE telegram_id<>200')
        self.store.db.commit()
        self.open()
        self.assertEqual(self.store.session(200)['recipients'],[])
        keyboard=self.telegram.messages[-1][2]
        self.assertIn([TO_POOL],keyboard)
        self.assertIn([TO_PERSON],keyboard)
        self.say(TO_POOL)
        self.say(SEND)
        self.assertEqual(self.store.listing(200)[0]['status'],'new')

    def test_direct_recipient_is_optional_and_self_cannot_be_forged(self):
        self.open()
        self.say(TO_PERSON)
        draft=self.store.session(200)
        number=next(i for i,row in enumerate(draft['technician_choices'],1) if row['bitrix_id']==134)
        self.say(str(number))
        self.say(SEND)
        row=self.store.listing(200)[0]
        self.assertEqual((row['owner'],row['status']),(300,'assigned'))
        link=self.work.intake.link(200,self.work.intake.sales.lookup_fom('00081'))
        with self.assertRaises(InboxError):
            self.store.create(200,200,link,'Проверить','Помощь нужна','self-assignment')

    def test_uzbek_route_buttons_work_and_queue_replay_is_idempotent(self):
        with self.primary.db:
            self.primary.db.execute("INSERT INTO settings(key,value) VALUES('language:200','uz') ON CONFLICT(key) DO UPDATE SET value='uz'")
        self.open()
        self.say(tr(TO_POOL,'uz'))
        self.say(SEND)
        row=self.store.listing(200)[0]
        link=self.work.intake.link(200,self.work.intake.sales.lookup_fom('00081'))
        repeated=self.store.create(200,None,link,row['title'],row['description'],row['request_id'],
            support_details=row['support_details'])
        self.assertEqual(repeated['id'],row['id'])
        self.assertEqual(len(self.store.listing(200)),1)

    def test_legacy_cached_self_choice_returns_to_route_without_creating(self):
        self.open()
        draft=self.store.session(200)
        draft.update(step='confirm',recipient=200)
        self.store.set_session(200,draft)
        self.say(SEND)
        self.assertEqual(self.store.session(200)['step'],'recipient')
        self.assertEqual(self.store.listing(200),[])


if __name__=='__main__':
    unittest.main()
