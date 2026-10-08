"""Pharmacy creation must not depend on a redundant service comment."""
from copy import deepcopy
import unittest

import test_live_deals as live_fixture
from datfo_crm_bot.api import RemoteError
from datfo_crm_bot.sales_intake import CONTINUE_DEAL


class PharmacyNoteRecoveryTests(unittest.TestCase):
    setUp = live_fixture.LiveDealTests.setUp
    tearDown = live_fixture.LiveDealTests.tearDown
    say = live_fixture.LiveDealTests.say
    message = live_fixture.LiveDealTests.message
    use_offline_pharmacy_components = live_fixture.LiveDealTests.use_offline_pharmacy_components
    new_pharmacy_form = live_fixture.LiveDealTests.new_pharmacy_form

    def comment_api(self, crm):
        calls = []
        crm.needs_note = lambda kind: True
        crm.find_note = lambda state, result: False

        def deny(*args):
            calls.append(args)
            raise RemoteError('ACCESS_DENIED')

        crm.add_note = deny
        return calls

    def test_new_pharmacy_and_sale_succeed_without_comment_edit_permission(self):
        crm = self.use_offline_pharmacy_components()
        calls = self.comment_api(crm)
        state = self.new_pharmacy_form()
        self.say('Подтвердить')
        operation = self.store.operation(state['request_id'])
        self.assertEqual(operation['status'], 'succeeded')
        self.assertEqual(calls, [])
        self.assertEqual(len(crm.items), 1)
        self.assertEqual(self.api.rows[501]['parentId1034'], operation['result']['pharmacy_id'])
        self.assertEqual(self.api.activities[901]['responsibleId'], 132)

    def test_old_rejected_service_comment_resumes_with_same_pharmacy(self):
        crm = self.use_offline_pharmacy_components()
        calls = self.comment_api(crm)
        state = self.new_pharmacy_form()
        self.api.reject_create = True
        self.say('Подтвердить')
        child_id = state['pharmacy_form']['request_id']
        child = self.store.operation(child_id)
        snapshot = deepcopy(child['value'])
        pharmacy_id = child['result']['id']
        # Persist the partial journal left by the previous bot version.
        self.store.set_operation_step(child_id, 'note', 'rejected')
        self.store.status(child_id, 'created', child['result'])
        self.store.status(state['request_id'], 'created')
        self.api.reject_create = False
        self.say(CONTINUE_DEAL)
        self.say(CONTINUE_DEAL)
        self.assertEqual(self.store.operation(state['request_id'])['status'], 'succeeded')
        self.assertEqual(self.store.operation(child_id)['value'], snapshot)
        self.assertEqual(self.store.operation(child_id)['result']['id'], pharmacy_id)
        self.assertEqual(len(crm.records['companies']), 1)
        self.assertEqual(len(crm.records['requisites']), 1)
        self.assertEqual(len(crm.items), 1)
        self.assertEqual(len(self.api.rows), 1)
        self.assertEqual(len(self.api.activities), 1)
        self.assertEqual(calls, [])

    def test_explicit_legacy_comment_is_preserved_and_denial_reports_saved_pharmacy(self):
        crm = self.use_offline_pharmacy_components()
        calls = self.comment_api(crm)
        state = deepcopy(self.new_pharmacy_form())
        state['request_id'] += '-legacy'
        state['pharmacy_form']['request_id'] = state['request_id'] + '-pharmacy'
        state['pharmacy_form']['description'] = 'Вход со стороны двора'
        self.store.prepare(100, state, '2026-10-07T00:00:00Z')
        self.store.set_session(100, state)
        self.say('Подтвердить')
        response = self.telegram.messages[-1][1]
        self.assertIn('Аптека уже создана', response)
        self.assertIn('ваш комментарий', response)
        self.assertNotIn('не разрешил боту создать аптеку', response)
        self.assertEqual(calls[0][0]['description'], 'Вход со стороны двора')
        self.assertEqual(self.api.rows, {})
        comments = []
        crm.add_note = lambda state, result, manager, user: comments.append(state['description'])
        self.say(CONTINUE_DEAL)
        self.assertEqual(comments, ['Вход со стороны двора'])
        self.assertEqual(len(crm.items), 1)
        self.assertEqual(self.store.operation(state['request_id'])['status'], 'succeeded')

    def test_company_permission_denial_is_not_reported_as_pharmacy_creation_denial(self):
        crm = self.use_offline_pharmacy_components()
        self.new_pharmacy_form()

        def deny(*args):
            raise RemoteError('ACCESS_DENIED')

        crm.create_company = deny
        self.say('Подтвердить')
        response = self.telegram.messages[-1][1]
        self.assertNotIn('не разрешил боту создать аптеку', response)
        self.assertEqual(self.live.bot.sales.okb.last_error_step, 'company')
        self.assertEqual(crm.items, {})
        self.assertEqual(self.api.rows, {})


if __name__ == '__main__':
    unittest.main()
