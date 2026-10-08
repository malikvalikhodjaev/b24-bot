from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from datfo_crm_bot.api import RemoteError
from datfo_crm_bot.input_forms import TEXT_FORM, STEP_FORM, WEB_FORM, parse_text, collect, form_keyboard
from datfo_crm_bot.intake_ui import ADD_LOCATION, SKIP_LOCATION, ADD_REMINDER, NO_REMINDER, coordinates
from datfo_crm_bot.i18n import language_context
from datfo_crm_bot.okb_service import CREATE_CONTACT, CONTINUE
from datfo_crm_bot.okb_crm import OkbCrm, OkbSettings
from datfo_crm_bot.reminder_calendar import PREVIOUS_MONTH, NEXT_MONTH
from datfo_crm_bot.sales_intake import USE_PHARMACY, USE_ADDRESS
import test_okb as okb_fixture
import test_live_deals as live_fixture


def payload(crm, **changes):
    region = crm.choices('business_region')[2]
    city = crm.cities_for_region(region['id'])[0]
    value = {'inn': '111222333', 'company_name': 'Фирма <А>', 'title': 'Аптека <А>',
             'address': 'Учебная улица, 1', 'business_region': region['id'], 'city': city['id'],
             'program': '5919', 'phone': '', 'create_contact': False, 'reminder': False, 'discussion_programs':['5246']}
    value.update(changes)
    return value


