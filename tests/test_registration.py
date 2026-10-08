from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from datfo_crm_bot.api import RemoteError
from datfo_crm_bot.config import ConfigError, User
from datfo_crm_bot.demo import demo_config, DemoCrm
from datfo_crm_bot.registration import Directory, Registration, RegistrationSettings
from datfo_crm_bot.service import Bot
from datfo_crm_bot.storage import Store


class Telegram:
    def __init__(self):
        self.messages = []
        self.fail = False

    def send(self, chat_id, text, keyboard=None):
        if self.fail:
            self.fail = False
            raise RemoteError("CONNECTION_ERROR", uncertain=True)
        self.messages.append((chat_id, text, keyboard))


class Employees:
    def __init__(self):
        self.rows = {10: {"id": 10, "name": "Руководитель"}, 20: {"id": 20, "name": "Менеджер"}}

    def employee(self, employee_id):
        return self.rows.get(employee_id)

    def search(self, name):
        return [row for row in self.rows.values() if name.casefold() in row["name"].casefold()]


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "bot.sqlite3"
        self.store = Store(self.path)
        self.telegram, self.directory = Telegram(), Employees()
        self.settings = RegistrationSettings({1: User(10, True, "Руководитель")})
        self.registration = Registration(self.settings, self.store, self.directory, self.telegram)
        self.registration.bootstrap()
        self.number = 100

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def message(self, user_id, text, **overrides):
        self.number += 1
        message = {"from": {"id": user_id, "first_name": "Человек"}, "chat": {"id": user_id, "type": "private"}, "text": text}
        message.update(overrides)
        return {"update_id": self.number, "message": message}

    def say(self, user_id, text, **overrides):
        update = self.message(user_id, text, **overrides)
        self.registration.handle(update)
        return update

    def request(self, user_id=2, name="Менеджер"):
        self.say(user_id, "/start")
        self.say(user_id, name)
        return self.say(user_id, "1")

    def test_username_and_phone_not_required_and_name_does_not_grant_access(self):
        self.request()
        self.assertEqual(self.store.registration(2)["status"], "pending")
        self.assertIsNone(self.registration.user(2))
        self.assertIsNone(self.store.recipient(20))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)

    def test_approve_routes_to_exact_chat_and_grants_regular_role(self):
        self.request()
        self.say(1, "/approve 2")
        self.assertEqual(self.registration.user(2), User(20, False, "Менеджер"))
        self.assertEqual(self.store.recipient(20), 2)
        self.assertIsNone(self.store.recipient(999))
        self.say(2, "/profile")
        self.assertIn("Подтверждена", self.telegram.messages[-1][1])

    def test_arbitrary_manager_cannot_approve_or_view_other_requests(self):
        self.request()
        self.say(3, "/approve 2")
        self.say(3, "/registrations")
        self.assertIsNone(self.registration.user(2))
        self.assertNotIn("Б24 #20", self.telegram.messages[-1][1])

    def test_same_employee_cannot_be_bound_twice(self):
        self.request(2)
        self.request(3)
        self.say(1, "/approve 2")
        self.say(1, "/approve 3")
        self.assertEqual(self.store.recipient(20), 2)
        self.assertIsNone(self.registration.user(3))
        self.assertIn("уже привязана", self.telegram.messages[-1][1])

    def test_owner_cannot_be_impersonated_or_revoked_via_chat(self):
        self.request(2, "Руководитель")
        self.say(1, "/approve 2")
        self.assertIsNone(self.registration.user(2))
        self.say(1, "/revoke 1")
        self.assertTrue(self.registration.user(1).admin)

    def test_employee_deactivated_after_request_not_approved(self):
        self.request()
        del self.directory.rows[20]
        self.say(1, "/approve 2")
        self.assertIsNone(self.registration.user(2))
        self.assertIn("больше не активен", self.telegram.messages[-1][1])

    def test_revocation_stops_routing_and_re_registration(self):
        self.request()
        self.say(1, "/approve 2")
        self.store.set_session(2, {"step": "title"})
        self.say(1, "/revoke 2")
        self.assertIsNone(self.store.recipient(20))
        self.assertIsNone(self.store.session(2))
        self.say(2, "/register")
        self.assertIsNone(self.store.registration_session(2))
        self.assertIn("отключён", self.telegram.messages[-1][1])

    def test_rejected_manager_can_reapply_without_granting_access(self):
        self.request()
        self.say(1, "/reject 2")
        self.request()
        self.assertEqual(self.store.registration(2)["status"], "pending")
        self.assertIsNone(self.store.recipient(20))

    def test_group_or_mismatched_chat_cannot_register(self):
        self.say(2, "/start", chat={"id": -123, "type": "group"})
        self.say(2, "/start", chat={"id": 3, "type": "private"})
        self.assertEqual(self.telegram.messages, [])
        self.assertIsNone(self.store.registration_session(2))

    def test_restart_restores_pending_and_binding(self):
        self.request()
        self.store.close()
        self.store = Store(self.path)
        self.registration = Registration(self.settings, self.store, self.directory, self.telegram)
        self.registration.bootstrap()
        self.say(1, "/approve 2")
        self.assertEqual(self.store.recipient(20), 2)

    def test_replay_and_lost_approval_reply_do_not_duplicate_or_change_role(self):
        update = self.request()
        self.registration.handle(update)
        approval = self.message(1, "/approve 2")
        self.telegram.fail = True
        with self.assertRaises(RemoteError):
            self.registration.handle(approval)
        self.registration.handle(approval)
        self.assertEqual(len(self.store.registrations("approved")), 2)
        self.assertFalse(self.registration.user(2).admin)

    def test_approved_member_can_use_forms_then_revocation_blocks_saved_replies(self):
        self.request()
        self.say(1, "/approve 2")
        config = replace(demo_config(self.path), users={1: User(10, True)})
        bot = Bot(config, self.store, DemoCrm(), self.telegram)
        update = self.message(2, "/deal")
        self.registration.handle(update, bot)
        self.assertEqual(self.store.session(2)["step"], "title")
        self.assertEqual(config.users[2].bitrix_id, 20)
        self.say(1, "/revoke 2")
        self.registration.handle(update, bot)
        self.assertNotIn(2, config.users)
        self.assertIn("отключён", self.telegram.messages[-1][1])

    def test_bootstrap_rejects_name_mismatch(self):
        bad = Registration(RegistrationSettings({99: User(10, True, "Чужое имя")}), self.store, self.directory, self.telegram)
        with self.assertRaises(ConfigError):
            bad.bootstrap()

    def test_html_escaping_in_profile_and_admin_list(self):
        self.store.request_registration(2, 2, {"id": 20, "name": "<b>Менеджер</b>"})
        self.say(1, "/registrations")
        self.assertIn("&lt;b&gt;", self.telegram.messages[-1][1])

    def test_invalid_choice_and_cancel_do_not_submit(self):
        self.say(2, "/start")
        self.say(2, "Неизвестный")
        self.say(2, "Менеджер")
        self.say(2, "999")
        self.assertIsNone(self.store.registration(2))
        self.say(2, "Отмена регистрации")
        self.assertIsNone(self.store.registration_session(2))

    def test_approval_buttons_keep_admin_permissions(self):
        self.request()
        self.say(1, "/registrations")
        self.assertIn(["Подтвердить 2", "Отклонить 2"], self.telegram.messages[-1][2])
        self.say(3, "Подтвердить 2")
        self.assertIsNone(self.registration.user(2))
        self.say(1, "Подтвердить 2")
        self.assertEqual(self.store.recipient(20), 2)

    def test_lost_search_reply_can_show_candidates_again(self):
        self.say(2, "/start")
        self.telegram.fail = True
        update = self.message(2, "Менеджер")
        with self.assertRaises(RemoteError):
            self.registration.handle(update)
        self.registration.handle(update)
        self.assertIn("1. Менеджер", self.telegram.messages[-1][1])

    def test_large_team_lists_have_bounded_messages_and_pagination(self):
        for index in range(2, 25):
            self.store.request_registration(index, index, {"id": 100 + index, "name": "&" * 120})
            self.store.remember_identity({"id": index, "first_name": "<" * 64, "last_name": "<" * 64})
        self.say(1, "/registrations")
        self.assertLess(len(self.telegram.messages[-1][1]), 4096)
        self.assertIn("Следующие: /registrations", self.telegram.messages[-1][1])


