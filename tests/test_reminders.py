from copy import deepcopy
from datetime import datetime, timedelta
import unittest

import test_live_deals as fixture
from datfo_crm_bot.api import RemoteError
from datfo_crm_bot.i18n import language_context
from datfo_crm_bot.reminders import Reminders


class ReminderApi(fixture.DealApi):
    def __init__(self):
        super().__init__()
        self.notes = {}
        self.reject_complete = self.lose_complete = self.lose_comment = False
        self.hide_comments = False
        self.before_get = None

    def call(self, method, payload):
        if method == 'crm.activity.get':
            self.calls.append((method, deepcopy(payload)))
            if self.before_get:
                self.before_get(self.activities[payload['id']])
            activity = self.activities[payload['id']]
            return {'result': {'ID': payload['id'], 'OWNER_TYPE_ID': 2, 'OWNER_ID': activity['ownerId'],
                'PROVIDER_ID': 'CRM_TODO', 'RESPONSIBLE_ID': activity['responsibleId'],
                'SUBJECT': activity['title'], 'DESCRIPTION': activity['description'],
                'ORIGINATOR_ID':activity.get('ORIGINATOR_ID'), 'ORIGIN_ID':activity.get('ORIGIN_ID'),
                'DEADLINE': activity['deadline'], 'COMPLETED': activity.get('completed','N')}}
        if method == 'crm.activity.update' and 'COMPLETED' in payload['fields']:
            self.calls.append((method, deepcopy(payload)))
            if self.reject_complete:
                raise RemoteError('ACCESS_DENIED')
            self.activities[payload['id']]['completed'] = payload['fields']['COMPLETED']
            if self.lose_complete:
                self.lose_complete = False
                raise RemoteError('CONNECTION_ERROR', uncertain=True)
            return {'result': True}
        if method == 'crm.timeline.comment.add':
            self.calls.append((method, deepcopy(payload)))
            ident = 1000 + len(self.notes)
            self.notes[ident] = {'ID': ident, **payload['fields']}
            if self.lose_comment:
                self.lose_comment = False
                raise RemoteError('CONNECTION_ERROR', uncertain=True)
            return {'result': ident}
        return super().call(method, payload)

    def list_all(self, method, payload, **kwargs):
        if method == 'crm.timeline.comment.list':
            if self.hide_comments:
                return []
            return [deepcopy(note) for note in self.notes.values() if note['ENTITY_ID']==payload['filter']['ENTITY_ID']]
        return super().list_all(method, payload, **kwargs)


