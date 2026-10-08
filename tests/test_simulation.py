from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from datfo_crm_bot.api import RemoteError
from datfo_crm_bot.config import User
from datfo_crm_bot.registration import Registration, RegistrationSettings
from datfo_crm_bot.simulation import EXIT, NOTICE, Simulation
from datfo_crm_bot.storage import Store


class Telegram:
    def __init__(self):
        self.messages = []
        self.fail = False

    def send(self, chat_id, text, keyboard=None):
        if self.fail and not text.startswith('⏳ Подождите') and not text.startswith('⏳ Кутинг'):
            self.fail = False
            raise RemoteError("CONNECTION_ERROR", uncertain=True)
        self.messages.append((chat_id, text, keyboard))


class NoDirectory:
    def search(self, name):
        raise AssertionError("Simulation must not call Bitrix directory")

    def employee(self, employee_id):
        raise AssertionError("Simulation must not call Bitrix directory")


class SimulationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.primary = Store(Path(self.folder.name) / "live.sqlite3")
        self.store = Store(Path(self.folder.name) / "simulation.sqlite3")
        self.primary.request_registration(100, 100, {"id": 132, "name": "Малик"})
        self.primary.approve_registration(100, 100, {"id": 132, "name": "Малик"})
        self.telegram = Telegram()
        self.registration = Registration(RegistrationSettings({100: User(132, True, "Малик")}), self.primary, NoDirectory(), self.telegram)
        self.simulation = Simulation(self.registration, self.store, self.telegram)
        self.number = 2000

    def tearDown(self):
        self.store.close()
        self.primary.close()
        self.folder.cleanup()

    def message(self, text, user=100, chat=None):
        self.number += 1
        return {"update_id": self.number, "message": {"from": {"id": user}, "chat": chat or {"id": user, "type": "private"}, "text": text}}

    def say(self, text, user=100, chat=None):
        update = self.message(text, user, chat)
        handled = self.simulation.handle(update)
        # Simulate restoration of a previously started pharmacy draft.
        if text == '/pharmacy':
            state = self.store.session(user)
            if state and state.get('step') == 'okb_bulk_input':
                self.store.set_session(user, {**state, 'step': 'okb_inn'})
        return handled, update

    def form(self, kind="deal", inn="123456789", choice="1"):
        self.say("/test")
        if kind == "pharmacy":
            for text in ("/pharmacy", inn, choice, "Тестовая карточка", "Без телефона", "3", "МИРАБАДСКИЙ", "1", "Ташкент, учебная улица, 1", "Пропустить локацию"):
                self.say(text)
            return self.say("Подтвердить")[1]
        for text in ("/" + kind, "Тестовая карточка", inn, choice,
                     "Ташкент, учебная улица, 1" if kind == "pharmacy" else "Описание теста", "Без телефона"):
            self.say(text)
        return self.say("Подтвердить")[1]

    def test_test_start_shows_fictional_data_and_keeps_registration(self):
        handled, update = self.say("/test")
        self.assertTrue(handled)
        self.assertTrue(self.simulation.active(100))
        self.assertEqual(self.primary.registration(100)["status"], "approved")
        self.assertIn("123456789", self.telegram.messages[-1][1])
        self.assertIn("987654321", self.telegram.messages[-1][1])
        self.assertIn([EXIT], self.telegram.messages[-1][2])

    def test_deal_is_only_simulated_and_does_not_have_fake_live_link(self):
        self.form()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM operations WHERE status='succeeded'").fetchone()[0], 1)
        self.assertEqual(self.primary.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 0)
        self.assertIn("Симуляция завершена", self.telegram.messages[-1][1])
        self.assertIn("не создавалась", self.telegram.messages[-1][1])
        self.assertNotIn("href=", self.telegram.messages[-1][1])
        self.assertNotIn("demo.invalid", self.telegram.messages[-1][1])
        self.assertNotIn("Сохранено в Б24", self.telegram.messages[-1][1])
        self.assertTrue(all(text.startswith(NOTICE) for _, text, _ in self.telegram.messages))

    def test_phone_can_be_skipped_and_preview_is_explicitly_simulation(self):
        self.say("/test")
        for text in ("/deal", "Проверка", "Без ИНН", "Описание", "Без телефона"):
            self.say(text)
        self.assertIn("завершить тест без записи в Б24", self.telegram.messages[-1][1])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM operations WHERE status='succeeded'").fetchone()[0], 0)

    def test_pharmacy_chooses_correct_fictional_company(self):
        self.form("pharmacy", "987654321", "2")
        state = self.store.session(100)
        self.assertEqual(state["company"]["id"], 103)
        self.assertEqual(state["address"], "Ташкент, учебная улица, 1")

    def test_support_and_statistics_only_count_simulations(self):
        self.form("support")
        for text in ("/stats", "Мои", "Сегодня"):
            self.say(text)
        response = self.telegram.messages[-1][1]
        self.assertIn("Учебная статистика", response)
        self.assertIn("Заявки техподдержки: 1", response)
        self.assertEqual(self.primary.statistics("0000", "9999", None), {})

    def test_double_confirmation_and_replayed_update_do_not_duplicate(self):
        update = self.form()
        self.simulation.handle(update)
        self.say("Подтвердить")
        self.assertEqual(len(self.simulation.bot.crm.items), 1)

    def test_restart_restores_dialog_mode_and_fictional_cards(self):
        self.form("pharmacy")
        self.simulation = Simulation(self.registration, self.store, self.telegram)
        self.assertTrue(self.simulation.active(100))
        self.assertEqual(len(self.simulation.bot.crm.items), 1)
        self.form("pharmacy")
        self.assertIn("уже есть", self.telegram.messages[-1][1])
        self.assertEqual(len(self.simulation.bot.crm.items), 1)

    def test_lost_telegram_result_reply_recovers_without_recreating(self):
        self.say("/test")
        for text in ("/deal", "Тест", "Без ИНН", "Описание", "Без телефона"):
            self.say(text)
        update = self.message("Подтвердить")
        self.telegram.fail = True
        with self.assertRaises(RemoteError):
            self.simulation.handle(update)
        self.simulation.handle(update)
        self.assertEqual(len(self.simulation.bot.crm.items), 1)
        self.assertIn("Симуляция завершена", self.telegram.messages[-1][1])

    def test_exit_clears_test_draft_but_preserves_real_draft_and_membership(self):
        self.primary.set_session(100, {"step": "real_draft"})
        self.say("/test")
        self.say("/deal")
        self.say(EXIT)
        self.assertFalse(self.simulation.active(100))
        self.assertIsNone(self.store.session(100))
        self.assertEqual(self.primary.session(100), {"step": "real_draft"})
        self.assertEqual(self.primary.recipient(132), 100)
        self.assertFalse(self.say("/profile")[0])

    def test_non_admin_and_groups_cannot_enter_test(self):
        self.primary.request_registration(200, 200, {"id": 150, "name": "Менеджер"})
        self.primary.approve_registration(200, 100, {"id": 150, "name": "Менеджер"})
        self.say("/test", 200)
        self.assertFalse(self.simulation.active(200))
        self.assertFalse(self.say("/test", 300, {"id": -1, "type": "group"})[0])

    def test_revoked_access_blocks_active_test_and_saved_responses(self):
        self.say("/test")
        self.primary.deny_registration(100, 100, "revoked")
        self.say("/stats")
        self.assertFalse(self.simulation.active(100))
        self.assertIn("доступна администратору", self.telegram.messages[-1][1])

    def test_same_database_for_simulation_is_rejected(self):
        with self.assertRaises(RuntimeError):
            Simulation(self.registration, self.primary, self.telegram)


if __name__ == "__main__":
    unittest.main()
