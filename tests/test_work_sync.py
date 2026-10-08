from copy import deepcopy
from datetime import datetime, timezone
import unittest

import test_work_inbox as worktests
from test_live_deals import DealApi
from datfo_crm_bot.api import RemoteError
from datfo_crm_bot.communications import InboxError
from datfo_crm_bot.work_inbox import WorkStore, elapsed, timestamp
from datfo_crm_bot.work_sync import WorkSync, SupportSettings, SOURCE


class SupportApi(DealApi):
    def __init__(self, settings):
        super().__init__()
        self.reject_create = False
        self.history = []
        self.departments = [{'ID':'17','NAME':'Отдел технической поддержки','PARENT':'15'}] + [
            {'ID':str(59+i),'NAME':'Техники регион №'+str(i),'PARENT':'17'} for i in range(1,5)]
        self.technicians = [{'ID':'134','ACTIVE':True,'USER_TYPE':'employee','NAME':'Техник',
                            'LAST_NAME':'','UF_DEPARTMENT':[60]}]
        self.catalogue[47] = [dict(row,sort=i*10,semantics='S' if key=='closed' else 'F' if key=='failed' else None)
                              for i,(key,row) in enumerate(settings.stages.items(),1)]

    def list_all(self, method, payload, *, key=None):
        if method == 'department.get':
            return deepcopy(self.departments)
        if method == 'user.get':
            rows = deepcopy(self.technicians)
            ident = payload.get('FILTER',{}).get('ID')
            return [row for row in rows if ident is None or str(row['ID'])==str(ident)]
        if method=='crm.stagehistory.list':
            return deepcopy(self.history)
        if method=='crm.deal.list':
            if self.hide_created:
                return []
            names = {'ORIGIN_ID':'originId','ORIGINATOR_ID':'originatorId'}
            return [{'ID':row['id']} for row in self.rows.values()
                    if all(str(row.get(names[name]))==str(value) for name,value in payload['filter'].items())]
        return super().list_all(method,payload,key=key)

    def call(self, method, payload):
        if method == 'crm.item.fields':
            from datfo_crm_bot.support_types import TYPE_FIELD, PROGRAM_FIELD, DESCRIPTION_FIELD
            result = super().call(method, payload)
            result['result']['fields'].update({
                TYPE_FIELD: {'type':'enumeration','isMultiple':False,'items':[
                    {'ID':'3108','VALUE':'[FA] Установка'},
                    {'ID':'5890','VALUE':'Неопознанная ошибка: нужно уточнить'},
                    {'ID':'3333','VALUE':'[FK] Не могут войти в F-Kassa'}]},
                PROGRAM_FIELD: {'type':'enumeration','isMultiple':False,'items':[
                    {'ID':'3116','VALUE':'F-Apteka'}, {'ID':'3122','VALUE':'F-Kassa'}]},
                DESCRIPTION_FIELD: {'type':'string','isMultiple':False},
            })
            return result
        if method=='crm.item.add' and self.reject_create:
            self.calls.append((method,deepcopy(payload)))
            raise RemoteError('ACCESS_DENIED')
        result = super().call(method,payload)
        if method in {'crm.item.add','crm.item.update'}:
            ident = result['result']['item']['id'] if method=='crm.item.add' else payload['id']
            self.rows[ident]['movedTime'] = f'2026-10-06T01:00:{self.clock:02}+00:00'
        return result


