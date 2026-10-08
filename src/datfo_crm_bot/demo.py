from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo
from .config import Config, Target, User


def demo_config(database: Path) -> Config:
    targets = {}
    for kind, type_id in (("deal", 2), ("pharmacy", 1000), ("support", 1002)):
        fields = {"title": "title", "description": "comments", "company_id": "companyId", "responsible_id": "assignedById",
                  "request_id": "originId" if kind == "deal" else "xmlId"}
        if kind == "pharmacy":
            fields["address"] = "ufAddress"
            fields["phone"] = "ufPhone"
        targets[kind] = Target(type_id, 0, None, fields, {"opened": "N"})
    return Config("", "https://demo.invalid/rest/1/demo/", database, ZoneInfo("Asia/Tashkent"), {1: User(10, True), 2: User(20)}, targets)


class DemoCrm:
    """Explicitly fictional and offline. Never passes data to an external API."""
    def __init__(self):
        self.items = {}

    def validate(self, kind: str) -> None:
        return None

    def companies(self, inn: str) -> list[dict]:
        if inn == "123456789":
            return [{"id": 101, "title": "Учебная компания — вымышленные данные"}]
        if inn == "987654321":
            return [{"id": 102, "title": "Учебная компания А"}, {"id": 103, "title": "Учебная компания Б"}]
        return []

    def find_request(self, kind: str, request_id: str) -> dict | None:
        return self.items.get(request_id, {}).get("result")

    def needs_note(self, kind: str) -> bool:
        return False

    def duplicate_pharmacy(self, state: dict) -> dict | None:
        if state["kind"] != "pharmacy" or not state.get("company"):
            return None
        for item in self.items.values():
            previous = item["state"]
            if previous["kind"] == "pharmacy" and all(previous.get(key) == state.get(key) for key in ("title", "address", "company")):
                return {**item["result"], "existing": True}
        return None

    def create(self, state: dict, bitrix_user_id: int, telegram_user_id: int, *, on_submit=None) -> dict:
        if on_submit:
            on_submit()
        result = {"id": len(self.items) + 1, "url": "https://demo.invalid/offline-card", "existing": False}
        self.items[state["request_id"]] = {"state": dict(state), "result": result}
        return result


class ConsoleTelegram:
    def send(self, chat_id: int, text: str, keyboard=None) -> None:
        print("\nБОТ:", text)
        if keyboard:
            print("Кнопки:", " / ".join(" | ".join(button if isinstance(button, str) else button["text"] for button in row) for row in keyboard))
