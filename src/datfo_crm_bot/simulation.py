from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path

from .config import ROOT
from .demo import DemoCrm, demo_config
from .okb_demo import OkbDemoMixin
from .okb_service import OKB, OkbBot
from .service import Bot, LABELS, MENU
from .storage import Store, process_lock
from .guidance import with_guide

TEST = "🧪 Тестовая симуляция"
EXIT = "Завершить симуляцию"
NOTICE = "<b>🧪 ТЕСТ — Б24 не изменяется</b>\n\n"


class SimulationTelegram:
    def __init__(self, telegram):
        self.telegram = telegram

    def send(self, chat_id: int, text: str, keyboard=None) -> None:
        rows = [list(row) for row in (keyboard or [])]
        if not any(EXIT in row for row in rows):
            rows.append([EXIT])
        self.telegram.send(chat_id, NOTICE + text, with_guide(rows))


class SimulationCrm(OkbDemoMixin, DemoCrm):
    """Fictional cards persist in the simulation database only; no Bitrix client exists here."""
    def __init__(self, store: Store):
        super().__init__()
        self.store = store
        self.setup_okb(store)
        for row in store.db.execute("SELECT key,value FROM settings WHERE key LIKE 'simulation-card:%'"):
            self.items[row["key"].split(":", 1)[1]] = json.loads(row["value"])

    def create(self, state: dict, bitrix_user_id: int, telegram_user_id: int, *, on_submit=None) -> dict:
        existing = self.find_request(state["kind"], state["request_id"])
        if existing:
            return existing
        result = super().create(state, bitrix_user_id, telegram_user_id, on_submit=on_submit)
        with self.store.db:
            self.store.db.execute("INSERT INTO settings(key,value) VALUES(?,?)", (
                "simulation-card:" + state["request_id"], json.dumps(self.items[state["request_id"]], ensure_ascii=False)))
        return result


class SimulationBot(OkbBot):
    @staticmethod
    def success(state: dict, result: dict):
        state["step"] = "done"
        action = "Эта учебная аптека уже есть; повторную не создавали" if result.get("existing") else "Симуляция завершена"
        text = f"✅ {action}: {LABELS[state['kind']]} · учебная карточка #{result['id']}.\nРеальная запись в Б24 не создавалась. Можно проверить учебную статистику или завершить симуляцию."
        return text, MENU, state

    def preview(self, user_id: int, state: dict):
        text, keyboard, state = super().preview(user_id, state)
        text = text.replace("Нажмите «Подтвердить», чтобы сохранить в Б24.", "Нажмите «Подтвердить», чтобы завершить тест без записи в Б24.")
        return text, keyboard, state

    def stats(self, user_id: int, state: dict, text: str):
        response, keyboard, state = super().stats(user_id, state, text)
        response = response.replace("Создано через бота · ", "Учебная статистика · ")
        return response, keyboard, state


class Simulation:
    def __init__(self, registration, store: Store, telegram):
        self.registration, self.store, self.telegram = registration, store, telegram
        primary_path = registration.store.db.execute("PRAGMA database_list").fetchone()[2]
        simulation_path = store.db.execute("PRAGMA database_list").fetchone()[2]
        if Path(primary_path).resolve() == Path(simulation_path).resolve():
            raise RuntimeError("Симуляция требует отдельную базу")
        config = replace(demo_config(Path(simulation_path)), users={})
        self.bot = SimulationBot(config, store, SimulationCrm(store), SimulationTelegram(telegram))

    def active(self, user_id: int) -> bool:
        row = self.registration.store.db.execute("SELECT value FROM settings WHERE key=?", ("simulation-active:" + str(user_id),)).fetchone()
        return bool(row and row[0] == "1")

    def set_active(self, user_id: int, active: bool) -> None:
        with self.registration.store.db:
            self.registration.store.db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                               ("simulation-active:" + str(user_id), "1" if active else "0"))

    def handle(self, update: dict) -> bool:
        message = update.get("message", {})
        sender, chat = message.get("from", {}), message.get("chat", {})
        if chat.get("type") != "private" or sender.get("is_bot") or not sender.get("id") or chat.get("id") != sender["id"]:
            return False
        user_id, chat_id = int(sender["id"]), int(chat["id"])
        text = message.get("text", "")
        text = text.strip() if isinstance(text, str) else ""
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.startswith("/") else text
        active = self.active(user_id)
        if command not in {"/test", TEST, "/endtest", EXIT} and not active:
            return False
        user = self.registration.user(user_id)
        if not user or not user.admin:
            self.set_active(user_id, False)
            self.bot.config.users.pop(user_id, None)
            self.telegram.send(chat_id, "Тестовая симуляция сейчас доступна администратору.", self.registration.keyboard(user_id))
            return True
        if command in {"/endtest", EXIT}:
            self.set_active(user_id, False)
            self.store.set_session(user_id, None)
            self.telegram.send(chat_id, "Симуляция завершена. Учебные результаты хранятся отдельно; Б24 не изменялся. Вы вернулись к регистрации команды.", self.registration.keyboard(user_id))
            return True
        self.bot.config.users[user_id] = user
        if command in {"/test", TEST}:
            self.set_active(user_id, True)
            introduction = "Выберите действие: сделка, ОКБ, аптека или поддержка.\n\nДля ОКБ ИНН обязателен: <code>123456789</code> найдёт одну учебную компанию, <code>987654321</code> — две. <code>111222333</code> проверит создание новой компании. Телефон <code>+998901234567</code> найдёт учебный контакт; <code>+998901111111</code> — два для выбора.\n\nПосле заполнения проверьте данные и нажмите «Подтвердить». Для выхода — «Завершить симуляцию» или /endtest."
            SimulationTelegram(self.telegram).send(chat_id, introduction, [[OKB]] + MENU)
            return True
        from .registration import PROFILE_LABELS
        if command in {"/profile", "/registrations", "/members", "/approve", "/reject", "/revoke", *PROFILE_LABELS, "Заявки менеджеров", "Подключённые менеджеры"}:
            return False
        self.bot.handle(update)
        return True


@contextmanager
def simulation_for(registration, telegram):
    database = ROOT / "data" / "simulation.sqlite3"
    with process_lock(database):
        store = Store(database)
        try:
            yield Simulation(registration, store, telegram)
        finally:
            store.close()