class PharmacyIntakeTests(unittest.TestCase):
    setUp, tearDown, say, form, contact_question = okb_fixture.OkbTests.setUp, okb_fixture.OkbTests.tearDown, okb_fixture.OkbTests.say, okb_fixture.OkbTests.form, okb_fixture.OkbTests.contact_question

    def test_new_pharmacy_has_only_whole_form_inputs_and_cannot_start_old_walk(self):
        self.legacy_entries = False
        publication = Path(self.folder.name) / 'data'
        publication.mkdir()
        (publication / 'miniapp-publication.json').write_text(json.dumps({
            'verified': True, 'url': 'https://fom-analytics.uz/bot-form/'}), encoding='utf-8')
        with patch('datfo_crm_bot.input_forms.ROOT', Path(self.folder.name)):
            self.say('/okb')
            original = deepcopy(self.store.session(100))
            self.assertEqual(original['step'], 'okb_bulk_input')
            self.say('123456789')
            self.assertEqual(self.store.session(100), original)
            self.assertEqual(self.crm.records['companies'], {})
            for action in (STEP_FORM, TEXT_FORM):
                self.say(action)
                keyboard = self.telegram.messages[-1][2]
                self.assertNotIn(STEP_FORM, str(keyboard))
                self.assertNotIn(TEXT_FORM, str(keyboard))
                self.assertIn(WEB_FORM, str(keyboard))
                self.assertEqual(self.store.session(100)['step'], 'okb_bulk_input')

    def send_location(self, point):
        self.number += 1
        self.bot.handle({'update_id': self.number, 'message': {'from': {'id': 100},
            'chat': {'id': 100, 'type': 'private'}, 'location': point}})

    def to_location(self):
        for text in ('/okb', '123456789', '1', 'Аптека', 'Без телефона', '3', '1', 'Улица, 1'):
            self.say(text)

    def test_confirm_contact_reuses_phone_and_asks_name_immediately(self):
        self.contact_question()
        self.say(CREATE_CONTACT)
        state = self.store.session(100)
        self.assertEqual(state['step'], 'okb_contact_name')
        self.assertEqual(state['phone'], '+998909876543')
        self.assertIn('Фамилия необязательна', self.telegram.messages[-1][1])
        self.assertNotIn('Использовать этот номер', str(self.telegram.messages[-1]))

    def test_address_offers_optional_location_without_landmark(self):
        self.to_location()
        self.assertEqual(self.store.session(100)['step'], 'okb_location_choice')
        self.say(SKIP_LOCATION)
        self.assertEqual(self.store.session(100)['step'], 'confirm')
        self.assertEqual(self.store.session(100)['landmark'], '')
        self.assertNotIn('ориентир', self.telegram.messages[-1][1].lower())

    def test_native_location_is_saved_only_after_confirmation(self):
        self.to_location()
        self.say(ADD_LOCATION)
        keyboard = self.telegram.messages[-1][2]
        self.assertTrue(any(isinstance(b, dict) and b.get('request_location') for row in keyboard for b in row))
        self.send_location({'latitude': 41.31, 'longitude': 69.28})
        state = self.store.session(100)
        self.assertEqual(state['step'], 'confirm')
        self.assertEqual(self.crm.items, {})
        self.say('Подтвердить')
        op = self.store.operation(state['request_id'])
        self.assertEqual(op['status'], 'succeeded')
        point = self.crm.records['pharmacy_locations'][str(op['result']['id'])]
        self.assertEqual(point, {'latitude': 41.31, 'longitude': 69.28})
        self.say('Подтвердить')
        self.assertEqual(len(self.crm.items), 1)

    def test_bad_or_out_of_order_location_keeps_current_step(self):
        self.say('/okb')
        self.send_location({'latitude': 41, 'longitude': 69})
        self.assertEqual(self.store.session(100)['step'], 'okb_inn')
        self.say('/cancel')
        self.to_location()
        self.send_location({'latitude': 91, 'longitude': 69})
        self.assertEqual(self.store.session(100)['step'], 'okb_location_choice')
        self.assertNotIn('location', self.store.session(100))

    def test_geo_bounds_reject_non_numbers_and_accept_zero(self):
        self.assertEqual(coordinates({'latitude': 0, 'longitude': 0}), {'latitude': 0.0, 'longitude': 0.0})
        for point in ({'latitude': True, 'longitude': 3}, {'latitude': float('nan'), 'longitude': 3},
                      {'latitude': 3, 'longitude': 181}, {'latitude': '41', 'longitude': 69}):
            with self.subTest(point=point), self.assertRaises(ValueError):
                coordinates(point)

    def test_single_message_reuses_existing_contact_without_position(self):
        self.say('/okb')
        data = payload(self.crm, phone='90 123 45 67', contact_name='Не создавать', create_contact=True)
        _, _, state = self.bot.route_payload(100, data, self.store.session(100))
        self.assertEqual(state['step'], 'confirm')
        self.assertEqual(state['contact']['id'], 301)
        self.assertFalse(state['create_contact'])
        self.assertEqual(len(self.crm.records['contacts']), 3)

    def test_single_message_new_contact_uses_consent_name_and_live_position(self):
        self.say('/okb')
        position = self.crm.contact_positions()[1]
        data = payload(self.crm, phone='90 987 65 43', contact_name='Имя', create_contact=True, position=position['id'])
        text, _, state = self.bot.route_payload(100, data, self.store.session(100))
        self.assertEqual(state['step'], 'confirm')
        self.assertEqual(state['contact_position'], position)
        self.assertIn('Фирма &lt;А&gt;', text)
        self.assertLess(text.index('🏪'), text.index('🏢'))
        self.assertLess(text.index('🏢'), text.index('👤'))
        self.assertEqual(self.crm.items, {})

    def test_single_message_asks_consent_for_unknown_phone_and_retains_fields(self):
        self.say('/okb')
        data = payload(self.crm, phone='90 987 65 43')
        _, _, state = self.bot.route_payload(100, data, self.store.session(100))
        self.assertEqual(state['step'], 'okb_contact_create')
        self.store.set_session(100, state)
        for text in (CREATE_CONTACT, 'Малика', '2'):
            self.say(text)
        state = self.store.session(100)
        self.assertEqual(state['step'], 'confirm')
        self.assertEqual(state['address'], data['address'])
        self.assertEqual(state['city']['id'], data['city'])

    def test_invalid_region_city_pair_is_rejected_before_any_write(self):
        data = payload(self.crm)
        data['city'] = self.crm.cities_for_region(self.crm.choices('business_region')[0]['id'])[0]['id']
        with self.assertRaises(ValueError):
            collect(data, self.crm, self.config)
        self.assertEqual(self.crm.records['companies'], {})

    def test_text_parser_rejects_typos_duplicates_and_accepts_uzbek_labels(self):
        with self.assertRaises(ValueError): parse_text('Телфн: 123')
        with self.assertRaises(ValueError): parse_text('ИНН: 111222333\nИНН: 123456789')
        with language_context('uz'):
            value = parse_text('ИНН: 111222333\nДорихона: Аптека\nИсм: Малика\nЭслатма: Қўнғироқ қилиш')
            self.assertEqual(value['title'], 'Аптека')
            self.assertTrue(value['create_contact'])
            self.assertTrue(value['reminder'])