class ReminderTests(unittest.TestCase):
    tearDown = fixture.LiveDealTests.tearDown
    say = fixture.LiveDealTests.say
    message = fixture.LiveDealTests.message
    form = fixture.LiveDealTests.form
    callback = fixture.LiveDealTests.callback

    def setUp(self):
        fixture.LiveDealTests.setUp(self)
        self.api = ReminderApi()
        self.live.crm.api = self.api
        self.state = self.form()
        self.say('Подтвердить')
        self.due = datetime.fromisoformat(self.state['deadline'])
        self.now = self.due - timedelta(minutes=30)
        self.reminders = Reminders(self.registration, self.live, self.telegram, clock=lambda: self.now)
        self.reminders.discover()
        self.telegram.calls.clear()
        self.telegram.messages.clear()
        self.api.calls.clear()

    def inline(self):
        return [payload for method,payload in self.telegram.calls if method=='sendMessage']

    def press(self, action, user=100):
        with language_context('ru'):
            return self.reminders.handle(self.callback('rm:'+action, user))

    def comment(self, text, user=100):
        update = self.message(text, user)
        with language_context('ru'):
            self.reminders.handle(update)
        return update

    def test_advance_and_due_are_each_delivered_once_across_restart(self):
        self.reminders.flush()
        self.assertEqual(self.inline(), [])
        self.now = self.due - timedelta(minutes=15)
        self.reminders.flush()
        self.now += timedelta(minutes=1)
        self.reminders.flush()
        self.assertEqual(len(self.inline()), 1)
        self.assertEqual(self.inline()[0]['chat_id'], 100)
        self.assertIn('rm:complete:901', str(self.inline()[0]['reply_markup']))
        self.now = self.due
        Reminders(self.registration, self.live, self.telegram, clock=lambda:self.now).flush()
        Reminders(self.registration, self.live, self.telegram, clock=lambda:self.now).flush()
        self.assertEqual(len(self.inline()), 2)
        self.assertEqual(self.db_notice_statuses(), ['sent','sent'])

    def db_notice_statuses(self):
        return [row[0] for row in self.store.db.execute('SELECT status FROM crm_reminder_notices ORDER BY slot')]

    def test_completed_in_bitrix_has_no_notification(self):
        self.api.activities[901]['completed']='Y'
        self.now=self.due
        self.reminders.flush()
        self.assertEqual(self.inline(), [])

    def test_live_responsible_receives_notification_and_old_owner_cannot_complete(self):
        self.api.activities[901]['responsibleId']=133
        self.now=self.due
        self.reminders.flush()
        self.assertEqual(self.inline()[0]['chat_id'], 200)
        self.press('complete:901', user=100)
        self.assertEqual(self.api.activities[901].get('completed','N'), 'N')
        self.assertIn('Ответственный', self.telegram.messages[-1][1])

    def test_unregistered_or_revoked_recipient_is_not_notified(self):
        self.store.deny_registration(100, 100, 'revoked')
        self.now=self.due
        self.reminders.flush()
        self.assertEqual(self.inline(), [])

    def test_moved_deadline_is_used_for_new_notifications(self):
        self.now=self.due
        self.reminders.flush()
        self.api.activities[901]['deadline']=(self.due+timedelta(days=1)).isoformat()
        self.now += timedelta(minutes=1)
        self.reminders.flush()
        self.assertEqual(len(self.inline()), 1)
        self.now=self.due+timedelta(days=1)
        self.reminders.flush()
        self.assertEqual(len(self.inline()), 2)

    def test_completion_between_poll_and_send_suppresses_notification(self):
        reads=[]
        def close_on_second_read(activity):
            reads.append(1)
            if len(reads)==2:
                activity['completed']='Y'
        self.api.before_get=close_on_second_read
        self.now=self.due
        self.reminders.flush()
        self.assertEqual(self.inline(), [])

    def test_deadline_changed_between_poll_and_send_suppresses_old_notice(self):
        reads=[]
        def move_on_second_read(activity):
            reads.append(1)
            if len(reads)==2:
                activity['deadline']=(self.due+timedelta(days=1)).isoformat()
        self.api.before_get=move_on_second_read
        self.now=self.due
        self.reminders.flush()
        self.assertEqual(self.inline(), [])

    def test_foreign_activity_binding_or_group_chat_cannot_mutate(self):
        self.api.activities[901]['ownerId']=999
        self.press('complete:901')
        self.assertFalse(any(method=='crm.activity.update' for method,_ in self.api.calls))
        update=self.callback('rm:comment:901')
        update['callback_query']['message']['chat']['type']='group'
        self.assertFalse(self.reminders.handle(update))

    def test_legacy_marker_reminder_still_works_without_native_origin_fields(self):
        activity=self.api.activities[901]
        activity.pop('ORIGINATOR_ID')
        activity.pop('ORIGIN_ID')
        activity['description']+='\nЗапрос бота: '+self.state['request_id']+'-next-step'
        self.now=self.due
        self.reminders.flush()
        self.assertEqual(len(self.inline()),1)

    def test_changed_native_origin_is_rejected_even_with_matching_legacy_marker(self):
        activity=self.api.activities[901]
        activity['ORIGIN_ID']='another-request-next-step'
        activity['description']+='\nЗапрос бота: '+self.state['request_id']+'-next-step'
        self.now=self.due
        self.reminders.flush()
        self.press('complete:901')
        self.assertFalse(any(method=='crm.activity.update' for method,_ in self.api.calls))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM crm_reminder_notices').fetchone()[0],0)

    def test_user_can_edit_reminder_description_without_losing_bot_binding(self):
        self.api.activities[901]['description']='Новый комментарий менеджера'
        self.now=self.due
        self.reminders.flush()
        self.assertEqual(len(self.inline()),1)
        self.press('complete:901')
        self.assertEqual(self.api.activities[901]['completed'],'Y')

    def test_unknown_telegram_response_is_not_automatically_sent_again(self):
        self.telegram.fail_inline=True
        self.now=self.due
        self.reminders.flush()
        self.now += timedelta(minutes=1)
        Reminders(self.registration,self.live,self.telegram,clock=lambda:self.now).flush()
        self.assertEqual(len(self.inline()),1)
        self.assertEqual(self.db_notice_statuses(),['uncertain'])

    def test_duplicate_completion_changes_only_completed_field_once(self):
        update=self.callback('rm:complete:901')
        self.reminders.handle(update)
        self.reminders.handle(update)
        writes=[payload for method,payload in self.api.calls if method=='crm.activity.update']
        self.assertEqual(writes,[{'id':901,'fields':{'COMPLETED':'Y'}}])
        self.assertEqual(self.api.activities[901]['completed'],'Y')
        self.now=self.due
        self.reminders.flush()
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM crm_reminder_notices').fetchone()[0],0)

    def test_completion_lost_response_recovers_without_second_update(self):
        self.api.lose_complete=True
        self.press('complete:901')
        self.press('complete:901')
        self.assertEqual(sum(method=='crm.activity.update' for method,_ in self.api.calls),1)
        self.assertEqual(self.store.db.execute("SELECT status FROM crm_reminder_actions WHERE kind='complete'").fetchone()[0],'succeeded')

    def test_denied_completion_is_not_marked_done_and_can_be_explicitly_retried(self):
        self.api.reject_complete=True
        self.press('complete:901')
        self.assertEqual(self.api.activities[901].get('completed','N'),'N')
        self.assertIn('не разрешил',self.inline()[-1]['text'])
        self.api.reject_complete=False
        self.press('complete:901')
        self.assertEqual(self.api.activities[901]['completed'],'Y')

    def test_comments_preserve_active_form_and_are_linked_to_actual_activity(self):
        original={'request_id':'draft','step':'title','workflow':'okb'}
        self.store.set_session(100,original)
        self.press('comment:901')
        update=self.comment('Позвонил, договорились о презентации')
        self.reminders.handle(update)
        self.assertEqual(self.store.session(100),original)
        self.assertEqual(len(self.api.notes),1)
        note=next(iter(self.api.notes.values()))
        self.assertEqual((note['ENTITY_ID'],note['ENTITY_TYPE']),(501,'deal'))
        self.assertIn('Напоминание #901',note['COMMENT'])
        self.assertIn('Позвонил, договорились о презентации',note['COMMENT'])
        self.assertIsNone(self.api.activities[901].get('completed'))

    def test_lost_comment_response_recovers_exact_comment_without_duplicate(self):
        self.api.lose_comment=True
        self.press('comment:901')
        self.comment('Клиент попросил перезвонить')
        token=self.store.db.execute("SELECT token FROM crm_reminder_actions WHERE kind='comment'").fetchone()[0]
        self.press('retry:'+token)
        self.press('retry:'+token)
        self.assertEqual(len(self.api.notes),1)
        self.assertEqual(sum(method=='crm.timeline.comment.add' for method,_ in self.api.calls),1)

    def test_unconfirmed_comment_not_found_never_reposts(self):
        self.api.lose_comment=True
        self.press('comment:901')
        self.comment('Результат звонка')
        self.api.hide_comments=True
        token=self.store.db.execute("SELECT token FROM crm_reminder_actions WHERE kind='comment'").fetchone()[0]
        self.press('retry:'+token)
        self.assertEqual(sum(method=='crm.timeline.comment.add' for method,_ in self.api.calls),1)

    def test_comment_cancel_keeps_primary_draft_and_replay_is_consumed(self):
        original={'request_id':'draft','step':'title'}
        self.store.set_session(100,original)
        self.press('comment:901')
        update=self.comment('/cancel')
        self.assertTrue(self.reminders.handle(update))
        self.assertEqual(self.store.session(100),original)
        self.assertEqual(self.api.notes,{})

    def test_menu_navigation_exits_comment_without_saving_button_label(self):
        from datfo_crm_bot.live_deals import MY_CRM
        self.press('comment:901')
        self.assertFalse(self.reminders.handle(self.message(MY_CRM)))
        self.assertEqual(self.api.notes,{})

    def test_source_or_activity_binding_changes_block_all_writes(self):
        self.api.rows[501]['originatorId']='other-app'
        self.press('complete:901')
        self.press('comment:901')
        self.assertFalse(any(method in {'crm.activity.update','crm.timeline.comment.add'} for method,_ in self.api.calls))

    def test_recipient_language_is_used_for_proactive_notification(self):
        with self.store.db:
            self.store.db.execute('INSERT INTO settings VALUES(?,?)',('language:100','uz'))
        self.now=self.due
        with language_context('ru'):
            self.reminders.flush()
        self.assertIn('Эслатма',self.inline()[0]['text'])
        self.assertIn('Бажарилди',str(self.inline()[0]['reply_markup']))

    def test_listing_reads_open_reminders_and_does_not_write_to_bitrix(self):
        self.reminders.handle(self.message('/reminders'))
        self.assertEqual(len(self.inline()),1)
        self.assertIn('Напоминание',self.inline()[0]['text'])
        self.assertFalse(any(method=='crm.activity.update' for method,_ in self.api.calls))

    def test_reminders_from_personal_hub_show_only_current_users_items(self):
        from datfo_crm_bot.crm_list import hub
        hub(self.live, 100)
        buttons = self.inline()[-1]['reply_markup']['inline_keyboard']
        self.assertTrue(any(button['callback_data'] == 'rm:list' for row in buttons for button in row))
        self.telegram.calls.clear()
        self.assertTrue(self.press('list'))
        self.assertIn('Напоминание', self.inline()[-1]['text'])
        self.assertFalse(any(method == 'crm.activity.update' for method, _ in self.api.calls))
        self.telegram.calls.clear()
        self.telegram.messages.clear()
        self.assertTrue(self.press('list', 200))
        self.assertEqual(self.inline(), [])
        self.assertIn('нет', self.telegram.messages[-1][1].lower())


if __name__=='__main__':
    unittest.main()
