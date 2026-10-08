from __future__ import annotations

import json

from .config import ROOT, ConfigError
from .okb_crm import OkbSettings
from .region_cities import RegionCities


class OkbDemoMixin:
    """Fictional OKB components; no external API, directory or Bitrix credentials."""
    def setup_okb(self, store):
        self.store = store
        self.settings = OkbSettings.load()
        self.catalogue = json.loads((ROOT / "okb-catalogue.json").read_text(encoding="utf-8"))
        self.region_cities = RegionCities()
        saved = store.db.execute("SELECT value FROM settings WHERE key='simulation-okb-records'").fetchone()
        self.records = json.loads(saved[0]) if saved else {"companies": {}, "requisites": {}, "contacts": {}, "bindings": [], "pharmacy_contacts": {}}
        if not saved:
            self.records["contacts"] = {
                "301": {"id": 301, "title": "Учебный контакт", "phone": "+998901234567"},
                "302": {"id": 302, "title": "Учебный контакт А", "phone": "+998901111111"},
                "303": {"id": 303, "title": "Учебный контакт Б", "phone": "+998901111111"}}
            self.records["bindings"] = [[301, 101]]
            self.save_okb()

    def save_okb(self):
        with self.store.db:
            self.store.db.execute("INSERT INTO settings VALUES('simulation-okb-records',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(self.records, ensure_ascii=False),))

    def validate_okb(self, state=None):
        self.region_cities.validate(state)

    def cities_for_region(self, region_id):
        return self.region_cities.choices(region_id, self.choices('city'))

    def choices(self, key):
        return self.catalogue[key]

    def companies(self, inn):
        return super().companies(inn) + [{"id": row["id"], "title": row["title"]} for row in self.records["companies"].values() if row.get("inn") == inn]

    def find_company_request(self, request_id):
        row = self.records["companies"].get(request_id)
        return {"id": row["id"], "title": row["title"]} if row else None

    def create_company(self, state, manager_id):
        row = {"id": 200 + len(self.records["companies"]), "title": state["company_name"], "manager_id": manager_id}
        self.records["companies"][state["request_id"]] = row
        self.save_okb()
        return {"id": row["id"], "title": row["title"]}

    def find_requisite(self, company_id, inn):
        if (company_id, inn) in {(101, "123456789"), (102, "987654321"), (103, "987654321")}:
            return {"id": company_id}
        row = self.records["requisites"].get(str(company_id) + ":" + inn)
        return {"id": row["id"]} if row else None

    def create_requisite(self, state, company):
        row = {"id": 400 + len(self.records["requisites"]), "company_id": company["id"], "inn": state["inn"]}
        self.records["requisites"][str(company["id"]) + ":" + state["inn"]] = row
        for entry in self.records["companies"].values():
            if entry["id"] == company["id"]:
                entry["inn"] = state["inn"]
        self.save_okb()
        return {"id": row["id"]}

    def contacts(self, phone):
        return [{"id": row["id"], "title": row["title"]} for row in self.records["contacts"].values() if row["phone"] == phone]

    def contact_positions(self, *, fresh=False):
        return [{'id':str(number), 'title':title, 'field':'UF_CRM_1747116501447'}
                for number, title in enumerate(('Владелец', 'Управляющий', 'Фармацевт', 'Технический специалист', 'Закупщик', 'Бухгалтер'), 3265)]

    def validate_new_contact(self, state):
        if not state.get('create_contact') or not state.get('contact_name') or not state.get('phone'):
            raise ConfigError('Создание контакта требует подтверждения, телефона и имени')
        if state.get('contact_position') not in self.contact_positions(fresh=True):
            raise ConfigError('Выбранная должность контакта изменилась')

    def find_contact_request(self, request_id):
        for row in self.records["contacts"].values():
            if row.get("request_id") == request_id:
                return {"id": row["id"], "title": row["title"]}
        return None

    def create_contact(self, state, manager_id):
        self.validate_new_contact(state)
        row = {"id": 1000 + len(self.records["contacts"]), "title": state['contact_name'],
               "phone": state["phone"], "request_id": state["request_id"], "manager_id": manager_id,
               'position':state['contact_position']}
        self.records["contacts"][str(row["id"])] = row
        self.save_okb()
        return {"id": row["id"], "title": row["title"]}

    def contact_company_link(self, contact_id, company_id):
        return {"linked": True} if [contact_id, company_id] in self.records["bindings"] else None

    def add_contact_company(self, contact_id, company_id):
        if [contact_id, company_id] not in self.records["bindings"]:
            self.records["bindings"].append([contact_id, company_id])
        self.save_okb()
        return {"linked": True}

    def create_okb_pharmacy(self, state, company, manager_id, contact):
        result = self.create({**state, "company": company}, manager_id, 0)
        self.records["pharmacy_contacts"][str(result["id"])] = [contact["id"]] if contact else []
        self.save_okb()
        return result

    def pharmacy_contact_link(self, pharmacy_id, contact_id):
        return {"linked": True} if contact_id in self.records["pharmacy_contacts"].get(str(pharmacy_id), []) else None

    def add_pharmacy_contact(self, pharmacy_id, contact_id):
        ids = self.records["pharmacy_contacts"].setdefault(str(pharmacy_id), [])
        if contact_id not in ids:
            ids.append(contact_id)
        self.save_okb()
        return {"linked": True}

    def pharmacy_location(self, pharmacy_id, point):
        from .intake_ui import coordinates
        return {'saved': True} if self.records.get('pharmacy_locations', {}).get(str(pharmacy_id)) == coordinates(point) else None

    def add_pharmacy_location(self, pharmacy_id, point):
        from .intake_ui import coordinates
        self.records.setdefault('pharmacy_locations', {})[str(pharmacy_id)] = coordinates(point)
        self.save_okb()
        return {'saved': True}
