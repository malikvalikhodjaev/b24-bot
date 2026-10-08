"""Read existing Sales connection and save metadata, without printing credentials."""
import concurrent.futures
import json
from pathlib import Path
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from datfo_crm_bot.api import Bitrix, RemoteError

SOURCE = Path(r"C:\Users\Lenovo\Desktop\Archive fom\Automatization\apps\sales_b24\exporter\config.json")
source = json.loads(SOURCE.read_text(encoding="utf-8-sig"))
client = Bitrix(source["webhook_url"].rstrip("/") + "/")
queries = {
    "profile": ("profile", {}),
    "types": ("crm.type.list", {}),
    "deal_fields": ("crm.item.fields", {"entityTypeId": 2, "useOriginalUfNames": "Y"}),
    "deal_categories": ("crm.category.list", {"entityTypeId": 2}),
    "pharmacy_fields": ("crm.item.fields", {"entityTypeId": 1034, "useOriginalUfNames": "Y"}),
    "pharmacy_categories": ("crm.category.list", {"entityTypeId": 1034}),
}

def read(entry):
    key, (method, payload) = entry
    try:
        return key, client.call(method, payload)["result"]
    except RemoteError as exc:
        return key, {"error": exc.code}

with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
    results = dict(pool.map(read, queries.items()))
destination = ROOT / "data" / "sales-schema.json"
destination.parent.mkdir(parents=True, exist_ok=True)
destination.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
summary = {}
for key, value in results.items():
    if isinstance(value, dict) and "fields" in value:
        summary[key] = [{"code": name, "title": meta.get("title"), "type": meta.get("type"),
                         "required": meta.get("isRequired"), "readonly": meta.get("isReadOnly"),
                         "multiple": meta.get("isMultiple"), "settings": meta.get("settings")}
                        for name, meta in value["fields"].items()
                        if key == "pharmacy_fields" or name in {"originId", "originatorId", "comments", "companyId", "categoryId", "assignedById", "title"} or meta.get("isRequired")]
    elif key == "profile":
        summary[key] = {name: value.get(name) for name in ("ID", "NAME", "LAST_NAME", "ADMIN", "error")}
    elif key == "types":
        summary[key] = [{"id": row.get("entityTypeId"), "title": row.get("title")}
                        for row in value.get("types", [])] if isinstance(value, dict) else value
    else:
        summary[key] = value
print(json.dumps(summary, ensure_ascii=False))
print("Metadata saved:", destination)
