"""Directory assignment works for employees before Telegram registration, without duplicate writes."""
from copy import deepcopy
import json
import unittest

import test_miniapp_workflows as workflows
from datfo_crm_bot.api import RemoteError
from datfo_crm_bot.communications import InboxError
from datfo_crm_bot.i18n import tr
from datfo_crm_bot.technicians import TechnicianDirectory, search
from datfo_crm_bot.work_inbox import SEND, TO_PERSON, TO_POOL, ALL_TECHNICIANS, REFRESH_TECHNICIANS


def employee(ident, name, surname='', department=60, **extra):
    return {'ID':str(ident), 'ACTIVE':True, 'USER_TYPE':'employee', 'NAME':name,
            'LAST_NAME':surname, 'UF_DEPARTMENT':[department], **extra}


class TechnicianTests(unittest.TestCase):
    setUp,tearDown=workflows.SupportFormTests.setUp,workflows.SupportFormTests.tearDown
    setUpBase=workflows.SupportFormTests.setUpBase
    send_form,token=workflows.SupportFormTests.send_form,workflows.SupportFormTests.token
    say=workflows.SupportFormTests.say

    def open(self):
        self.api.technicians = [employee(134,'Техник','Подключён'), employee(701,'Арсений','Черков'),
                                employee(702,'Вахтанг','Джишиашвили',61), employee(703,'Радик','Хасянов')]
        self.send_form({'token':self.token(),'mode':'support','pharmacy_kind':'existing','fom_id':'00081',
            'request_title':'Не работает программа','request_description':'Нужна помощь с подключением'})
        self.say(TO_PERSON)

    def choose(self, ident):
        draft=self.store.session(200)
        index=next(n for n,row in enumerate(draft['technician_choices'],1) if row['bitrix_id']==ident)
        self.say(str(index))

    def test_structure_filters_inactive_external_and_other_departments_and_retains_duplicates(self):
        self.open()
        self.api.technicians += [employee(800,'Уволен',ACTIVE=False), employee(801,'Партнёр',USER_TYPE='extranet'),
            employee(802,'Продажи',department=20), employee(803,'ПРОФКЕЙС',department=17),
            employee(804,'Без отдела',UF_DEPARTMENT=None), employee(805,'Elyor','Alimov',62),
            employee(806,'Elyor','Alimov',62)]
        rows=self.sync.technicians.listing(force=True)
        self.assertEqual({row['bitrix_id'] for row in rows},{134,701,702,703,805,806})
        self.assertEqual(len(search(rows,'alimov')),2)
        self.assertEqual([row['bitrix_id'] for row in search(rows,'Вахтанг',2)],[702])
        rows[0]['name']='Changed cache copy'
        self.assertNotEqual(self.sync.technicians.listing()[0]['name'],'Changed cache copy')

    def test_search_number_selection_creates_request_on_unconnected_bitrix_technician(self):
        self.open()
        self.say('арсе')
        self.assertEqual([row['bitrix_id'] for row in self.store.session(200)['technician_choices']],[701])
        self.say('1')
        self.assertEqual(self.store.session(200)['step'],'confirm')
        self.say(SEND)
        row=self.store.listing(200)[0]
        remote=self.api.rows[self.sync.binding(row['id'])['crm_id']]
        self.assertEqual((row['owner'],row['status'],row['initial_recipient']),(None,'assigned',0))
        self.assertEqual(remote['assignedById'],701)
        self.assertEqual(row['queue'],'r1')
        self.assertIn('Арсений Черков',remote['comments'])
        self.assertIn('Техник назначен в Б24',self.telegram.calls[-1][1]['text'])
        self.assertEqual(list(self.primary.db.execute('SELECT recipient FROM fom_request_outbox')),[])
        self.assertFalse(self.store.visible(row,300))
        with self.assertRaises(InboxError):
            self.store.change(300,row['id'],row['version'],'take')
        self.say(SEND)
        self.assertEqual(sum(method=='crm.item.add' for method,_ in self.api.calls),1)

    def test_connected_technician_is_assigned_and_receives_notification(self):
        self.open(); self.choose(134); self.say(SEND)
        row=self.store.listing(200)[0]
        self.assertEqual(row['owner'],300)
        self.assertEqual(self.api.rows[self.sync.binding(row['id'])['crm_id']]['assignedById'],134)
        self.assertTrue(any(payload.get('chat_id')==300 for method,payload in self.telegram.calls if method=='sendMessage'))
        self.assertTrue(self.store.change(300,row['id'],row['version'],'take')['changed'])

    def test_region_filter_and_search_reset_and_missing_name_have_clear_feedback(self):
        self.open(); self.say('🗺 R2')
        self.assertEqual([row['bitrix_id'] for row in self.store.session(200)['technician_choices']],[702])
        self.say('Черков')
        self.assertEqual(self.store.session(200)['technician_choices'],[])
        self.assertIn('Техник не найден',self.telegram.messages[-1][1])
        self.say(ALL_TECHNICIANS)
        self.assertEqual(len(self.store.session(200)['technician_choices']),4)
        self.choose(702)
        self.assertEqual(self.store.session(200)['queue'],'r234')

    def test_employee_departed_after_selection_blocks_all_crm_creates(self):
        self.open(); self.choose(701)
        self.api.technicians=[row for row in self.api.technicians if row['ID']!='701']
        self.say(SEND)
        self.assertEqual(self.store.session(200)['step'],'recipient')
        self.assertIn('Этот техник больше недоступен',self.telegram.messages[-1][1])
        self.assertFalse(any(method=='crm.item.add' for method,_ in self.api.calls))
        self.assertEqual(self.store.listing(200),[])

    def test_employee_department_changed_before_number_choice_is_rejected(self):
        self.open()
        self.api.technicians[1]['UF_DEPARTMENT']=[20]
        self.choose(701)
        self.assertEqual(self.store.session(200)['step'],'recipient')
        self.assertIn('Этот техник больше недоступен',self.telegram.messages[-1][1])

    def test_directory_failure_keeps_draft_and_pool_available_without_fallback(self):
        self.open()
        self.api.departments=[]
        self.say(REFRESH_TECHNICIANS)
        self.assertIn('Не удалось загрузить техников',self.telegram.messages[-1][1])
        self.say(TO_POOL); self.say(SEND)
        row=self.store.listing(200)[0]
        self.assertEqual((row['status'],row['owner'],row['technician_details']),('new',None,None))

    def test_creator_can_return_external_assignment_to_pool_but_others_cannot(self):
        self.open(); self.choose(701); self.say(SEND)
        row=self.store.listing(200)[0]
        with self.assertRaises(InboxError):
            self.store.change(500,row['id'],row['version'],'pool')
        after=self.store.change(200,row['id'],row['version'],'pool')['ticket']
        self.sync.flush()
        self.assertEqual((after['status'],after['owner'],after['technician_details']),('new',None,None))
        self.assertEqual(self.api.rows[self.sync.binding(row['id'])['crm_id']]['assignedById'],133)

    def test_later_approved_tech_registration_binds_by_bitrix_id_once_with_no_native_update(self):
        self.open(); self.choose(701); self.say(SEND)
        row=self.store.listing(200)[0]
        remote=deepcopy(self.api.rows[self.sync.binding(row['id'])['crm_id']])
        self.primary.request_registration(70100,70100,{'id':701,'name':'Арсений Черков'})
        self.sync.pull(row['id'])
        self.assertIsNone(self.store.ticket(row['id'])['owner'])
        self.primary.approve_registration(70100,100,{'id':701,'name':'Арсений Черков'})
        self.store.grant(70100,'fom_sales',100)
        self.sync.pull(row['id'])
        self.assertIsNone(self.store.ticket(row['id'])['owner'])
        self.store.grant(70100,'tech',100)
        self.sync.pull(row['id'])
        after=self.store.ticket(row['id'])
        self.assertEqual((after['owner'],after['status']),(70100,'assigned'))
        self.assertEqual(self.api.rows[self.sync.binding(row['id'])['crm_id']],remote)
        self.sync.pull(row['id'])
        self.assertEqual(self.store.ticket(row['id'])['version'],after['version'])
        events=list(self.primary.db.execute('SELECT recipient FROM fom_request_outbox WHERE recipient=?',(70100,)))
        self.assertEqual(len(events),1)
        self.assertTrue(self.store.change(70100,row['id'],after['version'],'take')['changed'])
        self.sync.flush()
        self.assertEqual(self.sync.binding(row['id'])['state'],'confirmed')

    def test_uzbek_buttons_and_search_work(self):
        with self.primary.db:
            self.primary.db.execute("INSERT INTO settings VALUES('language:200','uz') ON CONFLICT(key) DO UPDATE SET value='uz'")
        self.open(); self.say(tr(ALL_TECHNICIANS,'uz')); self.say('Хасянов'); self.say('1')
        self.assertEqual(self.store.session(200)['technician']['bitrix_id'],703)
        self.assertIn('Telegramда хабар олиш',self.telegram.messages[-1][1])

    def test_store_rechecks_directory_and_forged_nontech_id_is_rejected(self):
        self.open()
        self.api.technicians += [employee(333,'Продажи',department=20)]
        link=self.work.intake.link(200,self.work.intake.sales.lookup_fom('00081'))
        with self.assertRaises(InboxError):
            self.store.create(200,None,link,'Проверить','Нужна помощь','forged',technician_details={'bitrix_id':333})
        self.assertEqual(self.store.listing(200),[])

    def test_replay_after_technician_connects_keeps_original_request_and_never_duplicates(self):
        self.open(); self.choose(701); self.say(SEND)
        row=self.store.listing(200)[0]
        self.primary.request_registration(70100,70100,{'id':701,'name':'Арсений Черков'})
        self.primary.approve_registration(70100,100,{'id':701,'name':'Арсений Черков'})
        self.store.grant(70100,'tech',100)
        self.sync.poll()
        self.assertEqual(self.store.ticket(row['id'])['owner'],70100)
        link=self.work.intake.link(200,self.work.intake.sales.lookup_fom('00081'))
        replay=self.store.create(200,None,link,row['title'],row['description'],row['request_id'],
            support_details=row['support_details'],technician_details=row['technician_details'])
        self.assertEqual(replay['id'],row['id'])
        self.assertEqual(len(self.store.listing(200)),1)

    def test_revoked_creator_cannot_retry_creation_on_external_technician(self):
        self.open(); self.choose(701)
        self.api.reject_create=True; self.say(SEND)
        row=self.store.listing(200)[0]
        with self.primary.db:
            self.primary.db.execute("UPDATE registrations SET status='rejected' WHERE telegram_id=200")
        self.api.reject_create=False
        self.sync.retry(row['id'],100)
        self.assertEqual(self.sync.binding(row['id'])['error'],'FOM_EMPLOYEE_REVOKED')
        self.assertFalse(self.api.rows.get(self.sync.binding(row['id'])['crm_id']))

    def test_rejected_create_does_not_claim_that_technician_was_assigned_in_bitrix(self):
        self.open(); self.choose(701)
        self.api.reject_create=True; self.say(SEND)
        row=self.store.listing(200)[0]
        self.assertEqual(self.sync.binding(row['id'])['state'],'failed')
        self.assertNotIn('Техник назначен в Б24',self.telegram.calls[-1][1]['text'])
        self.assertIn('Проверка Б24 — ниже',self.telegram.messages[-1][1])

    def test_directory_cache_is_refreshed_explicitly_and_selection_checks_fresh_employee(self):
        self.open()
        self.api.technicians += [employee(899,'Новый','Техник',63)]
        self.assertNotIn(899,[row['bitrix_id'] for row in self.sync.technicians.listing()])
        self.say(REFRESH_TECHNICIANS)
        self.assertIn(899,[row['bitrix_id'] for row in self.store.session(200)['technician_choices']])

    def test_lost_request_response_keeps_exact_technician_and_does_not_create_duplicate(self):
        self.open(); self.choose(701)
        self.api.lost_create=True
        self.say(SEND)
        row=self.store.listing(200)[0]
        self.assertEqual(self.sync.binding(row['id'])['state'],'uncertain')
        self.sync.retry(row['id'],200)
        self.assertEqual(self.sync.binding(row['id'])['state'],'confirmed')
        self.assertEqual(self.api.rows[self.sync.binding(row['id'])['crm_id']]['assignedById'],701)
        self.assertEqual(sum(method=='crm.item.add' for method,_ in self.api.calls),1)

    def test_native_queue_automation_cannot_silently_replace_selected_technician(self):
        self.open(); self.choose(701)
        original=self.api.call
        def replace_owner(method,payload):
            response=original(method,payload)
            if method=='crm.item.add':
                self.api.rows[response['result']['item']['id']]['assignedById']=9999
            return response
        self.api.call=replace_owner
        self.say(SEND)
        row=self.store.listing(200)[0]
        self.assertEqual(self.sync.binding(row['id'])['state'],'uncertain')
        self.assertEqual(self.sync.binding(row['id'])['error'],'FOM_CRM_NOT_CONFIRMED')
        self.sync.retry(row['id'],200)
        self.assertEqual(sum(method=='crm.item.add' for method,_ in self.api.calls),1)
        self.assertEqual(self.sync.binding(row['id'])['state'],'uncertain')

    def test_external_assignment_manual_work_and_completion_keeps_assignee_without_telegram(self):
        self.open(); self.choose(701); self.say(SEND)
        row=self.store.listing(200)[0]
        remote=self.api.rows[self.sync.binding(row['id'])['crm_id']]
        remote.update(stageId=self.settings.stages['working']['id'],movedTime='2026-10-09T00:00:00Z')
        self.sync.pull(row['id'])
        self.assertEqual((self.store.ticket(row['id'])['status'],self.store.ticket(row['id'])['owner']),('working',None))
        remote.update(stageId=self.settings.stages['closed']['id'],movedTime='2026-10-09T01:00:00Z')
        self.sync.pull(row['id'])
        after=self.store.ticket(row['id'])
        self.assertEqual((after['status'],after['technician_details']['bitrix_id']),('closed',701))
        self.assertEqual(self.sync.binding(row['id'])['state'],'confirmed')

    def test_pagination_does_not_shift_cached_choices_and_duplicate_names_show_ids(self):
        self.open()
        self.api.technicians=[employee(900+i,'Имя',str(i)) for i in range(12)] + [
            employee(990,'Elyor','Alimov',62),employee(991,'Elyor','Alimov',62)]
        self.say(REFRESH_TECHNICIANS)
        self.assertIn('Б24 #990',self.telegram.messages[-1][1])
        snapshot=deepcopy(self.store.session(200)['technician_choices'])
        self.api.technicians.insert(0,employee(799,'А','Новый'))
        self.say('/request_next')
        self.assertEqual(self.store.session(200)['technician_choices'],snapshot)
        self.assertEqual(self.store.session(200)['recipient_page'],1)
        self.say('9')
        self.assertEqual(self.store.session(200)['technician']['bitrix_id'],snapshot[8]['bitrix_id'])


if __name__=='__main__':
    unittest.main()
