from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from datfo_crm_bot.api import RemoteError
from datfo_crm_bot.demo import DemoCrm, demo_config
from datfo_crm_bot.service import Bot, CHECK, CONFIRM, normalize_inn, normalize_phone
from datfo_crm_bot.storage import Store


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.fail_next = False

    def send(self, chat_id, text, keyboard=None):
        if self.fail_next and not text.startswith('⏳ Подождите') and not text.startswith('⏳ Кутинг'):
            self.fail_next = False
            raise RemoteError("CONNECTION_ERROR", uncertain=True)
        self.messages.append((chat_id, text, keyboard))


class LosingCrm(DemoCrm):
    def __init__(self, *, save=True, code="CONNECTION_ERROR", uncertain=True):
        super().__init__()
        self.save, self.code, self.uncertain = save, code, uncertain
        self.attempts = 0

    def create(self, state, bitrix_user_id, telegram_user_id, *, on_submit=None):
        if on_submit:
            on_submit()
        self.attempts += 1
        if self.save:
            super().create(state, bitrix_user_id, telegram_user_id)
        raise RemoteError(self.code, uncertain=self.uncertain)


class NoteCrm(DemoCrm):
    def __init__(self, *, lose=False, save_note=True, rejection=False):
        super().__init__()
        self.notes = set()
        self.lose, self.save_note, self.rejection = lose, save_note, rejection
        self.note_attempts = 0

    def needs_note(self, kind):
        return kind == "pharmacy"

    def find_note(self, state, result):
        return state["request_id"] in self.notes

    def add_note(self, state, result, bitrix_user_id, telegram_user_id):
        self.note_attempts += 1
        if self.rejection:
            raise RemoteError("ACCESS_DENIED")
        if self.save_note:
            self.notes.add(state["request_id"])
        if self.lose:
            raise RemoteError("CONNECTION_ERROR", uncertain=True)


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = demo_config(Path(self.temp.name) / "test.sqlite3")
        self.store = Store(self.config.database)
        self.telegram, self.crm = FakeTelegram(), DemoCrm()
        self.now = datetime(2026, 10, 4, 12, 30, tzinfo=self.config.timezone)
        self.bot = Bot(self.config, self.store, self.crm, self.telegram, clock=lambda: self.now)
        self.update_id = 0

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def message(self, text, user_id=1, *, update_id=None, private=True):
        if update_id is None:
            self.update_id += 1
            update_id = self.update_id
        return {"update_id": update_id, "message": {"from": {"id": user_id}, "chat": {"id": user_id, "type": "private" if private else "group"}, "text": text}}

    def send(self, text, user_id=1):
        self.bot.handle(self.message(text, user_id))
        return self.telegram.messages[-1][1]

    def draft(self, kind="deal", user_id=1, inn="123456789", choice="1"):
        self.send("/" + kind, user_id)
        self.send("Аптека / проект <А>", user_id)
        self.send(inn, user_id)
        if inn != "Без ИНН":
            self.send(choice, user_id)
        self.send("Ташкент, ул. Учебная, 15" if kind == "pharmacy" else "Описание исходного запроса", user_id)
        self.send("+998 (90) 123-45-67", user_id)
        return self.store.session(user_id)

    def test_three_forms_do_not_write_until_confirmation(self):
        for kind in ("deal", "pharmacy", "support"):
            with self.subTest(kind=kind):
                state = self.draft(kind)
                before = len(self.crm.items)
                self.assertEqual(state["step"], "confirm")
                self.assertIn("перед сохранением", self.telegram.messages[-1][1])
                self.send(CONFIRM)
                self.assertEqual(len(self.crm.items), before + 1)
                self.assertEqual(self.store.operation(state["request_id"])["status"], "succeeded")

    def test_company_explicit_choice_preserves_correct_id(self):
        self.draft("pharmacy", inn="987654321", choice="2")
        self.send(CONFIRM)
        self.assertEqual(next(iter(self.crm.items.values()))["state"]["company"]["id"], 103)

    def test_invalid_company_selection_cannot_advance(self):
        self.send("/deal")
        self.send("Тест")
        self.send("987654321")
        self.send("3")
        self.assertEqual(self.store.session(1)["step"], "company")
        self.assertFalse(self.crm.items)

    def test_unknown_inn_requires_explicit_unbound_choice(self):
        self.send("/pharmacy")
        self.send("Новая аптека")
        self.send("111111111")
        self.assertEqual(self.store.session(1)["step"], "company")
        self.send("Продолжить без компании")
        self.send("Ташкент, улица, 2")
        self.send("Без телефона")
        self.send(CONFIRM)
        created = next(iter(self.crm.items.values()))["state"]
        self.assertIsNone(created["company"])
        self.assertEqual(created["inn"], "111111111")

    def test_skip_inn(self):
        self.draft("support", inn="Без ИНН")
        self.send(CONFIRM)
        self.assertIsNone(next(iter(self.crm.items.values()))["state"]["company"])

    def test_inn_mistake_keeps_draft(self):
        self.send("/deal")
        self.send("Тест")
        self.send("ИНН123456789")
        self.assertEqual(self.store.session(1)["step"], "inn")
        self.assertIn("только цифры", self.telegram.messages[-1][1])

    def test_phone_mistake_keeps_draft(self):
        self.send("/support")
        self.send("Не могу войти")
        self.send("Без ИНН")
        self.send("Ошибка авторизации")
        self.send("телефон 901234567")
        self.assertEqual(self.store.session(1)["step"], "phone")

    def test_double_confirmation_does_not_duplicate(self):
        state = self.draft()
        self.send(CONFIRM)
        self.send(CONFIRM)
        self.assertEqual(len(self.crm.items), 1)
        self.assertEqual(self.store.operation(state["request_id"])["status"], "succeeded")

    def test_replayed_update_does_not_advance_twice(self):
        self.send("/deal")
        update = self.message("Название")
        self.bot.handle(update)
        first = self.telegram.messages[-1]
        self.bot.handle(update)
        self.assertEqual(self.store.session(1)["step"], "inn")
        self.assertEqual(first, self.telegram.messages[-1])

    def test_lost_telegram_reply_replays_without_advancing(self):
        self.send("/deal")
        update = self.message("Название")
        self.telegram.fail_next = True
        with self.assertRaises(RemoteError):
            self.bot.handle(update)
        self.bot.handle(update)
        self.assertEqual(self.store.session(1)["step"], "inn")
        self.assertIn("Введите ИНН", self.telegram.messages[-1][1])

    def test_replay_confirmation_after_send_failure_still_one_item(self):
        self.draft()
        update = self.message(CONFIRM)
        self.telegram.fail_next = True
        with self.assertRaises(RemoteError):
            self.bot.handle(update)
        self.bot.handle(update)
        self.assertEqual(len(self.crm.items), 1)
        self.assertIn("Сохранено", self.telegram.messages[-1][1])

    def test_restart_restores_draft_and_offset(self):
        self.send("/pharmacy")
        self.send("Новая точка")
        self.store.set_offset(42)
        self.store.close()
        self.store = Store(self.config.database)
        self.bot = Bot(self.config, self.store, self.crm, self.telegram, clock=lambda: self.now)
        self.assertEqual(self.store.offset(), 42)
        self.send("123456789")
        self.assertEqual(self.store.session(1)["step"], "company")

    def test_unknown_result_recovers_existing_item(self):
        self.crm = LosingCrm()
        self.bot.crm = self.crm
        self.draft()
        self.send(CONFIRM)
        self.assertIn("ответ потерян", self.telegram.messages[-1][1])
        self.send(CHECK)
        self.assertIn("Сохранено", self.telegram.messages[-1][1])
        self.assertEqual(self.crm.attempts, 1)

    def test_unknown_result_without_match_never_retries_create(self):
        self.crm = LosingCrm(save=False)
        self.bot.crm = self.crm
        self.draft()
        self.send(CONFIRM)
        for text in (CHECK, CONFIRM, "/cancel", "/deal", "/pending"):
            self.send(text)
        self.assertEqual(self.crm.attempts, 1)
        self.assertFalse(self.crm.items)

    def test_restart_after_unknown_result_verifies_only(self):
        self.crm = LosingCrm(save=False)
        self.bot.crm = self.crm
        self.draft()
        self.send(CONFIRM)
        self.store.close()
        self.store = Store(self.config.database)
        self.bot = Bot(self.config, self.store, self.crm, self.telegram, clock=lambda: self.now)
        self.send("/pending")
        self.assertEqual(self.crm.attempts, 1)

    def test_explicit_rejection_can_retry_same_operation(self):
        self.crm = LosingCrm(save=False, code="REQUIRED_FIELD_MISSING", uncertain=False)
        self.bot.crm = self.crm
        state = self.draft()
        self.send(CONFIRM)
        self.assertEqual(self.store.operation(state["request_id"])["status"], "rejected")
        self.bot.crm = DemoCrm()
        self.send(CONFIRM)
        self.assertEqual(self.store.operation(state["request_id"])["status"], "succeeded")

    def test_link_changed_before_confirmation_prevents_write(self):
        self.draft()
        self.crm.companies = lambda inn: []
        self.send(CONFIRM)
        self.assertIn("Связь ИНН", self.telegram.messages[-1][1])
        self.assertFalse(self.crm.items)

    def test_existing_pharmacy_is_not_created_or_counted(self):
        self.draft("pharmacy")
        self.send(CONFIRM)
        self.draft("pharmacy")
        self.send(CONFIRM)
        self.assertEqual(len(self.crm.items), 1)
        self.assertIn("Уже есть", self.telegram.messages[-1][1])
        self.send("/stats")
        self.send("Мои")
        result = self.send("Сегодня")
        self.assertIn("Аптеки: 1", result)

    def test_same_company_different_addresses_are_separate_pharmacies(self):
        self.draft("pharmacy")
        self.send(CONFIRM)
        self.send("/pharmacy")
        self.send("Аптека / проект <А>")
        self.send("123456789")
        self.send("1")
        self.send("Ташкент, другая улица, 2")
        self.send("Без телефона")
        self.send(CONFIRM)
        self.assertEqual(len(self.crm.items), 2)

    def test_cancel_never_writes(self):
        self.draft()
        self.send("/cancel")
        self.send(CONFIRM)
        self.assertIsNone(self.store.session(1))
        self.assertFalse(self.crm.items)

    def test_unlisted_user_cannot_create_or_read_statistics(self):
        for text in ("/deal", "/stats", "По команде", CONFIRM):
            self.assertIn("Доступ ещё не настроен", self.send(text, 99))
        self.assertFalse(self.crm.items)

    def test_group_messages_are_ignored(self):
        self.bot.handle(self.message("/deal", private=False))
        self.assertFalse(self.telegram.messages)
        self.assertIsNone(self.store.session(1))

    def test_nonadmin_cannot_get_team_statistics(self):
        self.draft(user_id=1)
        self.send(CONFIRM, 1)
        self.send("/stats", 2)
        self.send("По команде", 2)
        response = self.send("Сегодня", 2)
        self.assertIn("мои действия", response)
        self.assertIn("Сделки: 0", response)

    def test_team_statistics_include_other_users(self):
        self.draft(user_id=2)
        self.send(CONFIRM, 2)
        self.send("/stats")
        self.send("По команде")
        result = self.send("Эта неделя")
        self.assertIn("команда", result)
        self.assertIn("Сделки: 1", result)
        self.assertIn("28.09.2026 00:00", result)

    def test_local_midnight_period_boundary(self):
        state = self.draft()
        self.send(CONFIRM)
        result = {"id": 1, "url": "https://demo.invalid"}
        self.store.status(state["request_id"], "succeeded", result, "2026-10-03T19:01:00+00:00")
        self.send("/stats")
        self.send("Мои")
        self.assertIn("Сделки: 1", self.send("Сегодня"))
        self.store.status(state["request_id"], "succeeded", result, "2026-10-03T18:59:00+00:00")
        self.send("/stats")
        self.send("Мои")
        self.assertIn("Сделки: 0", self.send("Сегодня"))

    def test_revoked_access_cannot_replay_old_statistics(self):
        self.send("/stats")
        self.send("Мои")
        update = self.message("Сегодня")
        self.bot.handle(update)
        self.bot.config = replace(self.config, users={2: self.config.users[2]})
        self.bot.handle(update)
        self.assertIn("Доступ ещё не настроен", self.telegram.messages[-1][1])

    def test_html_in_title_is_escaped(self):
        self.draft()
        self.assertIn("&lt;А&gt;", self.telegram.messages[-1][1])

    def test_demoted_admin_cannot_replay_team_statistics(self):
        self.send("/stats")
        self.send("По команде")
        update = self.message("Сегодня")
        self.bot.handle(update)
        demoted = replace(self.config.users[1], admin=False)
        self.bot.config = replace(self.config, users={**self.config.users, 1: demoted})
        self.bot.handle(update)
        self.assertIn("Права доступа изменились", self.telegram.messages[-1][1])
        self.assertNotIn("Сделки:", self.telegram.messages[-1][1])

    def test_read_failure_before_submission_is_retryable(self):
        state = self.draft()
        original = self.crm.create
        def fail_before_submit(*args, **kwargs):
            raise RemoteError("CONNECTION_ERROR", uncertain=True)
        self.crm.create = fail_before_submit
        self.send(CONFIRM)
        self.assertEqual(self.store.operation(state["request_id"])["status"], "rejected")
        self.assertIn("Запись не отправлена", self.telegram.messages[-1][1])
        self.crm.create = original
        self.send(CONFIRM)
        self.assertEqual(len(self.crm.items), 1)

    def test_schema_failure_before_submission_does_not_leave_uncertain_write(self):
        from datfo_crm_bot.config import ConfigError
        state = self.draft()
        def invalid_schema(*args, **kwargs):
            raise ConfigError("Неизвестное поле")
        self.crm.create = invalid_schema
        self.send(CONFIRM)
        self.assertEqual(self.store.operation(state["request_id"])["status"], "rejected")
        self.assertFalse(self.store.unfinished(1))

    def test_pharmacy_note_completes_without_duplicate_card(self):
        self.bot.crm = NoteCrm()
        state = self.draft("pharmacy")
        self.send(CONFIRM)
        self.assertEqual(self.store.operation(state["request_id"])["status"], "succeeded")
        self.assertEqual(self.bot.crm.note_attempts, 1)
        self.assertEqual(len(self.bot.crm.items), 1)

    def test_lost_note_reply_recovers_existing_note(self):
        self.bot.crm = NoteCrm(lose=True)
        state = self.draft("pharmacy")
        self.send(CONFIRM)
        self.assertEqual(self.store.operation(state["request_id"])["status"], "created")
        self.send(CHECK)
        self.assertEqual(self.store.operation(state["request_id"])["status"], "succeeded")
        self.assertEqual(self.bot.crm.note_attempts, 1)
        self.assertEqual(len(self.bot.crm.items), 1)

    def test_unknown_note_without_match_never_reposts_or_recreates(self):
        self.bot.crm = NoteCrm(lose=True, save_note=False)
        state = self.draft("pharmacy")
        self.send(CONFIRM)
        self.send(CHECK)
        self.send(CHECK)
        self.assertEqual(self.store.operation(state["request_id"])["status"], "created")
        self.assertEqual(self.bot.crm.note_attempts, 1)
        self.assertEqual(len(self.bot.crm.items), 1)

    def test_rejected_note_can_retry_without_recreating_card(self):
        self.bot.crm = NoteCrm(rejection=True)
        state = self.draft("pharmacy")
        self.send(CONFIRM)
        self.bot.crm.rejection = False
        self.send(CHECK)
        self.assertEqual(self.store.operation(state["request_id"])["status"], "succeeded")
        self.assertEqual(self.bot.crm.note_attempts, 2)
        self.assertEqual(len(self.bot.crm.items), 1)

    def test_identity_collection_does_not_authorize_user(self):
        self.store.remember_identity({"id": 99, "username": "learner"})
        self.assertIn("Доступ ещё не настроен", self.send("/deal", 99))


class ValidationTests(unittest.TestCase):
    def test_uzbek_inn_separators(self):
        self.assertEqual(normalize_inn("123 456-789", (9,)), "123456789")

    def test_invalid_inn_cannot_be_silently_normalized(self):
        for text in ("foo123456789", "12345678", "١٢٣٤٥٦٧٨٩", "123456789.0", ""):
            with self.subTest(text=text), self.assertRaises(ValueError):
                normalize_inn(text, (9,))

    def test_phone_accepts_local_numbers_and_preserves_foreign_numbers(self):
        self.assertEqual(normalize_phone("+998 (90) 123-45-67"), "+998901234567")
        self.assertEqual(normalize_phone("901234567"), "+998901234567")
        self.assertEqual(normalize_phone("998 90 123-45-67"), "+998901234567")
        self.assertEqual(normalize_phone("+44 20 7123 4567"), "+442071234567")
        self.assertEqual(normalize_phone("442071234567"), "442071234567")
        self.assertEqual(normalize_phone("123-45-67"), "1234567")
        for value in ("+123", "номер 901234567", "++998901234567", "١٢٣٤٥٦٧٨٩", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_phone(value)


if __name__ == "__main__":
    unittest.main()
