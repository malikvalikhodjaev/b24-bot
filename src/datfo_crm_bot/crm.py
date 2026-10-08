from __future__ import annotations

from urllib.parse import urlsplit
from .api import Bitrix, RemoteError
from .config import Config, ConfigError, Target


class Crm:
    def __init__(self, config: Config, api: Bitrix):
        self.config, self.api = config, api
        parsed = urlsplit(config.webhook)
        self.portal = f"{parsed.scheme}://{parsed.netloc}"
        self.validated: set[str] = set()

    def validate(self, kind: str) -> None:
        if kind in self.validated:
            return
        target = self.config.targets[kind]
        result = self.api.call("crm.item.fields", {"entityTypeId": target.entity_type_id, "useOriginalUfNames": "Y"})["result"]
        fields = result.get("fields") if isinstance(result, dict) else None
        if not isinstance(fields, dict):
            raise RemoteError("INVALID_FIELDS")
        configured = set(target.fields.values()) | set(target.defaults)
        if target.category_id is not None:
            configured.add("categoryId")
        for name in configured:
            meta = fields.get(name)
            if not isinstance(meta, dict) or meta.get("isReadOnly") is True:
                raise ConfigError(f"{kind}: поле {name} отсутствует или недоступно для записи")
        if target.category_id is not None:
            result = self.api.call("crm.category.list", {"entityTypeId": target.entity_type_id})["result"]
            categories = result.get("categories") if isinstance(result, dict) else None
            if not isinstance(categories, list) or not any(str(row.get("id")) == str(target.category_id) for row in categories):
                raise ConfigError(f"{kind}: воронка {target.category_id} не найдена для выбранного типа")
        self.validated.add(kind)

    def companies(self, inn: str) -> list[dict]:
        rows = self.api.list_all("crm.requisite.list", {"filter": {"ENTITY_TYPE_ID": 4, "RQ_INN": inn},
                                                      "select": ["ENTITY_ID", "ENTITY_TYPE_ID", "RQ_INN"]})
        ids = sorted({int(row["ENTITY_ID"]) for row in rows
                      if str(row.get("ENTITY_TYPE_ID")) == "4" and str(row.get("RQ_INN", "")).strip() == inn
                      and str(row.get("ENTITY_ID", "")).isascii() and str(row.get("ENTITY_ID", "")).isdigit() and int(row["ENTITY_ID"]) > 0})
        if not ids:
            return []
        # crm.company.list matches the existing Datfo exporter and avoids its documented universal-company incompatibility.
        companies = self.api.list_all("crm.company.list", {"filter": {"@ID": ids}, "select": ["ID", "TITLE"], "order": {"ID": "ASC"}})
        results = {int(row["ID"]): {"id": int(row["ID"]), "title": str(row.get("TITLE") or f"Компания #{row['ID']}")}
                   for row in companies if str(row.get("ID", "")).isdigit() and int(row["ID"]) in ids}
        if len(results) != len(ids):
            raise RemoteError("COMPANY_LOOKUP_INCOMPLETE")
        return [results[item_id] for item_id in ids]

    def _list(self, target: Target, filters: dict) -> list[dict]:
        if target.category_id is not None:
            filters = {**filters, "categoryId": target.category_id}
        # This portal drops standard fields from explicit select; filtered wildcard reads work.
        rows = self.api.list_all("crm.item.list", {"entityTypeId": target.entity_type_id, "useOriginalUfNames": "Y",
                                                  "filter": filters, "select": ["*"], "order": {"id": "ASC"}}, key="items")
        return [{"id": row.get("id", row.get("ID")), "title": row.get("title", row.get("TITLE", ""))} for row in rows]

    def result(self, target: Target, item: dict, *, existing: bool = False) -> dict:
        try:
            item_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            raise RemoteError("INVALID_ITEM_ID", uncertain=True) from None
        if item_id < 1:
            raise RemoteError("INVALID_ITEM_ID", uncertain=True)
        path = f"/crm/deal/details/{item_id}/" if target.entity_type_id == 2 else f"/crm/type/{target.entity_type_id}/details/{item_id}/"
        return {"id": item_id, "url": self.portal + path, "existing": existing}

    def find_request(self, kind: str, request_id: str) -> dict | None:
        target = self.config.targets[kind]
        rows = self._list(target, {target.fields["request_id"]: request_id})
        if len(rows) > 1:
            raise RemoteError("DUPLICATE_REQUEST_ID")
        return self.result(target, rows[0]) if rows else None

    def duplicate_pharmacy(self, state: dict) -> dict | None:
        if state["kind"] != "pharmacy" or not state.get("company"):
            return None
        target = self.config.targets["pharmacy"]
        fields = target.fields
        rows = self._list(target, {fields["title"]: state["title"], fields["address"]: state["address"],
                                   fields["company_id"]: state["company"]["id"]})
        if len(rows) > 1:
            raise RemoteError("DUPLICATE_PHARMACY_RECORDS")
        return self.result(target, rows[0], existing=True) if rows else None

    @staticmethod
    def description(state: dict, bitrix_user_id: int, telegram_user_id: int) -> str:
        details = [state.get("description", ""), f"Телефон: {state.get('phone') or 'не указан'}",
                   f"ИНН, введённый пользователем: {state.get('inn') or 'не указан'}",
                   f"Автор: Telegram ID {telegram_user_id}; сотрудник Б24 {bitrix_user_id}"]
        if state.get("address"):
            details.insert(1, f"Адрес: {state['address']}")
        if state.get("next_step"):
            details.insert(1, f"Следующий шаг: {state['next_step']}\nСрок: {state['deadline']}")
        return "\n".join(item for item in details if item)

    def needs_note(self, kind: str) -> bool:
        return "description" not in self.config.targets[kind].fields

    @staticmethod
    def note_tag(state: dict) -> str:
        return "Запрос бота: " + state["request_id"]

    def find_note(self, state: dict, result: dict) -> bool:
        target = self.config.targets[state["kind"]]
        entity_type = "deal" if target.entity_type_id == 2 else f"dynamic_{target.entity_type_id}"
        rows = self.api.list_all("crm.timeline.comment.list", {"filter": {"ENTITY_ID": result["id"], "ENTITY_TYPE": entity_type},
                                                             "select": ["ID", "COMMENT"], "order": {"ID": "DESC"}})
        return any(str(row.get("COMMENT", "")).rstrip().endswith(self.note_tag(state)) for row in rows)

    def add_note(self, state: dict, result: dict, bitrix_user_id: int, telegram_user_id: int) -> None:
        target = self.config.targets[state["kind"]]
        entity_type = "deal" if target.entity_type_id == 2 else f"dynamic_{target.entity_type_id}"
        response = self.api.call("crm.timeline.comment.add", {"fields": {"ENTITY_ID": result["id"], "ENTITY_TYPE": entity_type,
            "COMMENT": self.description(state, bitrix_user_id, telegram_user_id) + "\n" + self.note_tag(state)}})
        value = response.get("result")
        if isinstance(value, bool) or not str(value).isdigit() or int(value) < 1:
            raise RemoteError("INVALID_NOTE_RESPONSE", uncertain=True)

    def create(self, state: dict, bitrix_user_id: int, telegram_user_id: int, *, on_submit=None) -> dict:
        # Check the live field schema again immediately before mutation: Bitrix ignores unknown fields.
        self.validated.discard(state["kind"])
        self.validate(state["kind"])
        target = self.config.targets[state["kind"]]
        values = {"title": state["title"], "description": self.description(state, bitrix_user_id, telegram_user_id),
                  "responsible_id": target.responsible_id or bitrix_user_id, "request_id": state["request_id"]}
        if state["kind"] == "pharmacy":
            values["manager_ids"] = [bitrix_user_id]
        if state.get("company"):
            values["company_id"] = state["company"]["id"]
        if state.get("pharmacy_id"):
            values["pharmacy_id"] = state["pharmacy_id"]
        for field in ("address", "phone", "inn"):
            if state.get(field):
                values[field] = state[field]
        payload = dict(target.defaults)
        for name, value in values.items():
            if name in target.fields:
                payload[target.fields[name]] = value
        if target.category_id is not None:
            payload["categoryId"] = target.category_id
        if on_submit is not None:
            on_submit()
        response = self.api.call("crm.item.add", {"entityTypeId": target.entity_type_id, "useOriginalUfNames": "Y", "fields": payload})
        result = response["result"]
        item = result.get("item") if isinstance(result, dict) else None
        if not isinstance(item, dict):
            raise RemoteError("INVALID_CREATE_RESPONSE", uncertain=True)
        return self.result(target, item)
