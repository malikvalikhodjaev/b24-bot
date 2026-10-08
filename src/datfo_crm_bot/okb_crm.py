from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from .api import RemoteError
from .config import ConfigError, ROOT, Target, positive_id
from .crm import Crm
from .region_cities import RegionCities
from .intake_ui import coordinates


@dataclass(frozen=True)
class OkbSettings:
    pharmacy: Target
    preset_id: int
    status_id: str
    stage_id: str

    @classmethod
    def load(cls, path: Path | None = None):
        try:
            data = json.loads((path or ROOT / "okb-settings.json").read_text(encoding="utf-8-sig"))
            target = Target.parse("pharmacy", data["pharmacy"])
            preset_id = positive_id(data["requisite_preset_id"], "Шаблон реквизитов")
            status_id = str(positive_id(data["default_status_id"], "Статус аптеки"))
            stage_id = data["default_stage_id"]
        except (OSError, ValueError, KeyError, TypeError):
            raise ConfigError("Не удалось прочитать настройки ОКБ") from None
        required = {"business_region", "city", "landmark", "status", "stage", "contact_ids", "manager_ids"}
        if not required.issubset(target.fields) or not isinstance(stage_id, str) or not re.fullmatch(r"[A-Za-z0-9_:]+", stage_id):
            raise ConfigError("ОКБ: не настроены поля региона, города, ориентира, статуса, контактов и менеджера")
        return cls(target, preset_id, status_id, stage_id)


def one(rows: list[dict], code: str) -> dict | None:
    if len(rows) > 1:
        raise RemoteError(code)
    return rows[0] if rows else None


