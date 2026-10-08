from copy import deepcopy
from datetime import datetime
import html
import re
from types import SimpleNamespace
import unittest

import test_work_inbox as fixture
from test_crm_statistics import CountingApi
from datfo_crm_bot.api import RemoteError
from datfo_crm_bot.app import dispatch
from datfo_crm_bot.crm_statistics import CreationStatistics
from datfo_crm_bot.i18n import LocalizedTelegram, tr
from datfo_crm_bot.language_ui import LanguageUI
from datfo_crm_bot.okb_crm import OkbSettings
from datfo_crm_bot.personal_statistics import PersonalStatistics, MY_STATS, KINDS


class PersonalStatisticsTests(unittest.TestCase):
    tearDown = fixture.WorkInboxTests.tearDown

    def setUp(self):
        fixture.WorkInboxTests.setUp(self)
        self.raw = self.telegram
        self.telegram = LocalizedTelegram(self.raw, self.primary)
        self.api = CountingApi((1, 127, 2, 2, 263, 3, 3, 374, 4, 4, 485, 5))
        for user in (100, 200, 300):
            for kind, ident in (('deal', 701), ('pharmacy', 801), ('support', 901)):
                request = f'authored-{user}-{kind}'
                self.primary.prepare(user, {'kind': kind, 'request_id': request}, '2026-10-07T00:00:00Z')
                self.primary.status(request, 'succeeded', {'id': ident})
            self.primary.set_operation_step(f'authored-{user}-pharmacy', 'contact', 'succeeded_write', {'id': 1001})
        self.statistics = CreationStatistics(self.api, OkbSettings.load().pharmacy, 47)
        self.now = datetime.fromisoformat('2026-10-07T08:30:00+05:00')
        self.overview = PersonalStatistics(self.registration, self.telegram, self.statistics, clock=lambda: self.now)
        self.ui = LanguageUI(self.registration, self.telegram)
        for user in (100, 200, 300):
            with self.primary.db:
                self.primary.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('language:'+str(user), 'ru'))

    def message(self, text=MY_STATS, user=200):
        return {'message': {'from': {'id': user}, 'chat': {'id': user, 'type': 'private'}, 'text': text}}

    def callback(self, period='week', user=200):
        return {'callback_query': {'id': 'stats-query', 'from': {'id': user},
                'message': {'message_id': 51, 'chat': {'id': user, 'type': 'private'}},
                'data': 'personal_stats:'+period}}

    def say(self, update):
        dispatch(update, self.ui, [self.overview.handle, self.work.handle, self.live.handle, self.registration.handle])

    def test_one_click_compact_month_table_has_contacts_and_never_admin_team(self):
        self.say(self.message(user=100))
        self.assertEqual([method for method, _ in self.raw.calls], ['sendMessage', 'editMessageText'])
        card = self.raw.calls[-1][1]
        self.assertIn('Моя статистика', card['text'])
        self.assertIn('01.10–07.10.2026', card['text'])
        self.assertIn('Контакты', card['text'])
        self.assertIn('370', card['text'])
        self.assertIn('без записей бота', card['text'])
        self.assertLess(len(card['text']), 650)
        self.assertNotIn('/crm_deal', card['text'])
        for method, payload in self.api.calls:
            self.assertNotIn('assignedById', payload['filter'])
            self.assertNotIn('ASSIGNED_BY_ID', payload['filter'])
            if '@id' not in payload['filter'] and '@ID' not in payload['filter']:
                field = 'createdBy' if method == 'crm.item.list' else 'CREATED_BY_ID'
                self.assertEqual(payload['filter'][field], 132)
        self.assertEqual(card['reply_markup']['inline_keyboard'][1][0]['text'], '✓ Месяц')

    def test_periods_use_tashkent_midnight_and_all_history_without_fake_start(self):
        self.overview.clock = lambda: datetime.fromisoformat('2026-10-06T21:30:00+00:00')
        expected = {'today': '2026-10-07T00:00:00+05:00', 'week': '2026-10-05T00:00:00+05:00',
                    'month': '2026-10-01T00:00:00+05:00', 'all': None}
        for period, value in expected.items():
            self.api.calls.clear()
            self.say(self.callback(period))
            for method, payload in self.api.calls:
                field = 'createdTime' if method == 'crm.item.list' else 'DATE_CREATE'
                expected_start = datetime.fromisoformat(value).strftime('%Y-%m-%d %H:%M:%S') if value and method == 'crm.item.list' else value
                self.assertEqual(payload['filter'].get('>='+field), expected_start)
                self.assertEqual(payload['filter']['<'+field], '2026-10-07 02:30:00' if method == 'crm.item.list' else '2026-10-07T02:30:00+05:00')
            self.assertEqual(self.raw.calls[-1][0], 'editMessageText')
            self.assertEqual(self.raw.calls[-1][1]['message_id'], 51)

    def test_contact_counts_use_bot_authorship_and_native_creator_not_current_owner(self):
        self.say(self.message())
        queries = [(method, payload['filter']) for method, payload in self.api.calls]
        self.assertEqual(queries[6][0], 'crm.contact.list')
        self.assertEqual(queries[6][1]['ORIGINATOR_ID'], 'datfo_telegram_okb')
        self.assertEqual(queries[6][1]['@ID'], [1001])
        self.assertNotIn('ASSIGNED_BY_ID', queries[6][1])
        self.assertNotIn('CATEGORY_ID', queries[6][1])
        self.assertEqual(queries[7][0], 'crm.contact.list')
        self.assertNotIn('ORIGINATOR_ID', queries[7][1])
        self.assertEqual(queries[7][1]['CREATED_BY_ID'], 133)

    def test_open_forms_registration_and_work_drafts_are_unchanged(self):
        sales = {'step': 'sales_okb', 'workflow': 'live_deal', 'pharmacy_form': {'step': 'okb_name'}}
        registration = {'step': 'name'}
        support = {'step': 'title', 'title': 'Моя заявка'}
        self.primary.set_session(200, sales)
        self.primary.set_registration_session(200, registration)
        self.work.store.set_session(200, support)
        self.say(self.message())
        self.api.calls.clear()
        self.say(self.callback('all'))
        self.assertEqual(self.primary.session(200), sales)
        self.assertEqual(self.primary.registration_session(200), registration)
        self.assertEqual(self.work.store.session(200), support)
        self.assertFalse(self.live.crm.api.calls)
        self.assertTrue(all(method.endswith('.list') for method, _ in self.api.calls))

    def test_partial_error_is_unknown_and_real_zero_is_zero(self):
        self.api.totals = [RemoteError('ACCESS_DENIED'), 127, 2, 0, '260', 3, True, 374, 4, 4, 485, 5]
        self.say(self.message())
        card = self.raw.calls[-1][1]['text']
        self.assertIn('—', card)
        self.assertIn('данные временно недоступны', card)
        self.assertNotIn('ACCESS_DENIED', card)
        self.assertNotIn('INVALID_', card)
        self.assertRegex(card, r'Аптеки\s+0\s+—')

    def test_ru_uz_button_and_card_are_localized_for_manager(self):
        for user, language in ((200, 'uz'), (200, 'ru')):
            with self.primary.db:
                self.primary.db.execute('UPDATE settings SET value=? WHERE key=?', (language, 'language:'+str(user)))
            self.api.calls.clear()
            self.say(self.message(tr(MY_STATS, language), user))
            card = self.raw.calls[-1][1]
            self.assertIn(tr('📊 <b>Моя статистика</b>', language), card['text'])
            labels = [button['text'] for row in card['reply_markup']['inline_keyboard'] for button in row]
            self.assertIn(tr('🔄 Обновить', language), labels)
            menu = [button['text'] if isinstance(button, dict) else button
                    for row in self.registration.more_keyboard(user) for button in row]
            self.assertIn(MY_STATS, menu)
        self.assertNotIn(MY_STATS, [b for row in self.registration.keyboard(600) for b in row])

    def test_technician_cannot_read_manager_statistics_even_from_old_button(self):
        self.api.calls.clear()
        self.assertTrue(self.overview.handle(self.message(user=300)))
        self.assertTrue(self.overview.handle(self.callback(user=300)))
        self.assertFalse(self.api.calls)
        self.assertNotIn(MY_STATS,[b for row in self.registration.keyboard(300) for b in row])

    def test_unregistered_revoked_and_group_queries_never_access_crm(self):
        self.assertTrue(self.overview.handle(self.message(user=600)))
        self.primary.deny_registration(200, 100, 'revoked')
        self.assertTrue(self.overview.handle(self.callback(user=200)))
        update = self.message(user=300)
        update['message']['chat']['type'] = 'group'
        self.assertFalse(self.overview.handle(update))
        update = self.callback(user=300)
        update['callback_query']['message']['chat']['id'] = 200
        self.assertFalse(self.overview.handle(update))
        self.assertFalse(self.api.calls)

    def test_simulations_and_malformed_callback_do_not_query_production(self):
        self.overview.simulation = SimpleNamespace(active=lambda user: True)
        self.assertFalse(self.overview.handle(self.message()))
        self.overview.simulation = None
        self.overview.inbox = SimpleNamespace(store=SimpleNamespace(mode=lambda user: {'role': 'sales'}))
        self.assertFalse(self.overview.handle(self.callback()))
        self.overview.inbox = None
        self.assertTrue(self.overview.handle(self.callback('all:132')))
        self.assertFalse(self.api.calls)

    def test_unrelated_text_remains_available_to_current_form(self):
        self.assertFalse(self.overview.handle(self.message('909754744')))
        self.assertFalse(self.overview.handle(self.message('/stats')))
        self.assertFalse(self.api.calls)

    def test_removed_message_keeps_result_accessible(self):
        original = self.raw.call
        def call(method, payload=None, **kwargs):
            if method == 'editMessageText':
                raise RemoteError('TELEGRAM_400')
            return original(method, payload, **kwargs)
        self.raw.call = call
        self.say(self.callback())
        self.assertEqual(self.raw.calls[-1][0], 'sendMessage')
        self.assertIn('Моя статистика', self.raw.calls[-1][1]['text'])

    def test_refresh_unchanged_plain_telegram_text_does_not_duplicate_card(self):
        self.say(self.message())
        card = self.raw.calls[-1][1]
        update = self.callback('month')
        update['callback_query']['message'].update(
            text=html.unescape(re.sub(r'</?[^>]+>', '', card['text'])), reply_markup=deepcopy(card['reply_markup']))
        self.raw.calls.clear()
        self.api.calls.clear()
        self.say(update)
        self.assertEqual(len(self.api.calls), 12)
        self.assertEqual([method for method, _ in self.raw.calls], ['answerCallbackQuery'])


if __name__ == '__main__':
    unittest.main()
