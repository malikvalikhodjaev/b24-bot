from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from datfo_crm_bot.api import RemoteError
from datfo_crm_bot.config import User, ConfigError
from datfo_crm_bot.demo import demo_config
from datfo_crm_bot.okb_crm import OkbCrm, OkbSettings
from datfo_crm_bot.okb_service import CONTINUE, OKB, OkbBot, CREATE_CONTACT, NO_CONTACT, USE_CONTACT_PHONE, BULK_UPLOAD_URL
from datfo_crm_bot.simulation import SimulationCrm
from datfo_crm_bot.storage import Store
from datfo_crm_bot.i18n import language_context


class Telegram:
    def __init__(self):
        self.messages = []
        self.fail = False

    def send(self, chat_id, text, keyboard=None):
        if self.fail and not text.startswith('⏳ Подождите') and not text.startswith('⏳ Кутинг'):
            self.fail = False
            raise RemoteError("CONNECTION_ERROR", uncertain=True)
        self.messages.append((chat_id, text, keyboard))


class OkbTests(unittest.TestCase):
    # These write/recovery scenarios exercise persisted drafts from the earlier UI.
    legacy_entries = True
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.folder.name) / "okb.sqlite3")
        self.telegram = Telegram()
        self.config = replace(demo_config(Path(self.folder.name) / "okb.sqlite3"), users={100: User(132, True, "Малик")})
        self.crm = SimulationCrm(self.store)
        self.bot = OkbBot(self.config, self.store, self.crm, self.telegram)
        self.number = 5000

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def say(self, text, user_id=100):
        self.number += 1
        update = {"update_id": self.number, "message": {"from": {"id": user_id}, "chat": {"id": user_id, "type": "private"}, "text": text}}
        self.bot.handle(update)
        if text in {'/okb', OKB} and getattr(self, 'legacy_entries', True):
            state = self.store.session(user_id)
            if state and state.get('step') == 'okb_bulk_input':
                self.store.set_session(user_id, {**state, 'step': 'okb_inn'})
        return update

    def form(self, *, new=False, phone="+998909876543", contact_choice="1", title="Тестовая аптека", company_choice="1"):
        self.say("/okb")
        self.say("111222333" if new else "123456789")
        self.say("Учебная фирма" if new else company_choice)
        self.say(title)
        self.say(phone or "Без телефона")
        if self.store.session(100)['step'] == 'okb_contact_create':
            for text in (CREATE_CONTACT, 'Малика', '1'):
                self.say(text)
        if self.store.session(100)["step"] == "okb_contact":
            self.say(contact_choice)
        for text in ("3", "МИРАБАДСКИЙ", "1", "Учебная улица, 1", "Пропустить локацию"):
            self.say(text)

    def test_inn_is_mandatory_and_no_creation_before_confirmation(self):
        self.say(OKB)
        self.say("Без ИНН")
        self.assertEqual(self.store.session(100)["step"], "okb_inn")
        self.say("/cancel")
        self.form(new=True)
        self.assertEqual(self.crm.records["companies"], {})
        self.assertEqual(len(self.crm.records["contacts"]), 3)
        self.assertEqual(self.crm.items, {})
        self.assertIn("Учебная фирма", self.telegram.messages[-1][1])
        self.assertIn("Потенциальная", self.telegram.messages[-1][1])

    def contact_question(self):
        for text in ('/okb','111222333','Новая фирма','Новая аптека','90 987 65 43'):
            self.say(text)

    def test_contact_creation_requires_consent_name_and_live_position_before_any_write(self):
        self.contact_question()
        self.assertEqual(self.store.session(100)['step'],'okb_contact_create')
        self.say('возможно')
        self.assertEqual(self.store.session(100)['step'],'okb_contact_create')
        self.say(CREATE_CONTACT)
        self.assertEqual(self.store.session(100)['step'],'okb_contact_name')
        self.say('Малика <А>')
        self.assertEqual(self.store.session(100)['step'],'okb_contact_position')
        self.say('Неизвестная должность')
        self.assertEqual(self.store.session(100)['step'],'okb_contact_position')
        self.say('3')
        self.assertEqual(self.store.session(100)['contact_name'],'Малика <А>')
        self.assertEqual(self.store.session(100)['contact_position']['title'],'Фармацевт')
        self.assertEqual(self.crm.records['companies'],{})
        self.assertEqual(len(self.crm.records['contacts']),3)
        self.assertEqual(self.crm.items,{})

    def test_contact_can_be_declined_or_changed_to_existing_number_without_duplication(self):
        self.contact_question()
        self.say(NO_CONTACT)
        self.assertEqual(self.store.session(100)['phone'],'')
        self.assertEqual(self.store.session(100)['step'],'okb_region')
        self.say('/cancel')
        self.contact_question()
        self.say(CREATE_CONTACT)
        legacy = self.store.session(100)
        legacy['step'] = 'okb_contact_phone'
        self.store.set_session(100, legacy)
        self.say('90 123 45 67')
        self.assertEqual(self.store.session(100)['step'],'okb_contact')
        self.say('1')
        self.assertEqual(self.store.session(100)['contact']['id'],301)
        self.assertFalse(self.store.session(100)['create_contact'])
        self.assertEqual(len(self.crm.records['contacts']),3)

    def test_success_notices_follow_confirmed_creation_and_reused_contact_has_no_new_contact_notice(self):
        self.form(new=True)
        self.assertNotIn('загрузчик',self.telegram.messages[-1][1])
        self.say('Подтвердить')
        result=self.store.operation(self.store.session(100)['request_id'])['result']
        row=self.crm.records['contacts'][str(result['contact_id'])]
        self.assertEqual(row['title'],'Малика')
        self.assertEqual(row['position']['title'],'Владелец')
        self.assertEqual(row['manager_id'],132)
        self.assertTrue(result['contact_created'])
        message=self.telegram.messages[-1][1]
        self.assertIn('Контакт тоже добавлен.',message)
        self.assertEqual(message.count('Спасибо'), 1)
        self.assertIn('<blockquote>А также, если хотите,',message)
        self.assertIn('>контакты</a>',message)
        self.assertIn('>аптеки</a>',message)
        self.assertIn(BULK_UPLOAD_URL.replace('&','&amp;'),message)
        self.form(phone='+998901234567',title='Другая аптека')
        self.say('Подтвердить')
        self.assertNotIn('Контакт тоже добавлен',self.telegram.messages[-1][1])

    def test_removed_contact_position_blocks_all_mutations_and_partial_failure_has_no_success_notice(self):
        self.form(new=True)
        self.crm.contact_positions=lambda fresh=False: []
        self.say('Подтвердить')
        self.assertEqual(self.crm.records['companies'],{})
        self.assertEqual(len(self.crm.records['contacts']),3)
        self.assertNotIn('массово загрузить',self.telegram.messages[-1][1])

    def test_contact_prompts_and_success_use_selected_language_without_translating_the_name(self):
        for language,question,thanks in (('ru','Создать новый контакт?','Контакт тоже добавлен.'),
                                         ('uz','Янги контакт яратайликми?','Контакт ҳам қўшилди.')):
            with language_context(language):
                self.say('/cancel')
                for text in ('/okb','123456789','1','Аптека '+language,'90 222 22 2'+str(len(self.crm.items))):
                    self.say(text)
                self.assertIn(question,self.telegram.messages[-1][1])
                for text in ('ҳа','Малика <А>','1','3','МИРАБАДСКИЙ','1','Улица 1','Пропустить локацию','Подтвердить'):
                    self.say(text)
                self.assertIn(thanks,self.telegram.messages[-1][1])
                result=self.store.operation(self.store.session(100)['request_id'])['result']
                self.assertEqual(self.crm.records['contacts'][str(result['contact_id'])]['title'],'Малика <А>')

    def test_current_program_must_be_selected_and_is_persisted_before_confirmation(self):
        target = self.config.targets['pharmacy']
        self.config = replace(self.config,targets={**self.config.targets,
            'pharmacy':replace(target,fields={**target.fields,'current_program':'UF_CRM_8_CURRENT_PROGRAM'})})
        self.bot.config = self.config
        original = self.crm.choices
        self.crm.choices = lambda key: [{'id':'5919','title':'1С'},{'id':'5920','title':'Excel'}] if key=='current_program' else original(key)
        self.form(new=True)
        self.assertEqual(self.store.session(100)['step'],'okb_program')
        self.assertEqual(self.crm.items,{})
        self.say('Подтвердить')
        self.assertEqual(self.store.session(100)['step'],'okb_program')
        self.say('2')
        state = self.store.session(100)
        self.assertEqual(state['current_program'],{'id':'5920','title':'Excel'})
        self.assertEqual(self.store.operation(state['request_id'])['value']['current_program']['id'],'5920')
        self.assertIn('Текущая программа: Excel',self.telegram.messages[-1][1])

    def test_new_company_requisite_contact_and_pharmacy_created_together(self):
        self.form(new=True)
        self.say("Подтвердить")
        self.assertEqual(len(self.crm.records["companies"]), 1)
        self.assertEqual(len(self.crm.records["requisites"]), 1)
        self.assertEqual(len(self.crm.records["contacts"]), 4)
        self.assertEqual(len(self.crm.items), 1)
        result = self.store.operation(self.store.session(100)["request_id"])["result"]
        self.assertEqual(result["company_id"], 200)
        self.assertIn([result["contact_id"], 200], self.crm.records["bindings"])

    def test_existing_company_and_contact_reused_without_overwriting_links(self):
        self.form(phone="+998901234567")
        self.say("Подтвердить")
        self.assertEqual(len(self.crm.records["contacts"]), 3)
        self.assertEqual(self.crm.records["companies"], {})
        self.assertEqual(self.crm.records["requisites"], {})
        self.assertIn([301, 101], self.crm.records["bindings"])
        self.assertEqual(self.store.operation(self.store.session(100)["request_id"])["result"]["contact_id"], 301)

    def test_duplicate_contacts_require_explicit_choice(self):
        self.form(phone="+998901111111", contact_choice="2")
        self.say("Подтвердить")
        self.assertEqual(self.store.operation(self.store.session(100)["request_id"])["result"]["contact_id"], 303)
        self.assertEqual(len(self.crm.records["contacts"]), 3)

    def test_existing_pharmacy_gets_additional_contact_and_keeps_old_contacts(self):
        self.form()
        self.say("Подтвердить")
        result = self.store.operation(self.store.session(100)["request_id"])["result"]
        old_contact = result["contact_id"]
        self.form(phone="+998901234567")
        self.assertIn("уже существует", self.telegram.messages[-1][1])
        self.say("Подтвердить")
        self.assertEqual(len(self.crm.items), 1)
        self.assertEqual(self.crm.records["pharmacy_contacts"][str(result["id"])], [old_contact, 301])
        self.assertEqual(self.store.statistics("0000", "9999", 100), {"pharmacy": 1})

    def test_city_must_come_from_dictionary_and_landmark_is_optional(self):
        for text in ("/okb", "123456789", "1", "Название", "Без телефона", "3", "НетТакогоГорода"):
            self.say(text)
        self.assertEqual(self.store.session(100)["step"], "okb_city_choice")
        for text in ("МИРАБАДСКИЙ", "1", "Улица 1", "Пропустить локацию", "Подтвердить"):
            self.say(text)
        self.assertEqual(self.store.session(100)["landmark"], "")
        self.assertEqual(self.store.session(100)["city"]["title"].casefold(), "МИРАБАДСКИЙ РАЙОН".casefold())

    def test_named_region_buttons_show_only_linked_cities_and_can_be_selected(self):
        for text in ('/okb', '123456789', '1', 'Аптека', 'Без телефона'):
            self.say(text)
        buttons = self.telegram.messages[-1][2]
        self.assertIn(['3. Город Ташкент'], buttons)
        self.say('3. Город Ташкент')
        state = self.store.session(100)
        self.assertEqual(state['step'], 'okb_city_choice')
        self.assertEqual(len(state['city_candidates']), 12)
        self.assertTrue(all(4601 <= int(city['id']) <= 4612 for city in state['city_candidates']))
        self.say('3. Мирабадский район')
        self.assertEqual(self.store.session(100)['city']['id'], '4603')
        self.assertEqual(self.store.session(100)['step'], 'okb_address')

    def test_search_cannot_offer_city_from_other_region_even_in_old_draft(self):
        for text in ('/okb', '123456789', '1', 'Аптека', 'Без телефона', '3'):
            self.say(text)
        self.say('Андижан')
        self.assertEqual(self.store.session(100)['step'], 'okb_city_choice')
        self.assertNotIn('4572', {city['id'] for city in self.store.session(100)['city_candidates']})
        state = self.store.session(100)
        state['step'] = 'okb_city_search'
        self.store.set_session(100, state)
        self.say('Андижан')
        self.assertEqual(self.store.session(100)['step'], 'okb_city_search')
        self.assertEqual(self.crm.items, {})

    def test_stale_global_city_buttons_are_replaced_with_region_choices(self):
        for text in ('/okb', '123456789', '1', 'Аптека', 'Без телефона', '3'):
            self.say(text)
        state = self.store.session(100)
        state['city_candidates'] = [{'id': '4572', 'title': 'Андижан'}]
        self.store.set_session(100, state)
        self.say('1')
        current = self.store.session(100)
        self.assertEqual(current['step'], 'okb_city_choice')
        self.assertNotIn('city', current)
        self.assertEqual(len(current['city_candidates']), 12)

    def test_region_city_mismatch_is_rejected_before_creating_company(self):
        self.form(new=True)
        state = self.store.session(100)
        state['city'] = {'id': '4572', 'title': 'Андижан'}
        with self.store.db:
            self.store.db.execute('UPDATE operations SET value=? WHERE request_id=?',
                (json.dumps(state), state['request_id']))
        self.say('Подтвердить')
        self.assertEqual(self.crm.records['companies'], {})
        self.assertEqual(self.crm.records['requisites'], {})
        self.assertEqual(self.crm.items, {})
        self.assertIn('Данные или поля Б24 изменились', self.telegram.messages[-1][1])

    def test_replayed_confirmation_does_not_duplicate_any_component(self):
        self.form(new=True)
        update = self.say("Подтвердить")
        self.bot.handle(update)
        self.say("Подтвердить")
        self.assertEqual(len(self.crm.items), 1)
        self.assertEqual(len(self.crm.records["companies"]), 1)
        self.assertEqual(len(self.crm.records["requisites"]), 1)
        self.assertEqual(len(self.crm.records["contacts"]), 4)

    def test_lost_final_telegram_reply_does_not_repeat_crm_operations(self):
        self.form(new=True)
        self.telegram.fail = True
        self.number += 1
        update = {"update_id": self.number, "message": {"from": {"id": 100}, "chat": {"id": 100, "type": "private"}, "text": "Подтвердить"}}
        with self.assertRaises(RemoteError):
            self.bot.handle(update)
        self.bot.handle(update)
        self.assertEqual(len(self.crm.items), 1)
        self.assertEqual(len(self.crm.records["companies"]), 1)

    def test_partial_company_is_recovered_after_restart(self):
        self.form(new=True)
        original = self.crm.create_company
        def lose(state, manager_id):
            original(state, manager_id)
            raise RemoteError("CONNECTION_ERROR", uncertain=True)
        self.crm.create_company = lose
        self.say("Подтвердить")
        self.assertEqual(len(self.crm.records["companies"]), 1)
        self.crm = SimulationCrm(self.store)
        self.bot = OkbBot(self.config, self.store, self.crm, self.telegram)
        self.say("/pending")
        self.say(CONTINUE)
        self.assertEqual(len(self.crm.records["companies"]), 1)
        self.assertEqual(len(self.crm.items), 1)

    def test_uncertain_unsaved_write_is_never_reposted(self):
        self.form(new=True)
        attempts = []
        def lose(state, manager_id):
            attempts.append(1)
            raise RemoteError("CONNECTION_ERROR", uncertain=True)
        self.crm.create_company = lose
        self.say("Подтвердить")
        self.say("/pending")
        self.say(CONTINUE)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.crm.records["companies"], {})
        self.assertIn("не подтверждён", self.telegram.messages[-1][1])

    def test_explicit_rejection_can_be_retried_without_repeating_succeeded_steps(self):
        self.form(new=True)
        original = self.crm.create_contact
        def reject(state, manager_id):
            raise RemoteError("ACCESS_DENIED")
        self.crm.create_contact = reject
        self.say("Подтвердить")
        self.assertEqual(len(self.crm.records["companies"]), 1)
        self.crm.create_contact = original
        self.say(CONTINUE)
        self.assertEqual(len(self.crm.records["companies"]), 1)
        self.assertEqual(len(self.crm.records["requisites"]), 1)
        self.assertEqual(len(self.crm.items), 1)

    def test_new_phone_ambiguity_detected_before_company_creation(self):
        self.form(new=True)
        for number in (2001, 2002):
            self.crm.records["contacts"][str(number)] = {"id": number, "title": "Новый дубль", "phone": "+998909876543"}
        self.say("Подтвердить")
        self.assertEqual(self.crm.records["companies"], {})
        self.assertEqual(self.crm.items, {})

    def test_company_appearing_after_preview_requires_new_selection(self):
        self.form(new=True)
        self.crm.records["companies"]["external"] = {"id": 500, "title": "Другой контрагент", "inn": "111222333"}
        self.say("Подтвердить")
        self.assertEqual(len(self.crm.records["companies"]), 1)
        self.assertEqual(self.crm.items, {})

    def test_loss_after_requisite_or_contact_link_or_pharmacy_recovers_only_existing_stage(self):
        for name in ("create_requisite", "create_contact", "add_contact_company", "create_okb_pharmacy"):
            with self.subTest(name=name):
                self.store.close()
                self.store = Store(Path(self.folder.name) / (name + ".sqlite3"))
                self.crm = SimulationCrm(self.store)
                self.bot = OkbBot(self.config, self.store, self.crm, self.telegram)
                self.form(new=True, title=name)
                original = getattr(self.crm, name)
                attempts = []
                def lose(*args):
                    attempts.append(1)
                    original(*args)
                    raise RemoteError("CONNECTION_ERROR", uncertain=True)
                setattr(self.crm, name, lose)
                self.say("Подтвердить")
                setattr(self.crm, name, original)
                self.say("/pending")
                self.say(CONTINUE)
                self.assertEqual(len(attempts), 1)
                self.assertEqual(self.store.operation(self.store.session(100)["request_id"])["status"], "succeeded")

    def test_unknown_user_cannot_start_okb(self):
        self.say("/okb", user_id=900)
        self.assertIsNone(self.store.session(900))
        self.assertEqual(self.crm.records["companies"], {})

    def test_partial_company_request_blocks_another_manager_with_same_inn(self):
        self.form(new=True)
        attempts = []
        def lose(state, manager_id):
            attempts.append(1)
            raise RemoteError("CONNECTION_ERROR", uncertain=True)
        self.crm.create_company = lose
        self.say("Подтвердить")
        self.config.users[200] = User(150, False, "Другой менеджер")
        for text in ("/okb", "111222333", "Учебная фирма", "Другая аптека", "Без телефона", "3", "МИРАБАДСКИЙ", "1", "Улица 2", "Пропустить локацию", "Подтвердить"):
            self.say(text, user_id=200)
        self.assertEqual(len(attempts), 1)
        self.assertIn("другом запросе", self.telegram.messages[-1][1])

    def test_first_write_explicitly_denied_can_be_cancelled(self):
        self.form(new=True)
        def reject(state, manager_id):
            raise RemoteError("ACCESS_DENIED")
        self.crm.create_company = reject
        self.say("Подтвердить")
        self.assertEqual(self.store.unfinished(100), [])
        self.say("/cancel")
        self.assertIsNone(self.store.session(100))
        self.assertEqual(self.crm.records["companies"], {})


