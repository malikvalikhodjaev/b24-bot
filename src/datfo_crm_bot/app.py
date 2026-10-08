from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import replace
import html
import os
from pathlib import Path
import tempfile
import time
from zoneinfo import ZoneInfo

from .api import Bitrix, RemoteError, Telegram
from .config import Config, ConfigError, ROOT, connection_from_env, load_env
from .crm import Crm
from .demo import ConsoleTelegram, DemoCrm, demo_config
from .service import Bot
from .registration import Directory, Registration, RegistrationSettings
from .okb_access import OkbAccess
from .okb_crm import OkbCrm, OkbSettings
from .okb_service import OkbBot
from .simulation import simulation_for
from .storage import Store, process_lock
from .communications import inbox_test_for
from .guidance import Guide
from .live_deals import LiveDeals, LiveCrm
from .work_inbox import WorkInbox
from .work_sync import SupportSettings
from .i18n import LocalizedTelegram, language_context, stored_language, tr
from .language_ui import LanguageUI
from .navigation import Navigation
from .personal_statistics import PersonalStatistics
from .crm_statistics import CreationStatistics
from .background import crm_worker
from .diagnostics import diagnostic
from .hosting import assert_polling_location

COMMANDS = [{"command": command, "description": description} for command, description in (
    ("start", "Меню Datfo"), ("deal", "Открыть сделку"), ("pharmacy", "Добавить аптеку"),
    ("support", "Открыть заявку в техподдержку"), ("stats", "Создано через бот и всего в Б24"), ("pending", "Проверить сохранение"),
    ("my_stats", "Моя краткая статистика: бот и Б24"),
    ("cancel", "Отменить черновик"), ("whoami", "Ваш Telegram ID"), ("help", "Помощь по ролям"),
    ("guide", "Помощь по ролям"), ("next", "Подсказка для текущего шага"),
    ("register", "Подключиться к Datfo Sales"), ("profile", "👤 Мой профиль"),
    ("registrations", "Заявки менеджеров, для Малика"), ("members", "Подключённая команда, для Малика"),
    ("test", "Тест без записи в Б24, для Малика"), ("endtest", "Завершить симуляцию"))]
COMMANDS.append({"command": "okb", "description": "Добавить аптеку в ОКБ"})
COMMANDS.extend([{"command":"inbox_test","description":"Тест инбокса и ролей, для Малика"},
                 {"command":"end_inbox_test","description":"Завершить тест инбокса"}])
COMMANDS.extend([{'command':'b24_test','description':'Тест с настоящими записями Б24'},
                 {'command':'end_b24_test','description':'Закончить тест с записью Б24'},
                 {'command':'my_crm','description':'📂 Мои сделки и заявки'},
                 {'command':'reminders','description':'Мои напоминания'},
                 {'command':'crm_deal','description':'Открыть созданную ботом запись Б24'}])
COMMANDS.extend([{'command':command,'description':description} for command,description in (
    ('request','Отправить заявку в техподдержку'), ('requests','Заявки в техподдержку'),
    ('requests_mine','Назначенные мне заявки'), ('requests_sent','Отправленные мной заявки'),
    ('request_ticket','Открыть заявку по номеру'), ('request_cancel','Отменить черновик заявки'),
    ('requests_stats','Статистика заявок'), ('diagnostics','Диагностика ФОМ: ошибки и обучение'),
    ('inbox_team','Настроить роли ФОМ, для Малика'),
    ('inbox_role','Назначить роль участника, для Малика'), ('requests_resend','Повтор уведомлений, для Малика'))])
COMMANDS = [row for row in COMMANDS if row['command'] not in {
    'test','endtest','inbox_test','end_inbox_test','b24_test','end_b24_test',
    'registrations','members','inbox_team','inbox_role','requests_resend'}]


def publish_commands(telegram, commands):
    telegram.call('setMyCommands', {'commands': commands})
    for language in ('ru', 'uz'):
        telegram.call('setMyCommands', {'commands': [{**row, 'description': tr(row['description'], language)}
                     for row in commands], 'language_code': language})


