from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
import unittest

import test_work_inbox as fixture
from datfo_crm_bot.guidance import with_guide
from datfo_crm_bot.i18n import LocalizedTelegram, tr
from datfo_crm_bot.input_forms import launch_button, launch_matches, consume_launch, form_keyboard
from datfo_crm_bot.language_ui import LanguageUI
from datfo_crm_bot.registration import Registration
from datfo_crm_bot.service import DEAL, SUPPORT, MENU
from datfo_crm_bot.work_inbox import NEW


class DirectLaunchMenuTests(unittest.TestCase):
    tearDown = fixture.WorkInboxTests.tearDown

    def setUp(self):
        fixture.WorkInboxTests.setUp(self)
        self.raw = self.telegram
        self.telegram = LocalizedTelegram(self.raw, self.primary)
        self.ui = LanguageUI(self.registration, self.telegram)
        self.registration.telegram = self.live.telegram = self.live.bot.telegram = self.work.telegram = self.telegram

    def assert_direct(self, button, mode, user=200):
        self.assertIsInstance(button, dict)
        url = urlsplit(button['web_app']['url'])
        self.assertEqual((url.scheme, url.netloc, url.path), ('https', 'fom-analytics.uz', '/bot-form/'))
        query = parse_qs(url.query)
        self.assertEqual(query['mode'], [mode])
        self.assertTrue(launch_matches(self.primary, user, mode, query['token'][0]))
        return query['token'][0]

    def test_old_stats_or_error_menu_restores_two_direct_buttons_in_both_languages(self):
        source = with_guide(deepcopy(MENU))
        original = deepcopy(source)
        for language in ('ru', 'uz'):
            with self.primary.db:
                self.primary.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('language:200', language))
            self.telegram.send(200, 'Меню', source)
            keyboard = self.raw.messages[-1][2]
            deal = next(b for row in keyboard for b in row if isinstance(b, dict) and b['text'] == tr(DEAL, language))
            request = next(b for row in keyboard for b in row if isinstance(b, dict) and b['text'] == tr(SUPPORT, language))
            self.assert_direct(deal, 'deal')
            self.assert_direct(request, 'support')
            self.assertEqual(parse_qs(urlsplit(deal['web_app']['url']).query)['lang'], [language])
        self.assertEqual(source, original)
        self.assertIsNone(self.primary.session(200))
        self.assertIsNone(self.work.store.session(200))
        self.assertFalse(self.api.calls)

    def test_cached_text_menu_replay_also_has_direct_buttons(self):
        self.live.bot.config.users[200] = self.registration.user(200)
        self.primary.commit_reply(901, 200, 200, None, 'Сохранено', with_guide(MENU))
        update = {'update_id': 901, 'message': {'from': {'id': 200}, 'chat': {'id': 200, 'type': 'private'}, 'text': '/pending'}}
        self.live.bot.handle(update)
        buttons = [b for row in self.raw.messages[-1][2] for b in row if isinstance(b, dict)]
        self.assertEqual({parse_qs(urlsplit(b['web_app']['url']).query)['mode'][0] for b in buttons}, {'deal', 'support'})
        self.assertIsNone(self.primary.session(200))
        self.assertFalse(self.api.calls)

    def test_support_inbox_and_sendmessage_reply_keyboard_are_direct(self):
        self.telegram.call('sendMessage', {'chat_id': 200, 'text': 'Заявки', 'reply_markup': {'keyboard': [[NEW]]}})
        self.assert_direct(self.raw.calls[-1][1]['reply_markup']['keyboard'][0][0], 'support')
        self.work.say(200, 'Заявки')
        self.assert_direct(self.raw.messages[-1][2][0][0], 'support')
        self.assert_direct(self.registration.keyboard(200)[0][0], 'deal')
        self.assert_direct(self.registration.keyboard(200)[0][1], 'support')

    def test_already_used_cached_menu_button_gets_new_token_without_consuming_draft(self):
        button = launch_button(self.primary, 200, 'deal', DEAL)
        first = self.assert_direct(button, 'deal')
        consume_launch(self.primary, 200, 'deal', first)
        state = {'step': 'confirm', 'request_id': 'still-active', 'workflow': 'live_deal'}
        self.primary.set_session(200, state)
        self.telegram.send(200, 'Меню', [[button]])
        second = self.assert_direct(self.raw.messages[-1][2][0][0], 'deal')
        self.assertNotEqual(first, second)
        self.assertEqual(self.primary.session(200), state)

    def test_existing_form_button_is_kept_bound_to_current_draft(self):
        state = {'request_id': 'datfo-live-existing-draft', 'form_mode': 'deal'}
        keyboard = form_keyboard(state, self.primary, 200)
        self.telegram.send(200, 'Продолжите', keyboard)
        received = self.raw.messages[-1][2][0][0]
        self.assertEqual(received['web_app'], keyboard[0][0]['web_app'])
        self.assertEqual(parse_qs(urlsplit(received['web_app']['url']).query)['token'], [state['request_id']])

    def test_existing_unopened_support_form_resumes_without_a_new_token(self):
        self.work.intake.start(200)
        before = deepcopy(self.work.store.session(200))
        self.telegram.send(200, 'Меню', [[SUPPORT, NEW]])
        for button in self.raw.messages[-1][2][0]:
            self.assertEqual(parse_qs(urlsplit(button['web_app']['url']).query)['token'], [before['request_id']])
            self.assertEqual(parse_qs(urlsplit(button['web_app']['url']).query)['mode'], ['support'])
        self.assertEqual(self.work.store.session(200), before)
        self.assertIsNone(self.primary.db.execute("SELECT value FROM settings WHERE key='webapp-launch:support:200'").fetchone())

    def test_existing_unopened_deal_form_resumes_without_losing_its_state(self):
        self.live.bot.config.users[200] = self.registration.user(200)
        _, _, state = self.live.bot.route(200, '/deal', None)
        self.primary.set_session(200, state)
        self.assertEqual(state['step'], 'sales_bulk_input')
        self.telegram.send(200, 'Меню', [[DEAL]])
        button = self.raw.messages[-1][2][0][0]
        self.assertEqual(parse_qs(urlsplit(button['web_app']['url']).query)['token'], [state['request_id']])
        self.assertEqual(self.primary.session(200), state)

    def test_unapproved_and_other_roles_do_not_receive_authorized_form_links(self):
        for user in (300, 400, 600):
            self.telegram.send(user, 'Меню', [[DEAL, SUPPORT]])
            self.assertEqual(self.raw.messages[-1][2], self.registration.keyboard(user) if user in (300,400) else [[DEAL, SUPPORT]])
        self.primary.deny_registration(200, 100, 'revoked')
        self.telegram.send(200, 'Меню', [[DEAL, SUPPORT]])
        self.assertEqual(self.raw.messages[-1][2], [[DEAL, SUPPORT]])
        self.assertEqual(self.primary.db.execute("SELECT count(*) FROM settings WHERE key LIKE 'webapp-launch:%'").fetchone()[0], 0)

    def test_training_modes_keep_their_own_buttons_and_inline_callbacks_are_untouched(self):
        self.work.simulation = SimpleNamespace(active=lambda user: True)
        self.telegram.send(200, 'Симуляция', [[DEAL, SUPPORT]])
        self.assertEqual(self.raw.messages[-1][2], [[DEAL, SUPPORT]])
        self.work.simulation = None
        self.work.test_inbox = SimpleNamespace(store=SimpleNamespace(mode=lambda user: {'role': 'sales'}))
        self.telegram.send(200, 'Инбокс-тест', [[DEAL, SUPPORT]])
        self.assertEqual(self.raw.messages[-1][2], [[DEAL, SUPPORT]])
        self.work.test_inbox = None
        buttons = [[{'text': DEAL, 'callback_data': 'guide:deal'}]]
        self.telegram.call('sendMessage', {'chat_id': 200, 'text': 'Помощь', 'reply_markup': {'inline_keyboard': buttons}})
        self.assertEqual(self.raw.calls[-1][1]['reply_markup']['inline_keyboard'], buttons)

    def test_registration_binds_delivery_wrapper_during_real_app_bootstrap(self):
        self.telegram.registration = None
        Registration(self.registration.settings, self.primary, self.registration.directory, self.telegram)
        self.assertIsNotNone(self.telegram.registration)
        self.assertEqual(self.telegram.registration.store, self.primary)


if __name__ == '__main__':
    unittest.main()
