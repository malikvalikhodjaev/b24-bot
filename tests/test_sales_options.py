from copy import deepcopy
from datetime import timedelta
import json
import unittest

import test_miniapp_workflows as forms
import test_reminders as reminders
from datfo_crm_bot.api import RemoteError
from datfo_crm_bot.input_forms import reminder
from datfo_crm_bot.i18n import language_context
from datfo_crm_bot.reminders import Reminders
from datfo_crm_bot.sales_intake import CONFIRM_INACTIVE
from datfo_crm_bot.sales_options import DISCUSSION_FIELD
from datfo_crm_bot.service import CONFIRM


class SalesOptionsTests(unittest.TestCase):
    setUp, tearDown = forms.DealFormTests.setUp, forms.DealFormTests.tearDown
    message, say = forms.DealFormTests.message, forms.DealFormTests.say
    token, send_form = forms.DealFormTests.token, forms.DealFormTests.send_form

    def open(self, programs=None):
        token = self.token()
        self.send_form({'token': token, 'mode': 'deal', 'pharmacy_kind': 'existing', 'fom_id': '00081',
                        'discussion_programs': ['5246', '5247'] if programs is None else programs})
        return token

    def test_multiple_programs_are_native_ids_in_same_linked_deal(self):
        self.open()
        self.assertIn('F-Kassa', self.telegram.messages[-1][1])
        self.say(CONFIRM)
        fields = [payload['fields'] for method, payload in self.api.calls if method == 'crm.item.add'][-1]
        self.assertEqual(fields[DISCUSSION_FIELD], ['5246', '5247'])
        self.assertEqual(fields['parentId1034'], 81)

    def test_empty_or_unknown_programs_do_not_prepare_or_create(self):
        for values in ([], ['not-a-program'], '5246', [True]):
            self.open(values)
            self.assertFalse(self.api.rows)
            self.assertFalse(self.store.db.execute('SELECT 1 FROM operations').fetchone())
            self.store.set_session(100, None)

    def test_duplicate_programs_are_deduplicated(self):
        self.open(['5246', '5246'])
        self.assertEqual(len(self.store.session(100)['discussion_programs']), 1)

    def test_each_inactive_status_requires_yes_and_does_not_change_pharmacy(self):
        for status in ('4794', '4795'):
            self.api.pharmacies[81]['UF_CRM_8_1772692252313'] = status
            before = deepcopy(self.api.pharmacies[81])
            token = self.open()
            self.assertEqual(self.store.session(100)['step'], 'sales_status_confirm')
            self.assertFalse(self.store.operation(token))
            self.say(CONFIRM_INACTIVE)
            self.assertEqual(self.store.session(100)['step'], 'confirm')
            self.say(CONFIRM)
            self.assertEqual(self.store.operation(token)['status'], 'succeeded')
            self.assertEqual(self.api.pharmacies[81], before)
            self.store.set_session(100, None)

    def test_status_changed_after_preview_is_rechecked_before_native_write(self):
        self.api.pharmacies[81]['UF_CRM_8_1772692252313'] = '4793'
        token = self.open()
        self.api.pharmacies[81]['UF_CRM_8_1772692252313'] = '4795'
        self.say(CONFIRM)
        self.assertFalse(self.api.rows)
        self.assertEqual(self.store.session(100)['step'], 'sales_status_confirm')
        self.say(CONFIRM_INACTIVE)
        self.say(CONFIRM)
        self.assertEqual(self.store.operation(token)['status'], 'succeeded')

    def test_preset_is_canonical_and_other_retains_user_text(self):
        now = self.live.bot.clock()
        data = {'reminder': True, 'reminder_action': 'call', 'next_step': 'injected preset text',
                'deadline': (now + timedelta(days=1)).replace(tzinfo=None).isoformat()}
        with language_context('ru'):
            value = reminder(data, self.live.bot.config.timezone, now)
            self.assertEqual(value['next_step'], 'Позвонить')
            data.update(reminder_action='other', next_step='Уточнить поставку')
            self.assertEqual(reminder(data, self.live.bot.config.timezone, now)['next_step'], 'Уточнить поставку')
            data['next_step'] = ' '
            with self.assertRaises(ValueError):
                reminder(data, self.live.bot.config.timezone, now)


class SnoozeApi(reminders.ReminderApi):
    def call(self, method, payload):
        if method == 'crm.activity.todo.updateDeadline':
            self.calls.append((method, deepcopy(payload)))
            if getattr(self, 'reject_snooze', False):
                raise RemoteError('ACCESS_DENIED')
            if not getattr(self, 'ignore_snooze', False):
                self.activities[payload['id']]['deadline'] = payload['value']
            if getattr(self, 'lose_snooze', False):
                self.lose_snooze = False
                raise RemoteError('CONNECTION_ERROR', uncertain=True)
            return {'result': {'id': payload['id']}}
        return super().call(method, payload)