class DirectoryTests(unittest.TestCase):
    def test_filtered_lookup_retains_only_active_internal_employee_identifiers(self):
        class Api:
            def list_all(self, method, payload):
                self.method, self.payload = method, payload
                return [{"ID": "20", "ACTIVE": True, "NAME": "Менеджер", "LAST_NAME": "Продаж", "EMAIL": "private"},
                        {"ID": "21", "ACTIVE": False, "NAME": "Уволен"},
                        {"ID": "22", "ACTIVE": True, "USER_TYPE": "extranet", "NAME": "Внешний"}]
        api = Api()
        directory = Directory(api)
        self.assertEqual(directory.search("Менеджер"), [{"id": 20, "name": "Менеджер Продаж"}])
        self.assertEqual(api.payload["FILTER"]["NAME_SEARCH"], "Менеджер")
        self.assertNotIn("EMAIL", api.payload["select"])
        self.assertIsNone(directory.employee(21))

    def test_invalid_registration_settings_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "registration.json"
            path.write_text('{"admins": {"1": {"bitrix_id": 10, "name": "A"}, "2": {"bitrix_id": 10, "name": "A"}}}', encoding="utf-8")
            with self.assertRaises(ConfigError):
                RegistrationSettings.load(path)


if __name__ == "__main__":
    unittest.main()
