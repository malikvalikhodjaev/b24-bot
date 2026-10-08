from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[2]
KINDS = ("deal", "pharmacy", "support")


class ConfigError(ValueError):
    pass


def positive_id(value, label: str) -> int:
    if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit() or int(value) < 1:
        raise ConfigError(f"{label}: требуется положительный числовой ID")
    return int(value)


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f".env, строка {number}: требуется KEY=VALUE")
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
            raise ConfigError(f".env, строка {number}: неверное имя переменной")
        os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class User:
    bitrix_id: int
    admin: bool = False
    name: str = ""


def connection_from_env(*, live: bool = False) -> tuple[str, str]:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    webhook = os.getenv("BITRIX_WEBHOOK_URL", "").strip()
    source_path = os.getenv("BITRIX_CONFIG_PATH", "").strip()
    if not webhook and source_path:
        try:
            source = json.loads(Path(source_path).read_text(encoding="utf-8-sig"))
            webhook = str(source.get("webhook_url", "")).strip()
        except (OSError, ValueError, AttributeError):
            raise ConfigError("Не удалось прочитать существующее подключение BITRIX_CONFIG_PATH") from None
    if token and not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token):
        raise ConfigError("Неверный формат TELEGRAM_BOT_TOKEN")
    if webhook:
        parsed = urlsplit(webhook)
        if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment or parsed.username or parsed.password or not re.fullmatch(r"/rest/\d+/[A-Za-z0-9_-]+/?", parsed.path):
            raise ConfigError("BITRIX_WEBHOOK_URL: нужен HTTPS URL входящего webhook Б24 без query/fragment")
        webhook = webhook.rstrip("/") + "/"
    if live and (not token or not webhook):
        raise ConfigError("Для запуска нужны TELEGRAM_BOT_TOKEN и подключение Б24 через BITRIX_WEBHOOK_URL или BITRIX_CONFIG_PATH")
    return token, webhook


@dataclass(frozen=True)
class Target:
    entity_type_id: int
    category_id: int | None
    responsible_id: int | None
    fields: dict[str, str]
    defaults: dict

    @classmethod
    def parse(cls, kind: str, data: dict) -> "Target":
        entity_id = positive_id(data.get("entity_type_id"), f"{kind}.entity_type_id")
        if (kind == "deal" and entity_id != 2) or (kind == "pharmacy" and entity_id < 128) or (kind == "support" and entity_id != 2 and entity_id < 128):
            raise ConfigError(f"{kind}: сделка имеет тип 2, аптека — тип смарт-процесса, поддержка — сделка или смарт-процесс")
        category = data.get("category_id")
        if category is not None:
            if isinstance(category, bool) or not str(category).isascii() or not str(category).isdigit():
                raise ConfigError(f"{kind}.category_id: требуется ID воронки, 0 допустим")
            category = int(category)
        if entity_id == 2 and category is None:
            raise ConfigError(f"{kind}.category_id: явно укажите воронку")
        fields = data.get("fields", {})
        required = {"title", "company_id", "responsible_id", "request_id"}
        if kind == "pharmacy":
            required.update({"address", "phone"})
        else:
            required.add("description")
        if not isinstance(fields, dict) or not required.issubset(fields):
            raise ConfigError(f"{kind}.fields: нужны поля {', '.join(sorted(required))}")
        if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", v) for v in fields.values()):
            raise ConfigError(f"{kind}.fields: неверное имя поля Б24")
        if len(set(fields.values())) != len(fields):
            raise ConfigError(f"{kind}.fields: разные данные нельзя записывать в одно поле")
        defaults = data.get("defaults", {})
        if not isinstance(defaults, dict):
            raise ConfigError(f"{kind}.defaults: требуется объект")
        if any(key in defaults for key in fields.values()):
            raise ConfigError(f"{kind}.defaults: значение пересекается с полем формы")
        if "categoryId" in defaults:
            raise ConfigError(f"{kind}: воронка задаётся только через category_id")
        responsible = data.get("responsible_id")
        if responsible is not None:
            responsible = positive_id(responsible, f"{kind}.responsible_id")
        return cls(entity_id, category, responsible, fields, defaults)


@dataclass(frozen=True)
class Config:
    token: str
    webhook: str
    database: Path
    timezone: ZoneInfo
    users: dict[int, User]
    targets: dict[str, Target]
    inn_lengths: tuple[int, ...] = (9,)

    @classmethod
    def load(cls, *, live: bool = False, settings_path: Path | None = None) -> "Config":
        load_env(ROOT / ".env")
        path = settings_path or Path(os.getenv("BOT_SETTINGS_PATH", "settings.json"))
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            raise ConfigError("Нет settings.json: требуется настроить пользователей, воронку и поля смарт-процессов")
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (ValueError, OSError) as exc:
            raise ConfigError("Не удалось прочитать JSON настроек") from exc
        if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
            raise ConfigError("settings.users: требуется объект Telegram ID → пользователь Б24")
        users = {}
        for telegram_id, item in data["users"].items():
            if not isinstance(item, dict) or not isinstance(item.get("admin", False), bool):
                raise ConfigError("Пользователь: нужен объект с bitrix_id и булевым admin")
            name = item.get("name", "")
            if not isinstance(name, str) or len(name) > 120:
                raise ConfigError("Пользователь: name должен быть строкой до 120 символов")
            users[positive_id(telegram_id, "Telegram ID")] = User(positive_id(item.get("bitrix_id"), "bitrix_id"), item.get("admin", False), name)
        if not users:
            raise ConfigError("Нужно настроить хотя бы одного пользователя; доступ по ИНН не выдаётся")
        target_data = data.get("targets")
        if not isinstance(target_data, dict):
            raise ConfigError("settings.targets: требуется объект")
        targets = {}
        for kind in KINDS:
            item = target_data.get(kind)
            if not isinstance(item, dict):
                raise ConfigError(f"Не настроен процесс {kind}")
            targets[kind] = Target.parse(kind, item)
        if targets["pharmacy"].entity_type_id == targets["support"].entity_type_id:
            raise ConfigError("Аптеки и поддержка должны иметь разные типы смарт-процессов")
        if targets["support"].entity_type_id == 2 and targets["support"].category_id == targets["deal"].category_id:
            raise ConfigError("Заявки поддержки и продажи должны иметь разные воронки")
        lengths = data.get("inn_lengths", [9])
        if not isinstance(lengths, list) or not lengths or any(type(n) is not int or n < 4 or n > 20 for n in lengths):
            raise ConfigError("inn_lengths: требуется список длин ИНН, по умолчанию [9] для Узбекистана")
        token, webhook = connection_from_env(live=live)
        try:
            timezone = ZoneInfo(os.getenv("TIMEZONE", "Asia/Tashkent"))
        except ZoneInfoNotFoundError as exc:
            raise ConfigError("Не найден часовой пояс; в Python должна быть доступна база tzdata") from exc
        database = Path(os.getenv("BOT_DB_PATH", "data/crm_bot.sqlite3"))
        if not database.is_absolute():
            database = ROOT / database
        return cls(token, webhook, database, timezone, users, targets, tuple(lengths))