class SupportSyncTests(unittest.TestCase):
    setUpBase = worktests.WorkInboxTests.setUp
    tearDown = worktests.WorkInboxTests.tearDown
    ticket = worktests.WorkInboxTests.ticket
    say = worktests.WorkInboxTests.say
    callback = worktests.WorkInboxTests.callback

    def setUp(self):
        self.setUpBase()
        self.settings = SupportSettings.load()
        self.api = SupportApi(self.settings)
        self.api.rows[501] = {'id':501,'title':'Аптека тест','originatorId':'datfo-sales-telegram','originId':'verified-deal',
            'categoryId':43,'stageId':'C43:BASE','assignedById':133,'companyId':7,'updatedTime':'1'}
        self.live.crm.api = self.api
        self.work.sync = WorkSync(self.store,self.live.crm,self.settings)
        self.sync = self.work.sync

    def created(self, recipient=300):
        row = self.ticket(recipient)
        self.sync.flush()
        self.assertEqual(self.sync.binding(row['id'])['state'],'confirmed')
        return row

    def remote(self, row):
        return self.api.rows[self.sync.binding(row['id'])['crm_id']]

    def change(self, row, action, target=None):
        result = self.store.change(row['owner'],row['id'],row['version'],action,target)['ticket']
        self.sync.flush()
        self.assertEqual(self.sync.binding(row['id'])['state'],'confirmed')
        return result

    def test_all_stages_real_owner_links_and_wait_resume_keep_first_timestamp(self):
        source = deepcopy(self.api.rows[501])
        row = self.created()
        remote = self.remote(row)
        self.assertEqual((remote['categoryId'],remote['stageId'],remote['assignedById']),(47,'C47:NEW',134))
        self.assertEqual(remote['companyId'],7)
        self.assertIn('Исходная сделка #501',remote['comments'])
        row = self.change(row,'r1')
        self.assertEqual(remote['stageId'],'C47:UC_V2TW36')
        row = self.change(row,'r234')
        self.assertEqual(remote['stageId'],'C47:PREPARATION')
        row = self.change(row,'take')
        first = row['work_at']
        self.assertIsNotNone(first)
        self.assertEqual(remote['stageId'],'C47:PREPAYMENT_INVOIC')
        row = self.change(row,'wait')
        self.assertEqual(remote['stageId'],'C47:EXECUTING')
        row = self.change(row,'resume')
        row = self.change(row,'assign',400)
        row = self.change(row,'take')
        self.assertEqual((row['work_at'],remote['assignedById']),(first,135))
        row = self.change(row,'close')
        self.assertEqual((remote['stageId'],row['status']),('C47:WON','closed'))
        self.assertIsNotNone(row['resolved_at'])
        self.assertIn('От создания до решения:',remote['comments'])
        self.assertEqual(self.api.rows[501],source)
        self.work.card(200,row)
        self.assertIn('Б24 техник ёрдам заявкаси #',self.telegram.calls[-1][1]['text'])
        self.assertIn('Яратилишдан якунгача:',self.telegram.calls[-1][1]['text'])
        self.assertEqual(elapsed(row,datetime(2030,1,1,tzinfo=timezone.utc)),elapsed(row))

    def test_creation_lost_response_is_found_without_second_add(self):
        self.api.lost_create = True
        row = self.ticket(300)
        self.sync.flush()
        self.assertEqual(self.sync.binding(row['id'])['state'],'uncertain')
        with self.assertRaises(InboxError):
            self.store.change(300,row['id'],0,'take')
        self.sync.retry(row['id'],300)
        self.assertEqual(self.sync.binding(row['id'])['state'],'confirmed')
        self.assertEqual(sum(method=='crm.item.add' for method,p in self.api.calls),1)

    def test_uncertain_create_not_found_never_resubmits_even_on_explicit_check(self):
        self.api.lost_create = True
        row = self.ticket(300)
        self.sync.flush()
        self.api.hide_created = True
        self.sync.retry(row['id'],300)
        self.sync.flush()
        self.assertEqual(self.sync.binding(row['id'])['state'],'uncertain')
        self.assertEqual(sum(method=='crm.item.add' for method,p in self.api.calls),1)

    def test_update_lost_response_and_restart_recovery_only_reads(self):
        row = self.created()
        self.api.lost_update = True
        row = self.store.change(300,row['id'],0,'take')['ticket']
        self.sync.flush()
        self.assertEqual(self.sync.binding(row['id'])['state'],'uncertain')
        self.work.sync = WorkSync(self.store,self.live.crm,self.settings)
        self.work.sync.retry(row['id'],300)
        self.assertEqual(self.work.sync.binding(row['id'])['state'],'confirmed')
        self.assertEqual(sum(method=='crm.item.update' for method,p in self.api.calls),1)
        self.assertEqual(self.store.ticket(row['id'])['work_at'],row['work_at'])

    def test_manual_stage_and_responsible_are_adopted_and_old_button_rejected(self):
        row = self.created()
        remote = self.remote(row)
        remote.update(stageId='C47:PREPAYMENT_INVOIC',assignedById=135,movedTime='2026-10-06T02:13:47+00:00')
        self.sync.pull(row['id'])
        after = self.store.ticket(row['id'])
        self.assertEqual((after['owner'],after['status']),(400,'working'))
        self.assertEqual(timestamp(after['work_at']),timestamp(remote['movedTime']))
        with self.assertRaises(InboxError):
            self.store.change(400,row['id'],0,'close')
        remote.update(stageId='C47:WON',movedTime='2026-10-06T05:32:17+00:00')
        self.sync.pull(row['id'])
        after = self.store.ticket(row['id'])
        self.assertEqual(after['resolved_at'],'2026-10-06T05:32:17+00:00')
        self.assertFalse(any(method=='crm.item.update' for method,p in self.api.calls))

    def test_manual_change_during_pending_write_needs_explicit_reconcile(self):
        row = self.created()
        pending = self.store.change(300,row['id'],0,'take')['ticket']
        remote = self.remote(row)
        remote.update(stageId='C47:EXECUTING',assignedById=135,movedTime='2026-10-06T02:30:00+00:00')
        self.sync.flush()
        self.assertEqual(self.sync.binding(row['id'])['state'],'conflict')
        with self.assertRaises(InboxError):
            self.sync.reconcile(row['id'],200)
        self.sync.reconcile(row['id'],100)
        after = self.store.ticket(row['id'])
        self.assertEqual((after['status'],after['owner']),('waiting',400))
        self.assertEqual(self.sync.binding(row['id'])['state'],'confirmed')
        after = self.change(after,'resume')
        self.assertEqual(after['status'],'working')
        self.assertEqual(sum(method=='crm.item.update' for method,p in self.api.calls),1)

    def test_source_tamper_or_unknown_owner_never_gets_overwritten(self):
        row = self.created()
        remote = self.remote(row)
        remote['originId'] = 'someone-else'
        self.sync.pull(row['id'])
        self.assertEqual(self.sync.binding(row['id'])['error'],'FOM_CRM_SOURCE_CHANGED')
        with self.assertRaises(InboxError):
            self.store.change(300,row['id'],0,'take')
        self.assertFalse(any(method=='crm.item.update' for method,p in self.api.calls))

    def test_rejected_create_retry_is_explicit_and_no_fake_success(self):
        self.api.reject_create = True
        row = self.ticket(300)
        self.sync.flush()
        self.sync.flush()
        self.assertEqual(sum(method=='crm.item.add' for method,p in self.api.calls),1)
        self.assertIsNone(self.sync.binding(row['id'])['crm_id'])
        self.assertIn('тасдиқланмади',self.sync.text(row))
        self.api.reject_create = False
        self.sync.retry(row['id'],300)
        self.assertEqual(self.sync.binding(row['id'])['state'],'confirmed')

    def test_time_uses_creation_including_transfers_wait_and_timezone(self):
        row = self.created()
        with self.store.db:
            self.store.db.execute("UPDATE fom_requests SET created_at='2026-10-05 20:00:00',work_at='2026-10-06 01:00:00',resolved_at='2026-10-06 03:30:45',status='closed' WHERE id=?",(row['id'],))
        row = self.store.ticket(row['id'])
        self.assertEqual(elapsed(row),27045)
        block = self.sync.time_block(row)
        self.assertIn('06.10.2026 01:00:00',block)
        self.assertIn('07:30:45',block)
        self.work.card(200,row)
        self.assertIn('07:30:45',self.telegram.calls[-1][1]['text'])
        self.assertAlmostEqual(self.store.stats(row['owner'])['resolution_seconds'],27045,places=2)
        if row['owner']!=200:
            self.assertIsNone(self.store.stats(200)['resolution_seconds'])

    def test_human_notes_preserved_and_stage_catalogue_change_blocks_writes(self):
        row = self.created()
        remote = self.remote(row)
        remote['comments'] += '\nКомментарий сотрудника'
        row = self.change(row,'take')
        self.assertIn('Комментарий сотрудника',remote['comments'])
        self.assertEqual(remote['comments'].count('--- FOM BOT TIME BEGIN ---'),1)
        self.api.catalogue[47][3]['title'] = 'Переименованная стадия'
        row = self.store.change(300,row['id'],row['version'],'wait')['ticket']
        self.sync.flush()
        self.assertEqual(self.sync.binding(row['id'])['error'],'FOM_STAGE_CHANGED')
        self.assertEqual(remote['stageId'],'C47:PREPAYMENT_INVOIC')

    def test_region_preview_then_confirm_uses_selected_region(self):
        for value in ['/request 501','3','Нужна помощь','Проверить проблему','🗺 R1 ҳудуди']:
            self.say(value)
        self.assertEqual(self.store.session(200)['queue'],'r1')
        self.say('✅ Сўровни юбориш')
        row = self.store.listing(200)[0]
        self.assertEqual(self.remote(row)['stageId'],'C47:UC_V2TW36')

    def test_migration_restores_previous_event_times_and_does_not_enroll_history(self):
        row = self.created()
        row = self.change(row,'take')
        row = self.change(row,'close')
        first,done = row['work_at'],row['resolved_at']
        with self.store.db:
            self.store.db.execute('UPDATE fom_requests SET work_at=NULL,resolved_at=NULL WHERE id=?',(row['id'],))
        reopened = WorkStore(self.primary,(100,))
        self.assertEqual((reopened.ticket(row['id'])['work_at'],reopened.ticket(row['id'])['resolved_at']),(first,done))

    def test_native_region_queue_holder_keeps_first_recipient_and_claim_sets_actual_executor(self):
        row = self.created()
        remote = self.remote(row)
        remote.update(stageId='C47:PREPARATION',assignedById=988,movedTime='2026-10-06T02:30:00+00:00')
        self.sync.pull(row['id'])
        row = self.store.ticket(row['id'])
        self.assertEqual((row['owner'],row['initial_recipient'],row['queue']),(300,300,'r234'))
        self.assertEqual(self.sync.binding(row['id'])['bitrix_owner'],988)
        self.assertIn('ID 988',self.sync.text(row))
        row = self.change(row,'take')
        self.assertEqual((remote['assignedById'],remote['stageId']),(134,'C47:PREPAYMENT_INVOIC'))

    def test_working_owner_without_connected_identity_blocks_claim(self):
        row = self.created()
        remote = self.remote(row)
        remote.update(stageId='C47:PREPAYMENT_INVOIC',assignedById=988,movedTime='2026-10-06T02:30:00+00:00')
        self.sync.pull(row['id'])
        self.assertEqual(self.sync.binding(row['id'])['error'],'FOM_CRM_OWNER_NOT_CONNECTED')
        with self.assertRaises(InboxError):
            self.store.change(300,row['id'],row['version'],'take')
        self.assertFalse(any(method=='crm.item.update' for method,p in self.api.calls))

    def test_periodic_read_delivers_manual_completion_once_without_writes(self):
        row = self.created()
        remote = self.remote(row)
        remote.update(stageId='C47:WON',movedTime='2026-10-06T02:30:00+00:00')
        self.work.flush()
        self.assertEqual(self.store.ticket(row['id'])['status'],'closed')
        sent = sum(method=='sendMessage' for method,p in self.telegram.calls)
        self.work.flush()
        self.assertEqual(sum(method=='sendMessage' for method,p in self.telegram.calls),sent)
        self.assertFalse(any(method=='crm.item.update' for method,p in self.api.calls))

    def test_native_automation_on_create_can_change_region_holder_without_duplicate(self):
        row = self.ticket(300)
        self.api.lost_create = True
        self.sync.flush()
        remote = next(record for record in self.api.rows.values() if record.get('originatorId')==SOURCE)
        remote.update(stageId='C47:PREPARATION',assignedById=988,movedTime='2026-10-06T02:30:00+00:00')
        self.sync.retry(row['id'],300)
        self.assertEqual(self.sync.binding(row['id'])['state'],'confirmed')
        self.assertEqual(self.store.ticket(row['id'])['queue'],'r234')
        self.assertEqual(sum(method=='crm.item.add' for method,p in self.api.calls),1)

    def test_manual_first_work_between_polls_uses_native_history_instead_of_close_time(self):
        row = self.created()
        remote = self.remote(row)
        with self.store.db:
            self.store.db.execute("UPDATE fom_requests SET created_at='2026-10-06 00:00:00' WHERE id=?",(row['id'],))
        self.api.history = [{'OWNER_ID':remote['id'],'CATEGORY_ID':47,'STAGE_ID':'C47:PREPAYMENT_INVOIC',
            'CREATED_TIME':'2026-10-06T01:22:13+00:00'},
            {'OWNER_ID':remote['id'],'CATEGORY_ID':47,'STAGE_ID':'C47:PREPAYMENT_INVOIC','CREATED_TIME':'2026-10-06T01:45:00+00:00'}]
        remote.update(stageId='C47:WON',movedTime='2026-10-06T02:30:00+00:00')
        self.sync.pull(row['id'])
        after = self.store.ticket(row['id'])
        self.assertEqual(after['work_at'],'2026-10-06T01:22:13+00:00')
        self.assertEqual(after['resolved_at'],'2026-10-06T02:30:00+00:00')
        self.assertEqual(elapsed(after),9000)

    def test_archived_request_hidden_from_every_user_and_no_crm_poll_or_write(self):
        row = self.created()
        with self.store.db:
            self.store.db.execute('UPDATE fom_requests SET archived=1 WHERE id=?',(row['id'],))
        row = self.store.ticket(row['id'])
        self.assertEqual(self.store.listing(100),[])
        self.assertEqual(self.store.listing(200,'sent'),[])
        self.assertFalse(self.store.visible(row,100))
        with self.assertRaises(InboxError):
            self.sync.retry(row['id'],100)
        with self.assertRaises(InboxError):
            self.work.card(100,row)
        self.api.calls.clear()
        self.work.flush()
        self.assertEqual(self.api.calls,[])
        self.assertEqual(len(self.store.history(row['id'])),1)

    def test_clean_start_and_help_do_not_offer_qa_or_admin_flows(self):
        from datfo_crm_bot.app import COMMANDS
        from datfo_crm_bot.guidance import TOPICS
        greeting,keyboard = self.registration.route(100,100,'/start','/start')
        self.assertNotIn('Telegram ID',greeting)
        labels=' '.join(label['text'] if isinstance(label,dict) else label for line in keyboard for label in line)
        self.assertNotIn('🧪',labels)
        self.assertNotIn('/inbox_team',labels)
        self.assertNotIn('Заявки менеджеров',labels)
        self.assertNotIn('/b24_test',TOPICS['home'])
        self.assertNotIn('/inbox_test',TOPICS['home'])
        self.assertNotIn('test',{row['command'] for row in COMMANDS})
        admin,keyboard=self.registration.route(100,100,'/admin','/admin')
        self.assertIn(['/inbox_team'],keyboard)
        _,keyboard=self.registration.route(200,200,'/admin','/admin')
        self.assertNotIn(['/inbox_team'],keyboard)


if __name__=='__main__':
    unittest.main()