def dispatch(update, language_ui, handlers):
    started = time.monotonic()
    try:
        return dispatch_message(update, language_ui, handlers)
    finally:
        elapsed = time.monotonic() - started
        if elapsed >= 2:
            diagnostic('telegram.update', elapsed, 'handled')


def dispatch_message(update, language_ui, handlers):
    if language_ui.handle(update):
        return
    callback = update.get('callback_query')
    sender = callback.get('from', {}) if isinstance(callback, dict) else update.get('message', {}).get('from', {})
    with language_context(stored_language(language_ui.store, sender.get('id'))):
        normalized = language_ui.normalize(update)
        for handler in handlers:
            if handler(normalized):
                break


def registration_only(*, check: bool = False, okb_enabled: bool = False) -> int:
    load_env(ROOT / ".env")
    if not check:
        assert_polling_location()
    token, webhook = connection_from_env(live=True)
    settings = RegistrationSettings.load()
    telegram = Telegram(token)
    me = telegram.call("getMe")
    if str(me.get("username", "")).lower() != "fom_bitrix_bot":
        raise ConfigError("Токен должен относиться к @fom_bitrix_bot")
    if telegram.call("getWebhookInfo").get("url"):
        raise ConfigError("У бота уже настроен webhook; регистрация не запущена")
    directory = Directory(Bitrix(webhook))
    if check:
        for user in settings.admins.values():
            employee = directory.employee(user.bitrix_id)
            if not employee or employee["name"].casefold() != user.name.casefold():
                raise ConfigError("Учётная запись администратора не подтверждена в Б24")
        if okb_enabled:
            okb_settings = OkbSettings.load()
            config = Config(token, webhook, ROOT / "data" / "crm_bot.sqlite3", ZoneInfo(os.getenv("TIMEZONE", "Asia/Tashkent")), {}, {"pharmacy": okb_settings.pharmacy})
            OkbCrm(config, Bitrix(webhook), okb_settings).validate_okb()
            live_crm = LiveCrm(config,Bitrix(webhook))
            live_crm.categories()
            live_crm.sales_destination()
        print("@fom_bitrix_bot: администратор и выбранные процессы подтверждены. Выполнено только чтение.")
        return 0
    database = Path(os.getenv("BOT_DB_PATH", "data/crm_bot.sqlite3"))
    if not database.is_absolute():
        database = ROOT / database
    with process_lock(database):
        store = Store(database)
        try:
            telegram = LocalizedTelegram(telegram, store)
            registration = Registration(settings, store, directory, telegram)
            registration.bootstrap()
            language_ui = LanguageUI(registration, telegram)
            okb = None
            if okb_enabled:
                okb_settings = OkbSettings.load()
                okb_config = Config(token, webhook, database, ZoneInfo(os.getenv("TIMEZONE", "Asia/Tashkent")), {}, {"pharmacy": okb_settings.pharmacy})
                okb_crm = OkbCrm(okb_config, Bitrix(webhook), okb_settings)
                okb_crm.validate_okb()
                okb = OkbAccess(registration, okb_config, okb_crm, telegram)
            available = {"start", "settings", "language", "register", "profile", "registrations", "members", "whoami", "cancel", "test", "endtest", "inbox_test", "end_inbox_test", "guide", "help", "next"}
            if okb:
                available.update({"okb", "stats", "my_stats", "pending", "deal", "support", "b24_test", "end_b24_test", "my_crm", "crm_deal", "reminders"})
                available.update({'request','requests','requests_mine','requests_sent','request_ticket','request_cancel',
                                  'requests_stats','diagnostics','inbox_team','inbox_role','requests_resend'})
            commands = [item for item in COMMANDS if item["command"] in available]
            registration.commands = commands
            publish_commands(telegram, registration.command_menu(None, commands) if okb else commands)
            print("@fom_bitrix_bot: регистрация запущена. ОКБ: " + ("включено" if okb else "выключено") + ". Сделки: продажи KG / План. Заявки: отдельная форма поддержки.", flush=True)
            with simulation_for(registration, telegram) as simulation, inbox_test_for(registration,telegram,simulation) as inbox, ExitStack() as background_resources:
                live = LiveDeals(registration,okb_config,Bitrix(webhook),telegram,simulation,inbox) if okb else None
                from .reminders import Reminders
                reminders = Reminders(registration, live, telegram) if live else None
                work = WorkInbox(registration,telegram,live,simulation,inbox,SupportSettings.load()) if live else None
                personal_stats = None
                if work and okb:
                    statistics = CreationStatistics(Bitrix(webhook), okb_settings.pharmacy, work.sync.settings.category_id)
                    okb.bot.creation_statistics = statistics
                    live.bot.creation_statistics = statistics
                    personal_stats = PersonalStatistics(registration, telegram, statistics, simulation=simulation, inbox=inbox)
                guide = Guide(registration, telegram, inbox, simulation, work)
                navigation = Navigation(registration, telegram, simulation, inbox)
                from .role_access import RoleAccess
                role_access = RoleAccess(registration, telegram)
                for member in store.registrations('approved'):
                    registration.refresh_commands(member['telegram_id'])
                background = background_resources.enter_context(crm_worker(okb_config, registration, work)) if work else None
                print("Тестовая симуляция /test доступна администратору; данные хранятся отдельно от CRM.", flush=True)
                print("/inbox_test: два учебных профиля, общий инбокс, захват и диспетчерское назначение. Б24 не изменяется.", flush=True)
                if live:
                    print("/b24_test: реальные тестовые сделки/заявки; /my_crm: связи и текущие стадии Б24.",flush=True)
                    print('/requests: рабочие заявки менеджеров ФОМ, адресат, ссылка на Б24 и история передач.',flush=True)
                while True:
                    try:
                        if background:
                            background.check()
                        for update in telegram.updates(store.offset()):
                            handlers = [role_access.handle] + ([personal_stats.handle] if personal_stats else []) + ([reminders.handle] if reminders else []) + [guide.handle, navigation.handle] + ([work.handle] if work else []) + ([live.handle] if live else []) + [inbox.handle, simulation.handle] + ([okb.handle] if okb else []) + [registration.handle]
                            dispatch(update, language_ui, handlers)
                            store.set_offset(int(update["update_id"]) + 1)
                        simulation.store.prune_deliveries(store.offset())
                        inbox.store.prune_updates(store.offset())
                        if work:
                            work.store.prune(store.offset())
                    except RemoteError as exc:
                        if exc.code in {"TELEGRAM_401", "TELEGRAM_403", "TELEGRAM_409"}:
                            raise
                        print("Временная ошибка: " + exc.code, flush=True)
                        time.sleep(5)
        finally:
            store.close()


