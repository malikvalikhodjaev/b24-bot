"""Read OKB metadata only; credentials and personal contact values are never printed."""
import concurrent.futures
import json
from pathlib import Path
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from datfo_crm_bot.api import Bitrix, RemoteError
from datfo_crm_bot.config import connection_from_env, load_env

load_env(ROOT / ".env")
_, webhook = connection_from_env(live=True)
api = Bitrix(webhook)
queries = {
    "pharmacy_fields": ("crm.item.fields", {"entityTypeId": 1034, "useOriginalUfNames": "Y"}),
    "pharmacy_stages": ("crm.status.list", {"filter": {"ENTITY_ID": "DYNAMIC_1034_STAGE_17"}}),
    "company_fields": ("crm.company.fields", {}),
    "contact_fields": ("crm.contact.fields", {}),
    "requisite_fields": ("crm.requisite.fields", {}),
    "presets": ("crm.requisite.preset.list", {}),
    "sample_pharmacy": ("crm.item.get", {"entityTypeId": 1034, "id": 24893, "useOriginalUfNames": "Y"}),
}

def read(entry):
    key, (method, payload) = entry
    try:
        return key, api.call(method, payload)["result"]
    except RemoteError as exc:
        return key, {"error": exc.code}

with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
    data = dict(pool.map(read, queries.items()))
for preset in data.get("presets", []) if isinstance(data.get("presets"), list) else []:
    try:
        preset["fields"] = api.call("crm.requisite.preset.field.list", {"preset": {"ID": preset["ID"]}})["result"]
    except RemoteError as exc:
        preset["fields_error"] = exc.code
path = ROOT / "data" / "okb-schema.json"
path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
pharmacy = data["pharmacy_fields"].get("fields", {})
for code, field in pharmacy.items():
    title = str(field.get("title", ""))
    if code in {"title", "stageId", "companyId", "assignedById", "createdBy", "contactIds"} or re.search("бизнес|город|район|статус|ориентир|адрес|менеджер|1с|ут|ИНН", title, re.I):
        print(json.dumps({"code": code, "title": title, "type": field.get("type"), "readonly": field.get("isReadOnly"),
                          "multiple": field.get("isMultiple"), "items_count": len(field.get("items") or []), "items_sample": (field.get("items") or [])[:3]}, ensure_ascii=False))
print("STAGES", json.dumps(data["pharmacy_stages"], ensure_ascii=False))
print("PRESETS", json.dumps([{k: value for k, value in row.items() if k in {"ID", "NAME", "COUNTRY_ID", "ENTITY_TYPE_ID", "fields", "fields_error"}} for row in data.get("presets", [])] if isinstance(data.get("presets"), list) else data.get("presets"), ensure_ascii=False))
for key, wanted in (("company_fields", {"TITLE", "PHONE", "ADDRESS", "ASSIGNED_BY_ID", "ORIGIN_ID", "ORIGINATOR_ID", "COMPANY_TYPE"}),
                    ("contact_fields", {"NAME", "PHONE", "COMPANY_ID", "ASSIGNED_BY_ID", "ORIGIN_ID", "ORIGINATOR_ID"}),
                    ("requisite_fields", {"RQ_INN", "XML_ID", "CODE", "RQ_COMPANY_NAME"})):
    fields = data[key]
    print(key, json.dumps({code: field for code, field in fields.items() if code in wanted or code == "error"}, ensure_ascii=False))
sample = data["sample_pharmacy"].get("item", {})
print("SAMPLE", json.dumps({code: sample.get(code) for code in ["id", "title", "companyId", "assignedById", "createdBy", "stageId"] + [code for code, field in pharmacy.items() if re.search("бизнес|город|район|статус", str(field.get("title", "")), re.I)]}, ensure_ascii=False))