class Api:
    def __init__(self):
        self.calls = []
        self.contacts = []
        self.duplicate_ids = []
        self.contact_ids = [11, 22]

    def call(self, method, payload=None):
        self.calls.append((method, payload))
        if method in {"crm.company.add", "crm.requisite.add", "crm.contact.add"}:
            return {"result": 100}
        if method == "crm.item.add":
            return {"result": {"item": {"id": 200}}}
        if method == "crm.duplicate.findbycomm":
            return {"result": getattr(self,'duplicate_response',{"CONTACT": self.duplicate_ids})}
        if method == 'crm.contact.fields':
            return {'result':{'UF_POSITION':{'type':'enumeration','isMultiple':False,'isReadOnly':False,
                'formLabel':'Должность список','items':[{'ID':'3265','VALUE':'Владелец'}]}}}
        if method == "crm.contact.company.items.get":
            return {"result": [{"COMPANY_ID": 90, "IS_PRIMARY": "Y"}]}
        if method == "crm.contact.company.add":
            return {"result": True}
        if method == "crm.item.get":
            return {"result": {"item": {"id": 200, "contactIds": self.contact_ids}}}
        if method == "crm.item.update":
            self.contact_ids = payload["fields"]["contactIds"]
            return {"result": {"item": {"id": 200, "contactIds": self.contact_ids}}}
        raise AssertionError(method)

    def list_all(self, method, payload):
        self.calls.append((method, payload))
        if method == "crm.contact.list":
            return self.contacts
        raise AssertionError(method)