class DealIntakeTests(unittest.TestCase):
    setUp, tearDown, say, message = live_fixture.LiveDealTests.setUp, live_fixture.LiveDealTests.tearDown, live_fixture.LiveDealTests.say, live_fixture.LiveDealTests.message
    use_offline_pharmacy_components = live_fixture.LiveDealTests.use_offline_pharmacy_components
    new_pharmacy_form = live_fixture.LiveDealTests.new_pharmacy_form

    def test_new_deal_and_stale_step_button_never_start_pharmacy_walk(self):
        self.legacy_entries = False
        self.say('/deal')
        original = deepcopy(self.store.session(100))
        self.assertEqual(original['step'], 'sales_bulk_input')
        self.say('Проверка связи')
        self.assertEqual(self.store.session(100), original)
        self.say(STEP_FORM)
        keyboard = self.telegram.messages[-1][2]
        self.assertNotIn(STEP_FORM, str(keyboard))
        self.assertNotIn(TEXT_FORM, str(keyboard))
        self.assertEqual(self.store.session(100), original)
        self.assertEqual(self.api.rows, {})

    def test_pharmacy_creation_permission_denial_preserves_contact_and_explains_required_right(self):
        crm = self.use_offline_pharmacy_components()
        state = self.new_pharmacy_form()
        def deny(*args): raise RemoteError('ACCESS_DENIED')
        crm.create_okb_pharmacy = deny
        self.say('Подтвердить')
        response = self.telegram.messages[-1][1]
        self.assertIn('право добавлять «Аптеки»', response)
        self.assertNotIn('ACCESS_DENIED', response)
        self.assertEqual(len(crm.records['companies']), 1)
        self.assertEqual(len(crm.records['requisites']), 1)
        self.assertEqual(self.api.rows, {})
        self.assertEqual(crm.items, {})
        self.assertEqual(self.store.operation(state['request_id'])['status'], 'created')

    def test_deal_without_description_or_reminder_creates_no_activity(self):
        for text in ('/deal', 'Проверка связи', USE_PHARMACY, USE_ADDRESS, NO_REMINDER, 'Без телефона'):
            self.say(text)
        state = self.store.session(100)
        self.assertEqual(state['step'], 'confirm')
        self.assertEqual(state['description'], '')
        self.assertFalse(state['reminder'])
        self.assertNotIn('Описание:', self.telegram.messages[-1][1])
        self.say('Подтвердить')
        self.assertEqual(self.api.activities, {})
        self.assertEqual(self.api.rows[501]['assignedById'], 132)
        self.assertEqual(self.store.operation(state['request_id'])['status'], 'succeeded')

    def test_calendar_selects_day_and_time_in_tashkent(self):
        now = datetime(2026, 10, 7, 10, 10, tzinfo=self.config.timezone)
        self.live.bot.clock = lambda: now
        for text in ('/deal', 'Проверка связи', USE_PHARMACY, USE_ADDRESS, ADD_REMINDER, 'Позвонить'):
            self.say(text)
        self.say(PREVIOUS_MONTH)
        self.assertEqual(self.store.session(100)['calendar_month'], '2026-10-01')
        self.say(NEXT_MONTH)
        self.assertEqual(self.store.session(100)['calendar_month'], '2026-11-01')
        self.say('10')
        self.assertEqual(self.store.session(100)['step'], 'sales_time')
        self.say('09:30')
        self.say('Без телефона')
        state = self.store.session(100)
        self.assertEqual(state['deadline'], '2026-11-10T09:30:00+05:00')
        self.say('Подтвердить')
        self.assertEqual(self.api.activities[901]['deadline'], state['deadline'])

    def test_today_offers_only_future_times(self):
        now = datetime(2026, 10, 7, 18, 10, tzinfo=self.config.timezone)
        self.live.bot.clock = lambda: now
        for text in ('/deal', 'Проверка связи', USE_PHARMACY, USE_ADDRESS, ADD_REMINDER, 'Позвонить', '7'):
            self.say(text)
        state = self.store.session(100)
        self.assertIn('18:30', state['reminder_times'])
        self.assertNotIn('18:00', state['reminder_times'])
        self.say('18:00')
        self.assertEqual(self.store.session(100)['step'], 'sales_time')
        self.assertEqual(self.api.rows, {})

    def web_data(self, data, **changes):
        update = self.message('')
        update['message'].pop('text')
        update['message']['web_app_data'] = {'data': json.dumps(data, ensure_ascii=False)}
        update['message'].update(changes)
        self.live.handle(update)
        return update

    def test_web_form_creates_preview_and_confirmation_is_idempotent(self):
        self.legacy_entries = False
        crm = self.use_offline_pharmacy_components()
        self.say('/deal')
        data = payload(crm, token=self.store.session(100)['request_id'], location={'latitude': 41.3, 'longitude': 69.2})
        update = self.web_data(data)
        state = self.store.session(100)
        self.assertEqual(state['step'], 'confirm')
        self.assertEqual(self.api.rows, {})
        self.assertEqual(crm.items, {})
        self.live.handle(update)
        self.say('Подтвердить')
        self.say('Подтвердить')
        self.assertEqual(len(self.api.rows), 1)
        self.assertEqual(len(crm.items), 1)
        self.assertEqual(self.api.activities, {})
        self.assertEqual(len(crm.records['companies']), 1)

    def test_web_form_missing_consent_resumes_contact_without_losing_reminder(self):
        crm = self.use_offline_pharmacy_components()
        self.say('/deal')
        due = (self.live.bot.clock() + timedelta(days=2)).replace(microsecond=0, tzinfo=None).isoformat()
        data = payload(crm, token=self.store.session(100)['request_id'], phone='90 987 65 43',
                       reminder=True, next_step='Позвонить', deadline=due)
        self.web_data(data)
        self.assertEqual(self.store.session(100)['pharmacy_form']['step'], 'okb_contact_create')
        for text in (CREATE_CONTACT, 'Имя', '1'):
            self.say(text)
        state = self.store.session(100)
        self.assertEqual(state['step'], 'confirm')
        self.assertEqual(state['next_step'], 'Позвонить')
        self.say('Подтвердить')
        self.assertEqual(len(self.api.activities), 1)

    def test_web_form_unknown_company_asks_only_name_and_keeps_other_values(self):
        crm = self.use_offline_pharmacy_components()
        self.say('/deal')
        data = payload(crm, token=self.store.session(100)['request_id'], company_name='')
        self.web_data(data)
        child = self.store.session(100)['pharmacy_form']
        self.assertEqual(child['step'], 'okb_company_name')
        self.assertEqual(child['title'], data['title'])
        self.assertEqual(child['address'], data['address'])
        self.say('Новая фирма')
        state = self.store.session(100)
        self.assertEqual(state['step'], 'confirm')
        self.assertEqual(state['pharmacy_form']['company_name'], 'Новая фирма')
        self.assertEqual(self.api.rows, {})

    def test_stale_malformed_oversized_or_out_of_order_web_form_is_rejected(self):
        crm = self.use_offline_pharmacy_components()
        self.say('/deal')
        original = deepcopy(self.store.session(100))
        for invalid in ({'token': 'old'}, {'token': original['request_id'], 'title': 'я' * 5000}):
            self.web_data(invalid)
            self.assertEqual(self.store.session(100), original)
        self.web_data({}, web_app_data=3)
        self.assertEqual(self.store.session(100), original)
        self.say('Проверка связи')
        self.web_data(payload(crm, token=original['request_id']))
        self.assertEqual(self.store.session(100)['step'], 'sales_pharmacy_confirm')
        self.assertEqual(self.api.rows, {})

    def test_removed_one_message_route_keeps_form_and_creates_nothing(self):
        self.legacy_entries = False
        crm = self.use_offline_pharmacy_components()
        data = payload(crm)
        self.say('/deal')
        self.say(TEXT_FORM)
        self.say('\n'.join(['ИНН: '+data['inn'], 'Фирма: '+data['company_name'], 'Аптека: '+data['title'],
            'Регион: '+data['business_region'], 'Город/район: '+data['city'], 'Адрес: '+data['address'], 'Программа: 1С']))
        self.assertEqual(self.store.session(100)['step'], 'sales_bulk_input')
        self.assertEqual(self.store.session(100)['step'], 'sales_bulk_input')
        self.assertEqual(self.api.rows, {})


class RequisiteLengthTests(unittest.TestCase):
    def test_short_ids_unchanged_and_long_id_is_stable_and_within_bitrix_limit(self):
        calls = []
        class Api:
            def call(self, method, payload):
                calls.append((method, payload))
                return {'result': 77}
        from datfo_crm_bot.demo import demo_config
        crm = OkbCrm(demo_config(Path('unused.sqlite3')), Api(), OkbSettings.load())
        for ident in ('short-request', 'datfo-live-20f7c6a956ea43ac9ee3cb8fd8bb770f-pharmacy',
                      'datfo-live-20f7c6a956ea43ac9ee3cb8fd8bb770f-pharmacy'):
            crm.create_requisite({'request_id': ident, 'inn': '304347078'}, {'id': 86017, 'title': 'Фирма'})
        self.assertEqual(calls[0][1]['fields']['XML_ID'], 'short-request')
        long = calls[1][1]['fields']
        self.assertLessEqual(len(long['XML_ID']), 45)
        self.assertEqual(long['XML_ID'], calls[2][1]['fields']['XML_ID'])
        self.assertEqual((long['ENTITY_ID'], long['RQ_INN']), (86017, '304347078'))


if __name__ == '__main__':
    unittest.main()
