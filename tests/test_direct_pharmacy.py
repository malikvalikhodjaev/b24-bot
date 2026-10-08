"""A direct pharmacy form keeps ownership, drafts and save-on-confirm semantics."""
from copy import deepcopy
import json
from urllib.parse import parse_qs, urlsplit
import unittest

import test_navigation as fixture
from test_intake_forms import payload
from datfo_crm_bot.app import dispatch
from datfo_crm_bot.input_forms import launch_matches, consume_launch
from datfo_crm_bot.okb_service import OKB
from datfo_crm_bot.role_access import RoleAccess
from datfo_crm_bot.service import CONFIRM


class DirectPharmacyTests(unittest.TestCase):
    setUp = fixture.NavigationTests.setUp
    tearDown = fixture.NavigationTests.tearDown
    update = fixture.NavigationTests.update
    language = fixture.NavigationTests.language

    def token(self, user=200):
        button = next(button for row in self.registration.more_keyboard(user) for button in row
                      if isinstance(button, dict) and button['text'] == OKB)
        query = parse_qs(urlsplit(button['web_app']['url']).query)
        self.assertEqual(query['mode'], ['pharmacy'])
        return query['token'][0]

    def receive(self, data, user=200, update=None):
        update = update or self.update('', user)
        update['message'].pop('text', None)
        update['message']['web_app_data'] = {'data': json.dumps(data, ensure_ascii=False)}
        dispatch(update, self.ui, [RoleAccess(self.registration, self.telegram).handle,
                                  self.navigation.handle, self.work.handle, self.live.handle,
                                  self.okb.handle, self.registration.handle])
        return update

    def data(self, token, **changes):
        return payload(self.crm, mode='pharmacy', token=token, contact_policy='optional', **changes)

    def test_direct_form_previews_then_confirm_and_replay_create_once(self):
        token = self.token()
        initial_records = deepcopy(self.crm.records)
        self.assertIsNone(self.primary.session(200))
        update = self.receive(self.data(token))
        state = deepcopy(self.primary.session(200))
        self.assertEqual((state['workflow'], state['step']), ('okb', 'confirm'))
        self.assertFalse(launch_matches(self.primary, 200, 'pharmacy', token))
        self.assertEqual(self.crm.items, {})
        self.assertEqual(self.crm.records, initial_records)
        self.receive(self.data(token), update=update)
        self.assertEqual(self.primary.session(200), state)
        self.assertEqual(self.crm.items, {})
        confirmation = self.update(CONFIRM)
        self.okb.handle(confirmation)
        created = deepcopy(self.crm.items)
        self.assertEqual(len(created), 1)
        self.okb.handle(confirmation)
        self.assertEqual(self.crm.items, created)

    def test_another_manager_cannot_use_owners_token(self):
        token = self.token()
        self.receive(self.data(token), user=100)
        self.assertIsNone(self.primary.session(100))
        self.assertTrue(launch_matches(self.primary, 200, 'pharmacy', token))
        self.assertEqual(self.crm.items, {})

    def test_closed_token_and_technician_cannot_create_a_pharmacy(self):
        token = self.token()
        consume_launch(self.primary, 200, 'pharmacy', token)
        self.receive(self.data(token))
        self.assertIsNone(self.primary.session(200))
        self.receive(self.data(token), user=300)
        self.assertIsNone(self.primary.session(300))
        self.assertEqual(self.crm.items, {})

    def test_existing_sale_or_support_draft_is_not_replaced(self):
        token = self.token()
        sale = {'request_id': 'active-sale', 'workflow': 'live_deal', 'kind': 'deal', 'step': 'sales_bulk_input'}
        self.primary.set_session(200, sale)
        self.receive(self.data(token))
        self.assertEqual(self.primary.session(200), sale)
        self.assertTrue(launch_matches(self.primary, 200, 'pharmacy', token))
        self.primary.set_session(200, None)
        support = {'request_id': 'active-support', 'step': 'web_form', 'web_intake': True}
        self.store.set_session(200, support)
        self.receive(self.data(token))
        self.assertIsNone(self.primary.session(200))
        self.assertEqual(self.store.session(200), support)
        self.assertTrue(launch_matches(self.primary, 200, 'pharmacy', token))
        self.assertEqual(self.crm.items, {})

    def test_invalid_fields_keep_same_form_for_correction(self):
        token = self.token()
        self.receive(self.data(token, inn='1'))
        state = self.primary.session(200)
        self.assertEqual((state['workflow'], state['step']), ('okb', 'okb_bulk_input'))
        self.assertEqual(self.token(), token)
        self.receive(self.data(token))
        self.assertEqual(self.primary.session(200)['step'], 'confirm')
        self.assertEqual(self.crm.items, {})