class SnoozeTests(unittest.TestCase):
    setUp = reminders.ReminderTests.setUp
    tearDown = reminders.ReminderTests.tearDown
    say, message, form, callback = reminders.ReminderTests.say, reminders.ReminderTests.message, reminders.ReminderTests.form, reminders.ReminderTests.callback
    press, inline = reminders.ReminderTests.press, reminders.ReminderTests.inline

    def prepare(self):
        if not isinstance(self.api, SnoozeApi):
            api = SnoozeApi()
            api.__dict__.update(self.api.__dict__)
            self.api = api
            self.live.crm.api = api
            self.reminders.api = api
        self.press('snooze:901')
        row = self.store.db.execute('SELECT * FROM crm_reminder_snooze_inputs WHERE user_id=100').fetchone()
        token = row['token']
        chosen = (self.now + timedelta(days=1)).date()
        self.press(f'sday:{token}:{chosen:%Y%m%d}')
        return token

    def writes(self):
        return [payload for method, payload in self.api.calls if method == 'crm.activity.todo.updateDeadline']

    def test_slot_postpones_immediately_and_duplicate_press_is_read_only(self):
        token = self.prepare()
        original = deepcopy(self.api.activities[901])
        self.press(f'stime:{token}:1000')
        self.press(f'stime:{token}:1000')
        self.assertNotIn('sconfirm:',str(self.inline()[-1]['reply_markup']))
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.writes()[0]['ownerId'], original['ownerId'])
        self.assertEqual(self.api.activities[901]['description'], original['description'])
        self.assertNotEqual(self.api.activities[901]['deadline'], original['deadline'])
        self.assertEqual(self.api.activities[901].get('completed', 'N'), 'N')
        self.assertEqual(self.reminders.action(token, 100)['status'], 'succeeded')

    def test_postpone_at_half_hour_keeps_minutes_in_native_deadline(self):
        token = self.prepare()
        self.press(f'stime:{token}:1030')
        self.assertIn('10:30', self.inline()[-1]['text'])
        self.press(f'stime:{token}:1030')
        self.press('sconfirm:' + token)  # Old confirmation remains read-only after success.
        self.assertEqual(len(self.writes()), 1)
        self.assertTrue(self.writes()[0]['value'].endswith('T10:30:00+05:00'))
        self.assertTrue(self.api.activities[901]['deadline'].endswith('T10:30:00+05:00'))

    def test_today_allows_upcoming_half_hour_and_hides_past_times(self):
        token = self.prepare()
        self.now = self.now.replace(hour=10, minute=10, second=0, microsecond=0)
        self.press(f'sday:{token}:{self.now:%Y%m%d}')
        buttons = [button for row in self.inline()[-1]['reply_markup']['inline_keyboard'] for button in row]
        values = {button['text'] for button in buttons}
        self.assertIn('10:30', values)
        self.assertIn('23:30', values)
        self.assertNotIn('10:00', values)
        self.press(f'stime:{token}:1000')
        self.assertFalse(self.writes())

    def test_unsupported_minutes_and_invalid_hours_do_not_write(self):
        token = self.prepare()
        original = self.store.db.execute('SELECT value FROM crm_reminder_snooze_inputs WHERE token=?',(token,)).fetchone()[0]
        for value in ('1015', '1060', '2400', '2430'):
            self.press(f'stime:{token}:{value}')
            self.assertEqual(self.store.db.execute('SELECT value FROM crm_reminder_snooze_inputs WHERE token=?',(token,)).fetchone()[0], original)
        self.assertFalse(self.writes())

    def test_lost_response_is_found_after_restart_without_second_update(self):
        token = self.prepare()
        self.api.lose_snooze = True
        self.press(f'stime:{token}:1000')
        self.assertEqual(self.reminders.action(token, 100)['status'], 'uncertain')
        self.reminders = Reminders(self.registration, self.live, self.telegram, clock=lambda: self.now)
        self.press('sconfirm:' + token)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.reminders.action(token, 100)['status'], 'succeeded')

    def test_lost_unconfirmed_update_is_not_replayed(self):
        token = self.prepare()
        self.api.lose_snooze = self.api.ignore_snooze = True
        self.press(f'stime:{token}:1000')
        self.press(f'stime:{token}:1000')
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.reminders.action(token, 100)['status'], 'uncertain')

    def test_changed_deadline_or_completed_activity_is_never_overwritten(self):
        token = self.prepare()
        self.api.activities[901]['deadline'] = (self.due + timedelta(days=2)).isoformat()
        self.press(f'stime:{token}:1000')
        self.assertFalse(self.writes())
        self.api.activities[901]['completed'] = 'Y'
        self.press('sconfirm:' + token)
        self.assertFalse(self.writes())

    def test_wrong_user_and_changed_responsible_cannot_postpone(self):
        token = self.prepare()
        self.press(f'stime:{token}:1000', user=200)
        self.assertFalse(self.writes())
        self.api.activities[901]['responsibleId'] = 133
        self.press(f'stime:{token}:1000')
        self.assertFalse(self.writes())

    def test_cancel_keeps_todo_and_existing_form_untouched(self):
        token = self.prepare()
        original = deepcopy(self.api.activities[901])
        self.store.set_session(100, {'request_id': 'current', 'step': 'preview'})
        self.press('scancel:' + token)
        self.press(f'stime:{token}:1000')
        self.assertFalse(self.writes())
        self.assertEqual(self.api.activities[901], original)
        self.assertEqual(self.store.session(100)['request_id'], 'current')

    def test_old_notice_does_not_block_notification_at_new_deadline(self):
        token = self.prepare()
        self.now = self.due
        self.reminders.flush()
        self.press(f'stime:{token}:1000')
        new_due = __import__('datetime').datetime.fromisoformat(self.api.activities[901]['deadline'])
        self.now = new_due
        self.reminders.flush()
        rows = self.store.db.execute("SELECT deadline FROM crm_reminder_notices WHERE slot='due' AND status='sent'").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0][0], rows[1][0])

    def test_past_or_malformed_dates_do_not_crash_or_mutate(self):
        token = self.prepare()
        self.press('sday:' + token + ':99999999')
        self.press('sday:' + token + ':20000101')
        self.assertFalse(self.writes())


if __name__ == '__main__':
    unittest.main()
