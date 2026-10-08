from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from datfo_crm_bot.api import Bitrix, RemoteError
from datfo_crm_bot.config import Config, ConfigError, Target
from datfo_crm_bot.crm import Crm
from datfo_crm_bot.demo import demo_config
from datfo_crm_bot.storage import process_lock


class FakeBitrix:
    def __init__(self, config):
        self.config = config
        self.calls = []
        self.requisites = [{"ENTITY_ID": "101", "ENTITY_TYPE_ID": "4", "RQ_INN": "123456789"},
                           {"ENTITY_ID": "101", "ENTITY_TYPE_ID": "4", "RQ_INN": "123456789"},
                           {"ENTITY_ID": "999", "ENTITY_TYPE_ID": "3", "RQ_INN": "123456789"},
                           {"ENTITY_ID": "777", "ENTITY_TYPE_ID": "4", "RQ_INN": "000000000"}]
        self.company_rows = [{"ID": "101", "TITLE": "Учебная компания"}]
        self.items = []
        self.missing_field = None
        self.bad_create_response = False

    def call(self, method, payload=None):
        payload = payload or {}
        self.calls.append((method, payload))
        if method == "crm.item.fields":
            target = next(t for t in self.config.targets.values() if t.entity_type_id == payload["entityTypeId"])
            names = set(target.fields.values()) | set(target.defaults) | {"categoryId"}
            return {"result": {"fields": {name: {"isReadOnly": False} for name in names if name != self.missing_field}}}
        if method == "crm.category.list":
            return {"result": {"categories": [{"id": 0}]}}
        if method == "crm.item.add":
            return {"result": {}} if self.bad_create_response else {"result": {"item": {"id": 25}}}
        if method == "crm.timeline.comment.add":
            return {"result": 90}
        raise AssertionError("Unexpected method " + method)

    def list_all(self, method, payload, key=None):
        self.calls.append((method, payload))
        if method == "crm.requisite.list":
            return self.requisites
        if method == "crm.company.list":
            return self.company_rows
        if method == "crm.item.list":
            return self.items
        raise AssertionError("Unexpected method " + method)


class CrmTests(unittest.TestCase):
    def setUp(self):
        self.config = demo_config(Path("unused.sqlite3"))
        self.api = FakeBitrix(self.config)
        self.crm = Crm(self.config, self.api)
        self.state = {"kind": "pharmacy", "request_id": "datfo-request-id", "title": "Точка № 2", "address": "Ташкент, улица, 2",
                      "phone": "+998901234567", "inn": "123456789", "company": {"id": 101, "title": "Учебная компания"}}

    def test_requisite_lookup_exact_inn_company_only_and_unique(self):
        self.assertEqual(self.crm.companies("123456789"), [{"id": 101, "title": "Учебная компания"}])
        company_call = next(p for m, p in self.api.calls if m == "crm.company.list")
        self.assertEqual(company_call["filter"]["@ID"], [101])

    def test_incomplete_company_lookup_is_error_not_missing_company(self):
        self.api.company_rows = []
        with self.assertRaisesRegex(RemoteError, "COMPANY_LOOKUP_INCOMPLETE"):
            self.crm.companies("123456789")

    def test_no_requisites_returns_no_company_without_second_request(self):
        self.api.requisites = []
        self.assertEqual(self.crm.companies("123456789"), [])
        self.assertEqual(len(self.api.calls), 1)

    def test_pharmacy_fields_and_real_company_link(self):
        self.crm.create(self.state, 10, 1)
        payload = next(p for m, p in self.api.calls if m == "crm.item.add")
        self.assertEqual(payload["entityTypeId"], 1000)
        self.assertEqual(payload["fields"]["companyId"], 101)
        self.assertEqual(payload["fields"]["ufAddress"], self.state["address"])
        self.assertEqual(payload["fields"]["xmlId"], self.state["request_id"])
        self.assertEqual(payload["fields"]["assignedById"], 10)
        self.assertIn(self.state["inn"], payload["fields"]["comments"])
        self.assertIn(self.state["phone"], payload["fields"]["comments"])

    def test_unbound_inn_preserved_without_company_id(self):
        self.state["company"] = None
        self.crm.create(self.state, 10, 1)
        fields = next(p for m, p in self.api.calls if m == "crm.item.add")["fields"]
        self.assertNotIn("companyId", fields)
        self.assertIn("123456789", fields["comments"])

    def test_support_routes_to_configured_specialist(self):
        target = replace(self.config.targets["support"], responsible_id=50)
        self.config = replace(self.config, targets={**self.config.targets, "support": target})
        self.api.config = self.config
        self.crm = Crm(self.config, self.api)
        self.state.update(kind="support", description="Ошибка входа")
        self.crm.create(self.state, 10, 1)
        fields = next(p for m, p in self.api.calls if m == "crm.item.add")["fields"]
        self.assertEqual(fields["assignedById"], 50)
        self.assertIn("сотрудник Б24 10", fields["comments"])

    def test_missing_field_prevents_mutation(self):
        self.api.missing_field = "companyId"
        with self.assertRaises(ConfigError):
            self.crm.create(self.state, 10, 1)
        self.assertFalse(any(m == "crm.item.add" for m, p in self.api.calls))

    def test_schema_change_after_validation_prevents_silent_loss(self):
        self.crm.validate("pharmacy")
        self.api.missing_field = "ufAddress"
        with self.assertRaises(ConfigError):
            self.crm.create(self.state, 10, 1)
        self.assertFalse(any(m == "crm.item.add" for m, p in self.api.calls))

    def test_invalid_create_response_has_unknown_outcome(self):
        self.api.bad_create_response = True
        with self.assertRaises(RemoteError) as raised:
            self.crm.create(self.state, 10, 1)
        self.assertTrue(raised.exception.uncertain)

    def test_request_lookup_uses_exact_stable_key(self):
        self.api.items = [{"id": 25}]
        result = self.crm.find_request("pharmacy", "datfo-request-id")
        self.assertEqual(result["url"], "https://demo.invalid/crm/type/1000/details/25/")
        filters = self.api.calls[-1][1]["filter"]
        self.assertEqual(filters, {"xmlId": "datfo-request-id", "categoryId": 0})
        self.assertEqual(self.api.calls[-1][1]['select'], ['*'])

    def test_ambiguous_request_is_error(self):
        self.api.items = [{"id": 25}, {"id": 26}]
        with self.assertRaisesRegex(RemoteError, "DUPLICATE_REQUEST_ID"):
            self.crm.find_request("pharmacy", "datfo-request-id")

    def test_pharmacy_duplicate_filter_is_location_specific(self):
        self.crm.duplicate_pharmacy(self.state)
        filters = self.api.calls[-1][1]["filter"]
        self.assertEqual(filters["companyId"], 101)
        self.assertEqual(filters["ufAddress"], self.state["address"])

    def test_pharmacy_without_description_uses_note_with_entered_data(self):
        fields = {key: value for key, value in self.config.targets["pharmacy"].fields.items() if key != "description"}
        target = replace(self.config.targets["pharmacy"], fields=fields)
        config = replace(self.config, targets={**self.config.targets, "pharmacy": target})
        crm = Crm(config, self.api)
        self.assertTrue(crm.needs_note("pharmacy"))
        self.state["company"] = None
        crm.add_note(self.state, {"id": 25}, 10, 1)
        payload = self.api.calls[-1][1]["fields"]
        self.assertEqual(payload["ENTITY_TYPE"], "dynamic_1000")
        self.assertIn("123456789", payload["COMMENT"])
        self.assertIn(self.state["address"], payload["COMMENT"])
        self.assertTrue(payload["COMMENT"].endswith(crm.note_tag(self.state)))

    def test_note_lookup_finds_exact_request_tag(self):
        with patch.object(self.api, "list_all", return_value=[{"ID": 90, "COMMENT": "Исходный текст\n" + self.crm.note_tag(self.state)}]):
            self.assertTrue(self.crm.find_note(self.state, {"id": 25}))
        with patch.object(self.api, "list_all", return_value=[{"ID": 90, "COMMENT": "Иной запрос"}]):
            self.assertFalse(self.crm.find_note(self.state, {"id": 25}))