class OkbCrm(Crm):
    def __init__(self, config, api, settings: OkbSettings):
        super().__init__(config, api)
        self.settings = settings
        self._fields = None
        self._contact_fields = None
        self.region_cities = RegionCities()

    def pharmacy_fields(self, *, fresh=False):
        if fresh or self._fields is None:
            result = self.api.call("crm.item.fields", {"entityTypeId": self.settings.pharmacy.entity_type_id, "useOriginalUfNames": "Y"})["result"]
            self._fields = result.get("fields") if isinstance(result, dict) else None
        if not isinstance(self._fields, dict):
            raise RemoteError("INVALID_FIELDS")
        return self._fields

    def choices(self, key: str) -> list[dict]:
        field = self.pharmacy_fields().get(self.settings.pharmacy.fields[key], {})
        items = field.get("items")
        if field.get("type") != "enumeration" or field.get("isMultiple") or not isinstance(items, list):
            raise ConfigError("ОКБ: требуется одиночный справочник " + key)
        return [{"id": str(item["ID"]), "title": str(item["VALUE"])} for item in items]

    def validate_okb(self, state: dict | None = None):
        if state and state.get('location'):
            coordinates(state['location'])
            if not {'latitude', 'longitude'}.issubset(self.settings.pharmacy.fields):
                raise ConfigError('ОКБ: поля координат не настроены')
        self.region_cities.validate(state)
        self.validated.discard("pharmacy")
        self.validate("pharmacy")
        self.pharmacy_fields(fresh=True)
        keys = ['business_region','city','status']
        if 'current_program' in self.settings.pharmacy.fields:
            keys.append('current_program')
        for key in keys:
            choices = self.choices(key)
            selected = self.settings.status_id if key == "status" else (state or {}).get(key, {}).get("id")
            if key=='current_program' and state is not None and not selected:
                raise ConfigError('ОКБ: выберите текущую программу из списка перед сохранением')
            if selected is not None and not any(item["id"] == str(selected) for item in choices):
                raise ConfigError("ОКБ: выбранное значение больше не существует: " + key)
        stage_entity = f"DYNAMIC_{self.settings.pharmacy.entity_type_id}_STAGE_{self.settings.pharmacy.category_id}"
        stages = self.api.list_all("crm.status.list", {"filter": {"ENTITY_ID": stage_entity}})
        if not any(row.get("STATUS_ID") == self.settings.stage_id for row in stages):
            raise ConfigError("ОКБ: начальная стадия не найдена")
        for method, required in (
            ("crm.company.fields", {"TITLE", "ASSIGNED_BY_ID", "ORIGINATOR_ID", "ORIGIN_ID", "OPENED"}),
            ("crm.contact.fields", {"NAME", "PHONE", "ASSIGNED_BY_ID", "ORIGINATOR_ID", "ORIGIN_ID", "OPENED"}),
            ("crm.requisite.fields", {"ENTITY_TYPE_ID", "ENTITY_ID", "PRESET_ID", "NAME", "RQ_INN", "XML_ID"})):
            fields = self.api.call(method)["result"]
            if not isinstance(fields, dict) or any(not isinstance(fields.get(key), dict) or fields[key].get("isReadOnly") for key in required):
                raise ConfigError("ОКБ: недоступны необходимые поля " + method)
        presets = self.api.list_all("crm.requisite.preset.list", {"filter": {"ID": self.settings.preset_id}})
        if not any(str(row.get("ID")) == str(self.settings.preset_id) and str(row.get("ENTITY_TYPE_ID")) == "8" for row in presets):
            raise ConfigError("ОКБ: шаблон реквизитов не найден")
        fields = self.api.call("crm.requisite.preset.field.list", {"preset": {"ID": self.settings.preset_id}})["result"]
        if not isinstance(fields, list) or not {"RQ_INN", "RQ_COMPANY_NAME"}.issubset({row.get("FIELD_NAME") for row in fields}):
            raise ConfigError("ОКБ: в шаблоне реквизитов отсутствует ИНН или название компании")

    def cities_for_region(self, region_id):
        return self.region_cities.choices(region_id, self.choices('city'))

    def find_company_request(self, request_id: str) -> dict | None:
        rows = self.api.list_all("crm.company.list", {"filter": {"ORIGINATOR_ID": "datfo_telegram_okb", "ORIGIN_ID": request_id}, "select": ["ID", "TITLE", "ORIGIN_ID"]})
        rows = [{"id": positive_id(row["ID"], "Компания"), "title": str(row["TITLE"])} for row in rows if row.get("ORIGIN_ID") == request_id]
        return one(rows, "DUPLICATE_COMPANY_REQUEST")

    def create_company(self, state: dict, manager_id: int) -> dict:
        result = self.api.call("crm.company.add", {"fields": {"TITLE": state["company_name"], "ASSIGNED_BY_ID": manager_id,
            "OPENED": "N", "ORIGINATOR_ID": "datfo_telegram_okb", "ORIGIN_ID": state["request_id"]}})["result"]
        return {"id": self.write_id(result), "title": state["company_name"]}

    @staticmethod
    def write_id(value) -> int:
        if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit() or int(value) < 1:
            raise RemoteError("INVALID_WRITE_RESPONSE", uncertain=True)
        return int(value)

    def find_requisite(self, company_id: int, inn: str) -> dict | None:
        rows = self.api.list_all("crm.requisite.list", {"filter": {"ENTITY_TYPE_ID": 4, "ENTITY_ID": company_id, "RQ_INN": inn}, "select": ["ID", "ENTITY_ID", "ENTITY_TYPE_ID", "RQ_INN"]})
        matches = [row for row in rows if str(row.get("ENTITY_ID")) == str(company_id) and str(row.get("ENTITY_TYPE_ID")) == "4" and str(row.get("RQ_INN", "")).strip() == inn]
        return {"id": positive_id(matches[0]["ID"], "Реквизиты")} if matches else None

    def create_requisite(self, state: dict, company: dict) -> dict:
        external_code = state['request_id']
        if len(external_code) > 45:
            external_code = 'fom-' + hashlib.sha256(external_code.encode('utf-8')).hexdigest()[:40]
        result = self.api.call("crm.requisite.add", {"fields": {"ENTITY_TYPE_ID": 4, "ENTITY_ID": company["id"], "PRESET_ID": self.settings.preset_id,
            "NAME": company["title"], "RQ_COMPANY_NAME": company["title"], "RQ_INN": state["inn"], "XML_ID": external_code, "ACTIVE": "Y"}})["result"]
        return {"id": self.write_id(result)}

    def contacts(self, phone: str) -> list[dict]:
        result = self.api.call("crm.duplicate.findbycomm", {"entity_type": "CONTACT", "type": "PHONE", "values": [phone]})["result"]
        # This portal serializes an empty PHP map as [], rather than {}.
        if result == []:
            return []
        if not isinstance(result, dict):
            raise RemoteError("INVALID_DUPLICATE_RESPONSE")
        ids = result.get("CONTACT", [])
        if not isinstance(ids, list):
            raise RemoteError("INVALID_DUPLICATE_RESPONSE")
        if len(ids) > 20:
            raise RemoteError("TOO_MANY_CONTACT_DUPLICATES")
        ids = {positive_id(value, "Контакт") for value in ids}
        if not ids:
            return []
        rows = self.api.list_all("crm.contact.list", {"filter": {"@ID": sorted(ids)}, "select": ["ID", "NAME", "LAST_NAME", "PHONE"]})
        if {int(row["ID"]) for row in rows} != ids:
            raise RemoteError("CONTACT_LOOKUP_INCOMPLETE")
        digits = re.sub(r"\D", "", phone)
        return [{"id": int(row["ID"]), "title": " ".join(str(row.get(key) or "") for key in ("NAME", "LAST_NAME")).strip() or "Контакт"}
            for row in rows if any(re.sub(r"\D", "", str(entry.get("VALUE", ""))) == digits for entry in row.get("PHONE", []))]

    def contact_positions(self, *, fresh=False) -> list[dict]:
        if fresh or self._contact_fields is None:
            self._contact_fields = self.api.call('crm.contact.fields')['result']
        if not isinstance(self._contact_fields, dict):
            raise RemoteError('INVALID_CONTACT_FIELDS')
        matches = [(key, field) for key, field in self._contact_fields.items()
                   if isinstance(field, dict) and any(str(field.get(label, '')).strip().casefold() == 'должность список'
                       for label in ('title', 'formLabel', 'listLabel', 'filterLabel'))]
        if len(matches) != 1:
            raise ConfigError('Не найден единственный справочник контакта «Должность список»')
        key, field = matches[0]
        items = field.get('items')
        if field.get('type') != 'enumeration' or field.get('isMultiple') or field.get('isReadOnly') or not isinstance(items, list) or not items:
            raise ConfigError('Поле «Должность список» должно быть доступным одиночным списком')
        choices = [{'id':str(positive_id(item['ID'], 'Должность')), 'title':str(item['VALUE']), 'field':key} for item in items]
        if len({row['id'] for row in choices}) != len(choices):
            raise ConfigError('В справочнике должностей повторяются ID')
        return choices

    def validate_new_contact(self, state: dict):
        if not state.get('create_contact') or not state.get('contact_name') or not state.get('phone'):
            raise ConfigError('Создание контакта требует подтверждения, телефона и имени')
        if state.get('contact_position') not in self.contact_positions(fresh=True):
            raise ConfigError('Выбранная должность контакта изменилась')

    def find_contact_request(self, request_id: str) -> dict | None:
        rows = self.api.list_all("crm.contact.list", {"filter": {"ORIGINATOR_ID": "datfo_telegram_okb", "ORIGIN_ID": request_id}, "select": ["ID", "NAME", "ORIGIN_ID"]})
        return one([{"id": int(row["ID"]), "title": str(row.get("NAME", "Контакт"))} for row in rows if row.get("ORIGIN_ID") == request_id], "DUPLICATE_CONTACT_REQUEST")

    def create_contact(self, state: dict, manager_id: int) -> dict:
        self.validate_new_contact(state)
        name = state['contact_name']
        position = state['contact_position']
        result = self.api.call("crm.contact.add", {"fields": {"NAME": name, "PHONE": [{"VALUE": state["phone"], "VALUE_TYPE": "WORK"}],
            position['field']:position['id'],
            "ASSIGNED_BY_ID": manager_id, "OPENED": "N", "ORIGINATOR_ID": "datfo_telegram_okb", "ORIGIN_ID": state["request_id"]}})["result"]
        return {"id": self.write_id(result), "title": name}

    def contact_company_link(self, contact_id: int, company_id: int) -> dict | None:
        rows = self.api.call("crm.contact.company.items.get", {"id": contact_id})["result"]
        if not isinstance(rows, list):
            raise RemoteError("INVALID_CONTACT_BINDINGS")
        return {"linked": True} if any(str(row.get("COMPANY_ID")) == str(company_id) for row in rows) else None

    def add_contact_company(self, contact_id: int, company_id: int) -> dict:
        result = self.api.call("crm.contact.company.add", {"id": contact_id, "fields": {"COMPANY_ID": company_id, "IS_PRIMARY": "N"}})["result"]
        if result is not True:
            raise RemoteError("INVALID_LINK_RESPONSE", uncertain=True)
        return {"linked": True}

    def create_okb_pharmacy(self, state: dict, company: dict, manager_id: int, contact: dict | None) -> dict:
        target = self.settings.pharmacy
        values = {"title": state["title"], "company_id": company["id"], "responsible_id": manager_id, "manager_ids": [manager_id],
                  "request_id": state["request_id"], "address": state["address"], "landmark": state.get("landmark", ""),
                  "business_region": state["business_region"]["id"], "city": state["city"]["id"], "status": self.settings.status_id, "stage": self.settings.stage_id}
        if contact:
            values["contact_ids"] = [contact["id"]]
        if state.get('phone_only') and state.get('phone'):
            values['phone'] = state['phone']
        if state.get('location'):
            values.update({key: format(value, '.8f') for key, value in coordinates(state['location']).items()})
        if 'current_program' in target.fields:
            if not state.get('current_program'):
                raise ConfigError('ОКБ: текущая программа обязательна')
            values['current_program'] = state['current_program']['id']
        payload = {**target.defaults, **{target.fields[key]: value for key, value in values.items()}, "categoryId": target.category_id}
        # Without contact consent, keep the number in the pharmacy's own phone field.
        result = self.api.call("crm.item.add", {"entityTypeId": target.entity_type_id, "useOriginalUfNames": "Y", "fields": payload})["result"]
        item = result.get("item") if isinstance(result, dict) else None
        if not isinstance(item, dict):
            raise RemoteError("INVALID_CREATE_RESPONSE", uncertain=True)
        return self.result(target, item)

    def pharmacy_contact_link(self, pharmacy_id: int, contact_id: int) -> dict | None:
        result = self.api.call("crm.item.get", {"entityTypeId": self.settings.pharmacy.entity_type_id, "id": pharmacy_id, "useOriginalUfNames": "Y"})["result"]
        item = result.get("item") if isinstance(result, dict) else None
        if not isinstance(item, dict) or not isinstance(item.get("contactIds", []), list):
            raise RemoteError("INVALID_PHARMACY_CONTACTS")
        return {"linked": True} if any(str(value) == str(contact_id) for value in item.get("contactIds", [])) else None

    def pharmacy_location(self, pharmacy_id: int, point: dict) -> dict | None:
        row = self.api.call('crm.item.get', {'entityTypeId': self.settings.pharmacy.entity_type_id,
            'id': pharmacy_id, 'useOriginalUfNames': 'Y'})['result'].get('item')
        if not isinstance(row, dict):
            raise RemoteError('INVALID_PHARMACY_LOCATION')
        for key, value in coordinates(point).items():
            try:
                actual = float(row.get(self.settings.pharmacy.fields[key]))
            except (ValueError, TypeError):
                return None
            if abs(actual - value) > 0.00000001:
                return None
        return {'saved': True}

    def add_pharmacy_location(self, pharmacy_id: int, point: dict) -> dict:
        fields = {self.settings.pharmacy.fields[key]: format(value, '.8f')
                  for key, value in coordinates(point).items()}
        result = self.api.call('crm.item.update', {'entityTypeId': self.settings.pharmacy.entity_type_id,
            'id': pharmacy_id, 'useOriginalUfNames': 'Y', 'fields': fields})['result']
        if not isinstance(result, dict) or not isinstance(result.get('item'), dict):
            raise RemoteError('INVALID_LOCATION_RESPONSE', uncertain=True)
        return {'saved': True}

    def add_pharmacy_contact(self, pharmacy_id: int, contact_id: int) -> dict:
        try:
            result = self.api.call("crm.item.get", {"entityTypeId": self.settings.pharmacy.entity_type_id, "id": pharmacy_id, "useOriginalUfNames": "Y"})["result"]
        except RemoteError as exc:
            raise RemoteError(exc.code) from None
        item = result.get("item") if isinstance(result, dict) else None
        if not isinstance(item, dict) or not isinstance(item.get("contactIds", []), list):
            raise RemoteError("INVALID_PHARMACY_CONTACTS")
        ids = list(dict.fromkeys([positive_id(value, "Контакт") for value in item.get("contactIds", [])] + [contact_id]))
        result = self.api.call("crm.item.update", {"entityTypeId": self.settings.pharmacy.entity_type_id, "id": pharmacy_id,
            "useOriginalUfNames": "Y", "fields": {self.settings.pharmacy.fields["contact_ids"]: ids}})["result"]
        if not isinstance(result, dict) or not isinstance(result.get("item"), dict):
            raise RemoteError("INVALID_LINK_RESPONSE", uncertain=True)
        return {"linked": True}