class PayloadTests(unittest.TestCase):
    def setUp(self):
        self.settings = OkbSettings.load()
        self.config = replace(demo_config(Path("unused.sqlite3")), targets={"pharmacy": self.settings.pharmacy})
        self.api = Api()
        self.crm = OkbCrm(self.config, self.api, self.settings)
        self.state = {"request_id": "unique-okb-request", "inn": "123456789", "title": "Аптека", "company_name": "Фирма",
            "phone": "+998901234567", "address": "Улица 1", "landmark": "У рынка", "business_region": {"id": "4559"}, "city": {"id": "4770"},
            'current_program':{'id':'5919','title':'1С'}}

    def test_pharmacy_payload_uses_verified_fields_and_omits_phone_and_created_by(self):
        self.crm.create_okb_pharmacy(self.state, {"id": 70}, 132, {"id": 99})
        fields = self.api.calls[-1][1]["fields"]
        self.assertEqual(fields["companyId"], 70)
        self.assertEqual(fields["assignedById"], 132)
        self.assertEqual(fields["UF_CRM_8_1735795380"], [132])
        self.assertEqual(fields["UF_CRM_8_1770098187583"], "4559")
        self.assertEqual(fields["UF_CRM_8_1772692252313"], "4798")
        self.assertEqual(fields["stageId"], "DT1034_17:UC_EF7EB7")
        self.assertEqual(fields["contactIds"], [99])
        self.assertEqual(fields['UF_CRM_8_CURRENT_PROGRAM'],'5919')
        self.assertNotIn("UF_CRM_8_1719398563749", fields)
        self.assertNotIn("createdBy", fields)

    def test_missing_program_cannot_send_create_payload(self):
        del self.state['current_program']
        with self.assertRaises(ConfigError):
            self.crm.create_okb_pharmacy(self.state,{'id':70},132,None)
        self.assertEqual(self.api.calls,[])

    def test_removed_program_is_detected_by_fresh_validation_before_writes(self):
        self.crm.validate = lambda kind: None
        self.crm.pharmacy_fields = lambda fresh=False: {}
        values = {'business_region':'4559','city':'4770','status':'4798','current_program':'9999'}
        self.crm.choices = lambda key: [{'id':values[key],'title':key}]
        with self.assertRaises(ConfigError):
            self.crm.validate_okb(self.state)
        self.assertEqual(self.api.calls,[])

    def test_new_company_does_not_infer_legal_address_or_phone_from_pharmacy(self):
        self.crm.create_company(self.state, 132)
        fields = self.api.calls[-1][1]["fields"]
        self.assertEqual(fields["TITLE"], "Фирма")
        self.assertNotIn("PHONE", fields)
        self.assertNotIn("ADDRESS", fields)
        self.assertEqual(fields["ORIGIN_ID"], "unique-okb-request")

    def test_requisite_contains_mandatory_inn_and_uz_template(self):
        self.crm.create_requisite(self.state, {"id": 70, "title": "Фирма"})
        fields = self.api.calls[-1][1]["fields"]
        self.assertEqual(fields["ENTITY_ID"], 70)
        self.assertEqual(fields["ENTITY_TYPE_ID"], 4)
        self.assertEqual(fields["PRESET_ID"], 4)
        self.assertEqual(fields["RQ_INN"], "123456789")

    def test_contact_is_added_as_secondary_company_binding_without_replacing_existing(self):
        self.assertIsNone(self.crm.contact_company_link(99, 70))
        self.crm.add_contact_company(99, 70)
        fields = self.api.calls[-1][1]["fields"]
        self.assertEqual(fields, {"COMPANY_ID": 70, "IS_PRIMARY": "N"})

    def test_adding_contact_to_existing_pharmacy_preserves_current_contact_ids(self):
        self.crm.add_pharmacy_contact(200, 33)
        self.assertEqual(self.api.contact_ids, [11, 22, 33])

    def test_contact_search_checks_normalised_phone_and_rejects_incomplete_lookup(self):
        self.api.duplicate_ids = [99]
        self.api.contacts = [{"ID": "99", "NAME": "Контакт", "PHONE": [{"VALUE": "+998 (90) 123-45-67"}]}]
        self.assertEqual(self.crm.contacts("+998901234567"), [{"id": 99, "title": "Контакт"}])
        self.api.contacts = []
        with self.assertRaises(RemoteError):
            self.crm.contacts("+998901234567")

    def test_empty_duplicate_array_is_no_match_but_other_wrong_shapes_are_errors(self):
        for response in ([],{}, {'CONTACT':[]}):
            self.api.duplicate_response=response
            self.assertEqual(self.crm.contacts('+998901234567'),[])
        for response in ([99],None,False,{'CONTACT':'99'}):
            self.api.duplicate_response=response
            with self.assertRaises(RemoteError):
                self.crm.contacts('+998901234567')
        self.assertFalse(any(method=='crm.contact.list' for method,_ in self.api.calls))

    def test_contact_payload_uses_chosen_name_position_phone_and_responsible(self):
        state={**self.state,'create_contact':True,'contact_name':'Малика',
               'contact_position':self.crm.contact_positions()[0]}
        self.crm.create_contact(state,132)
        fields=self.api.calls[-1][1]['fields']
        self.assertEqual(fields['NAME'],'Малика')
        self.assertEqual(fields['UF_POSITION'],'3265')
        self.assertEqual(fields['PHONE'][0]['VALUE'],state['phone'])
        self.assertEqual(fields['ASSIGNED_BY_ID'],132)
        self.assertEqual(fields['ORIGIN_ID'],state['request_id'])

    def test_unapproved_or_removed_contact_position_cannot_create_contact(self):
        state={**self.state,'contact_name':'Малика','contact_position':self.crm.contact_positions()[0]}
        with self.assertRaises(ConfigError):
            self.crm.create_contact(state,132)
        state.update(create_contact=True,contact_position={**state['contact_position'],'id':'9999'})
        with self.assertRaises(ConfigError):
            self.crm.create_contact(state,132)
        self.assertFalse(any(method=='crm.contact.add' for method,_ in self.api.calls))


if __name__ == "__main__":
    unittest.main()