class ApiTests(unittest.TestCase):
    def test_pagination_reads_all_pages(self):
        api = Bitrix("https://demo.invalid/rest/1/demo/")
        with patch.object(api, "call", side_effect=[{"result": {"items": [{"id": 1}]}, "next": 50}, {"result": {"items": [{"id": 2}]}}]) as calls:
            result = api.list_all("crm.item.list", {}, key="items")
        self.assertEqual(result, [{"id": 1}, {"id": 2}])
        self.assertEqual(calls.call_args_list[1].args[1]["start"], 50)

    def test_broken_pagination_is_not_infinite(self):
        api = Bitrix("https://demo.invalid/rest/1/demo/")
        with patch.object(api, "call", return_value={"result": [], "next": 0}), self.assertRaises(RemoteError):
            api.list_all("crm.company.list", {})

    def test_exception_redacts_unknown_remote_code(self):
        self.assertEqual(str(RemoteError("https://secret.invalid/rest/1/password/")), "REMOTE_ERROR")


class ConfigTests(unittest.TestCase):
    def target_data(self):
        return {"entity_type_id": 1000, "category_id": None, "fields": {"title": "title", "description": "comments", "company_id": "companyId",
                "responsible_id": "assignedById", "request_id": "xmlId", "address": "ufAddress", "phone": "ufPhone"}, "defaults": {"opened": "N"}}

    def test_pharmacy_requires_real_type_id(self):
        value = self.target_data()
        value["entity_type_id"] = None
        with self.assertRaises(ConfigError):
            Target.parse("pharmacy", value)

    def test_pharmacy_cannot_replace_company_object(self):
        value = self.target_data()
        value["entity_type_id"] = 4
        with self.assertRaises(ConfigError):
            Target.parse("pharmacy", value)

    def test_field_mapping_collision_rejected(self):
        value = self.target_data()
        value["fields"]["address"] = "title"
        with self.assertRaises(ConfigError):
            Target.parse("pharmacy", value)

    def test_default_cannot_overwrite_company_link(self):
        value = self.target_data()
        value["defaults"]["companyId"] = 999
        with self.assertRaises(ConfigError):
            Target.parse("pharmacy", value)

    def test_second_process_cannot_use_same_database(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "bot.sqlite3"
            with process_lock(database):
                with self.assertRaises(RuntimeError):
                    with process_lock(database):
                        self.fail("Second process acquired the lock")

    def test_example_is_deliberately_not_live_configuration(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(ConfigError):
            Config.load(settings_path=Path(__file__).resolve().parents[1] / "settings.example.json")

    def test_support_can_use_existing_deal_pipeline(self):
        value = self.target_data()
        value.update(entity_type_id=2, category_id=47)
        target = Target.parse("support", value)
        self.assertEqual(target.entity_type_id, 2)
        self.assertEqual(target.category_id, 47)


if __name__ == "__main__":
    unittest.main()
