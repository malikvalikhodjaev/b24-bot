from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from datfo_crm_bot.app import dispatch
from datfo_crm_bot.demo import demo_config
from datfo_crm_bot.i18n import LocalizedTelegram, tr
from datfo_crm_bot.language_ui import LanguageUI
from datfo_crm_bot.navigation import HOME, MORE, Navigation
from datfo_crm_bot.okb_access import OkbAccess
from datfo_crm_bot.okb_service import OKB, CREATE_CONTACT, USE_CONTACT_PHONE
from datfo_crm_bot.service import CANCEL, DEAL, SUPPORT
from datfo_crm_bot.simulation import SimulationCrm
from datfo_crm_bot.sales_intake import NEXT_DAY
import test_work_inbox as work_fixture


class NavigationTests(unittest.TestCase):
    def setUp(self):
        work_fixture.WorkInboxTests.setUp(self)
        self.raw = self.telegram
        self.telegram = LocalizedTelegram(self.raw, self.primary)
        self.registration.telegram = self.telegram
        self.live.telegram = self.telegram
        self.live.bot.telegram.telegram = self.telegram
        self.work.telegram = self.telegram
        self.crm = SimulationCrm(self.primary)
        self.okb = OkbAccess(self.registration, replace(demo_config(self.path), users={}), self.crm, self.telegram)
        self.ui = LanguageUI(self.registration, self.telegram)
        self.navigation = Navigation(self.registration, self.telegram)
        for user in (100, 200, 300):
            self.language(user, 'ru')

    tearDown = work_fixture.WorkInboxTests.tearDown

    def language(self, user, language):
        with self.primary.db:
            self.primary.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('language:' + str(user), language))

    def update(self, text, user=200):
        self.sequence += 1
        return {'update_id': self.sequence, 'message': {'from': {'id': user}, 'chat': {'id': user, 'type': 'private'}, 'text': text}}

    def say(self, text, user=200):
        update = self.update(text, user)
        dispatch(update, self.ui, [self.navigation.handle, self.work.handle, self.live.handle, self.okb.handle, self.registration.handle])
        return update

    def contact_state(self, step='okb_contact_phone'):
        state = {'kind': 'pharmacy', 'workflow': 'okb', 'request_id': 'new-pharmacy',
                 'step': step, 'phone': '+998909754744', 'title': 'Аптека', 'create_contact': True}
        self.primary.set_session(200, state)
        return state

    def test_cancel_then_bare_phone_opens_useful_sections_without_profile_dump(self):
        self.contact_state()
        self.say(CANCEL)
        self.assertIsNone(self.primary.session(200))
        self.assertIn('Форма закрыта', self.raw.messages[-1][1])
        self.assertIn(MORE, str(self.raw.messages[-1][2]))
        self.say(MORE)
        self.assertIn(OKB, str(self.raw.messages[-1][2]))
        self.say('909754744')
        response = self.raw.messages[-1][1]
        self.assertIn('главное меню', response)
        self.assertIn('после названия аптеки', response)
        self.assertNotIn('Telegram ID', response)
        self.assertNotIn('Подтверждена', response)
        self.assertIsNone(self.primary.session(200))
        self.assertEqual(self.crm.items, {})

    def test_unknown_idle_text_does_not_show_profile_but_profile_still_works(self):
        self.say('Что-то непонятное')
        self.assertIn('Выберите раздел', self.raw.messages[-1][1])
        self.assertNotIn('📥 Заявки в техподдержку', self.raw.messages[-1][1])
        self.assertNotIn('Б24 #133', self.raw.messages[-1][1])
        self.say('/profile')
        self.assertIn('Менеджер ФОМ · #133', self.raw.messages[-1][1])
        self.assertIn('Рабочая роль: Менеджер продаж', self.raw.messages[-1][1])

    def test_technician_gets_role_specific_menu_and_phone_explanation(self):
        self.say('909754744', 300)
        response, keyboard = self.raw.messages[-1][1:]
        self.assertIn('Откройте нужную заявку', response)
        self.assertIn('📥 Заявки в техподдержку', response)
        self.assertNotIn('Аптека в ОКБ', response)
        self.assertNotIn(OKB, str(keyboard))
        self.assertNotIn(tr(DEAL, 'ru'), str(keyboard))

    def test_phone_in_contact_name_keeps_step_and_never_saves_number_as_name(self):
        before = self.contact_state('okb_contact_name')
        self.say('909754744')
        self.assertEqual(self.primary.session(200), before)
        self.assertIn('записываем имя контакта', self.raw.messages[-1][1])
        self.assertIn('нужен текст', self.raw.messages[-1][1])
        self.assertIn(HOME, str(self.raw.messages[-1][2]))
        self.assertEqual(len(self.crm.records['contacts']), 3)

    def test_position_error_preserves_named_choices_and_can_resume(self):
        before = self.contact_state('okb_contact_position')
        before.update(contact_name='Малика', contact_position_candidates=self.crm.contact_positions())
        self.primary.set_session(200, before)
        self.say('909754744')
        self.assertEqual(self.primary.session(200), before)
        self.assertIn('должность контакта', self.raw.messages[-1][1])
        self.assertIn('1. Владелец', str(self.raw.messages[-1][2]))
        self.say('1')
        self.assertEqual(self.primary.session(200)['step'], 'okb_region')

    def test_invalid_phone_restores_use_previous_number_button(self):
        before = self.contact_state()
        self.say('не номер')
        self.assertEqual(self.primary.session(200), before)
        self.assertIn('телефон нового контакта', self.raw.messages[-1][1])
        self.assertIn(tr(USE_CONTACT_PHONE, 'ru'), str(self.raw.messages[-1][2]))
        self.say(USE_CONTACT_PHONE)
        self.assertEqual(self.primary.session(200)['step'], 'okb_contact_name')

    def test_valid_numeric_inn_and_phone_are_not_mistaken_for_out_of_order_input(self):
        self.say('/okb')
        # Restore the INN step of a draft created before whole-form entry.
        self.primary.set_session(200, {**self.primary.session(200), 'step': 'okb_inn'})
        self.say('123456789')
        self.assertEqual(self.primary.session(200)['step'], 'okb_company')
        self.say('1')
        self.say('Аптека Нур')
        self.say('909754744')
        self.assertEqual(self.primary.session(200)['step'], 'okb_contact_create')
        self.assertEqual(self.primary.session(200)['phone'], '+998909754744')

    def test_embedded_pharmacy_validation_explains_child_step_and_keeps_parent(self):
        child = self.contact_state('okb_region')
        child['region_candidates'] = self.crm.choices('business_region')
        parent = {'workflow': 'live_deal', 'kind': 'deal', 'sales_intake': True, 'step': 'sales_okb',
                  'request_id': 'parent', 'category_id': 45, 'initial_stage': 'C45:PLAN', 'pharmacy_form': child}
        self.primary.set_session(200, parent)
        self.say('909754744')
        self.assertEqual(self.primary.session(200), parent)
        self.assertIn('бизнес-регион', self.raw.messages[-1][1])
        self.assertIn(child['region_candidates'][0]['title'], str(self.raw.messages[-1][2]))

    def test_bad_sales_date_keeps_deadline_shortcut(self):
        state = {'workflow': 'live_deal', 'kind': 'deal', 'sales_intake': True, 'step': 'sales_deadline',
                 'request_id': 'sale', 'category_id': 45, 'initial_stage': 'C45:PLAN'}
        self.primary.set_session(200, state)
        self.say('909754744')
        self.assertEqual(self.primary.session(200), state)
        self.assertIn('дату напоминания', self.raw.messages[-1][1])
        self.assertIn(NEXT_DAY, str(self.raw.messages[-1][2]))

    def test_home_and_section_buttons_do_not_replace_or_enter_data_into_request(self):
        self.say('/request 501')
        draft = deepcopy(self.work.store.session(200))
        self.say(HOME)
        self.assertEqual(self.work.store.session(200), draft)
        self.assertIn('Черновик сохранён', self.raw.messages[-1][1])
        self.say(tr(DEAL, 'ru'))
        self.assertEqual(self.work.store.session(200), draft)
        self.assertIsNone(self.primary.session(200))
        self.say('/cancel')
        self.assertIsNone(self.work.store.session(200))
        self.assertIn(501, self.api.rows)
        self.say('/deal')
        self.assertEqual(self.primary.session(200)['step'], 'sales_bulk_input')

    def test_request_wrong_choice_explains_recipient_step_and_preserves_choices(self):
        self.say('/request 501')
        before = deepcopy(self.work.store.session(200))
        self.say('909754744')
        self.assertEqual(self.work.store.session(200), before)
        self.assertIn('выбираем маршрут', self.raw.messages[-1][1])
        self.assertIn('Это номер телефона', self.raw.messages[-1][1])
        self.assertIn('На распределение', str(self.raw.messages[-1][2]))
        self.assertIn(HOME, str(self.raw.messages[-1][2]))

    def test_uzbek_home_alias_preserves_draft_and_does_not_mix_russian_help(self):
        self.language(200, 'uz')
        before = self.contact_state('okb_contact_name')
        self.say('909754744')
        self.assertIn('контакт исмини', self.raw.messages[-1][1])
        self.assertNotIn('На этом шаге', self.raw.messages[-1][1])
        self.assertIn(tr(HOME, 'uz'), str(self.raw.messages[-1][2]))
        self.say(tr(HOME, 'uz'))
        self.assertEqual(self.primary.session(200), before)
        self.assertIn('Қоралама сақланди', self.raw.messages[-1][1])

    def test_pending_write_cannot_be_discarded_via_navigation(self):
        state = self.contact_state()
        self.primary.prepare(200, state, '2026-10-07T00:00:00Z')
        self.primary.status(state['request_id'], 'uncertain')
        self.assertTrue(self.navigation.handle(self.update('/cancel')))
        self.assertEqual(self.primary.session(200), state)
        self.assertTrue(self.primary.unfinished(200))
        self.assertIn('Сначала нажмите', self.raw.messages[-1][1])
        self.primary.set_session(200, None)
        self.assertTrue(self.navigation.handle(self.update(HOME)))
        self.assertTrue(self.primary.unfinished(200))

    def test_replayed_cancel_does_not_delete_new_draft(self):
        self.contact_state()
        update = self.say('/cancel')
        new_state = self.contact_state('okb_contact_name')
        self.assertTrue(self.navigation.handle(update))
        self.assertEqual(self.primary.session(200), new_state)

    def test_text_containing_phone_is_valid_request_description(self):
        self.say('/request 501')
        self.say('1')
        self.say('Помогите подключить')
        self.say('Не работает доступ, позвоните 909754744')
        draft = self.work.store.session(200)
        self.assertEqual(draft['step'], 'confirm')
        self.assertIn('909754744', draft['description'])


if __name__ == '__main__':
    unittest.main()