def identify_only() -> int:
    """Collect participant IDs without granting access or loading any CRM credentials."""
    load_env(ROOT / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("Токен не настроен")
    telegram = Telegram(token)
    me = telegram.call("getMe")
    if str(me.get("username", "")).lower() != "fom_bitrix_bot":
        raise ConfigError("Токен должен относиться к @fom_bitrix_bot")
    if telegram.call("getWebhookInfo").get("url"):
        raise ConfigError("У бота уже настроен webhook; режим получения ID не запущен")
    database = ROOT / "data" / "crm_bot.sqlite3"
    with process_lock(database):
        store = Store(database)
        try:
            print("@fom_bitrix_bot: включён режим получения Telegram ID. Доступ к CRM не выдаётся.", flush=True)
            while True:
                try:
                    for update in telegram.updates(store.offset()):
                        message = update.get("message", {})
                        sender, chat = message.get("from", {}), message.get("chat", {})
                        if chat.get("type") == "private" and sender.get("id") and not sender.get("is_bot"):
                            store.remember_identity(sender)
                            text = "Ваш Telegram ID: <code>" + html.escape(str(sender["id"])) + "</code>.\nПодключение Datfo Sales готовится. Передайте этот ID для настройки доступа менеджера."
                            telegram.send(int(chat["id"]), text)
                        store.set_offset(int(update["update_id"]) + 1)
                except RemoteError as exc:
                    if exc.code in {"TELEGRAM_401", "TELEGRAM_403", "TELEGRAM_409"}:
                        raise
                    print("Временная ошибка Telegram: " + exc.code, flush=True)
                    time.sleep(5)
        finally:
            store.close()


def demo(script: Path | None, check: bool = False) -> int:
    with tempfile.TemporaryDirectory(prefix="datfo-crm-demo-") as folder:
        config = demo_config(Path(folder) / "demo.sqlite3")
        store = Store(config.database)
        try:
            print("ОФЛАЙН-ДЕМО. Все компании вымышленные; подключения к Telegram и Б24 нет.")
            if check:
                print("Схема базы и конфигурация демонстрации: OK")
                return 0
            bot = Bot(config, store, DemoCrm(), ConsoleTelegram())
            bot.handle({"update_id": 0, "message": {"from": {"id": 1}, "chat": {"id": 1, "type": "private"}, "text": "/start"}})
            inputs = iter(script.read_text(encoding="utf-8-sig").splitlines()) if script else None
            update_id = 0
            print("Учебные ИНН: 123456789 — одна компания, 987654321 — две. Для выхода /exit.")
            while True:
                try:
                    text = next(inputs) if inputs is not None else input("\nВЫ: ")
                except (StopIteration, EOFError, KeyboardInterrupt):
                    break
                if inputs is not None:
                    print("\nВЫ:", text)
                if text == "/exit":
                    break
                update_id += 1
                bot.handle({"update_id": update_id, "message": {"from": {"id": 1}, "chat": {"id": 1, "type": "private"}, "text": text}})
        finally:
            store.close()
    return 0


def run(config: Config, *, online_check: bool = False) -> int:
    if not online_check:
        assert_polling_location()
    telegram = Telegram(config.token)
    identity = telegram.call("getMe")
    if str(identity.get("username", "")).lower() != "fom_bitrix_bot":
        raise ConfigError("Токен относится к другому боту. Требуется токен @fom_bitrix_bot")
    webhook = telegram.call("getWebhookInfo")
    if webhook.get("url"):
        raise ConfigError("У @fom_bitrix_bot уже настроен webhook; существующее подключение нужно сначала согласованно переключить")
    okb_settings = OkbSettings.load()
    config = replace(config, targets={**config.targets, "pharmacy": okb_settings.pharmacy})
    crm = OkbCrm(config, Bitrix(config.webhook), okb_settings)
    for kind in config.targets:
        crm.validate(kind)
    crm.validate_okb()
    if online_check:
        print("@fom_bitrix_bot: имя подтверждено. Поля и воронки Б24 доступны. Выполнено только чтение.")
        return 0
    with process_lock(config.database), ExitStack() as resources:
        store = Store(config.database)
        try:
            telegram = LocalizedTelegram(telegram, store)
            publish_commands(telegram, COMMANDS)
            bot = OkbBot(config, store, crm, telegram)
            registration = None
            if (ROOT / "registration.json").exists():
                registration = Registration(RegistrationSettings.load(), store, Directory(Bitrix(config.webhook)), telegram)
                registration.bootstrap()
            language_ui = LanguageUI(registration, telegram) if registration else None
            simulation = resources.enter_context(simulation_for(registration, telegram)) if registration else None
            inbox = resources.enter_context(inbox_test_for(registration,telegram,simulation)) if registration else None
            live = LiveDeals(registration,config,Bitrix(config.webhook),telegram,simulation,inbox) if registration else None
            from .reminders import Reminders
            reminders = Reminders(registration, live, telegram) if live else None
            work = WorkInbox(registration,telegram,live,simulation,inbox,SupportSettings.load()) if live else None
            personal_stats = None
            if work:
                statistics = CreationStatistics(Bitrix(config.webhook), okb_settings.pharmacy, work.sync.settings.category_id)
                bot.creation_statistics = live.bot.creation_statistics = statistics
                personal_stats = PersonalStatistics(registration, telegram, statistics, simulation=simulation, inbox=inbox)
            guide = Guide(registration, telegram, inbox, simulation, work) if registration else None
            navigation = Navigation(registration, telegram, simulation, inbox) if registration else None
            background = resources.enter_context(crm_worker(config, registration, work)) if work else None
            print("@fom_bitrix_bot запущен. Остановка: Ctrl+C.")
            while True:
                try:
                    if background:
                        background.check()
                    updates = telegram.updates(store.offset())
                    for update in updates:
                        if not isinstance(update, dict) or "update_id" not in update:
                            raise RemoteError("INVALID_UPDATE")
                        if registration:
                            handlers = ([personal_stats.handle] if personal_stats else []) + ([reminders.handle] if reminders else []) + ([guide.handle, navigation.handle] if guide else []) + ([work.handle] if work else []) + ([live.handle] if live else []) + ([inbox.handle] if inbox else []) + ([simulation.handle] if simulation else []) + [lambda value: registration.handle(value, bot)]
                            dispatch(update, language_ui, handlers)
                        else:
                            bot.handle(update)
                        store.set_offset(int(update["update_id"]) + 1)
                    store.prune_deliveries(store.offset())
                    if simulation:
                        simulation.store.prune_deliveries(store.offset())
                    if inbox:
                        inbox.store.prune_updates(store.offset())
                    if work:
                        work.store.prune(store.offset())
                except RemoteError as exc:
                    if exc.code in {"TELEGRAM_401", "TELEGRAM_403", "TELEGRAM_409"}:
                        raise
                    print(f"Запрос временно не завершён ({exc.code}); следующая попытка через 5 секунд.")
                    time.sleep(5)
        finally:
            store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Datfo: сделки, аптеки, техподдержка и статистика через Telegram")
    parser.add_argument("--check", action="store_true", help="локальная проверка настроек без сетевых запросов")
    parser.add_argument("--check-online", action="store_true", help="проверка имени бота и полей Б24, только чтение")
    parser.add_argument("--demo", action="store_true", help="оффлайн-демонстрация на вымышленных данных")
    parser.add_argument("--identify-only", action="store_true", help="получение Telegram ID перед настройкой доступа, без CRM")
    parser.add_argument("--registration-only", action="store_true", help="самостоятельная регистрация менеджеров, без создания карточек CRM")
    parser.add_argument("--sales-bot", action="store_true", help="регистрация и добавление в ОКБ с записью в Б24 после подтверждения")
    parser.add_argument("--script", type=Path, help="текстовый файл с сообщениями для оффлайн-демо")
    args = parser.parse_args(argv)
    try:
        if args.sales_bot:
            if args.demo or args.check or args.identify_only or args.registration_only or args.script:
                raise ConfigError("--sales-bot используется отдельно либо с --check-online")
            return registration_only(check=args.check_online, okb_enabled=True)
        if args.registration_only:
            if args.demo or args.check or args.identify_only or args.script:
                raise ConfigError("--registration-only используется отдельно либо с --check-online")
            return registration_only(check=args.check_online)
        if args.identify_only:
            if args.demo or args.check or args.check_online or args.script:
                raise ConfigError("--identify-only используется отдельно")
            return identify_only()
        if args.script and not args.demo:
            raise ConfigError("--script используется только с --demo")
        if args.demo and args.check_online:
            raise ConfigError("--demo не выполняет онлайн-проверки")
        if args.demo:
            return demo(args.script, args.check)
        config = Config.load(live=not args.check)
        if args.check:
            print(f"Локальная конфигурация: OK; пользователей: {len(config.users)}; процессов: {len(config.targets)}.")
            print("Доступность Б24 и имя бота проверяются отдельно через --check-online.")
            return 0
        return run(config, online_check=args.check_online)
    except KeyboardInterrupt:
        print("Бот остановлен.")
        return 0
    except (ConfigError, RemoteError, RuntimeError, OSError) as exc:
        # ConfigError messages contain field names, never secret values. Never print URL-bearing OSError text.
        message = str(exc) if isinstance(exc, (ConfigError, RemoteError, RuntimeError)) else "Не удалось прочитать или записать локальный файл"
        print("Ошибка:", message)
        return 2
