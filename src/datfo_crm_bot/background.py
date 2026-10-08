"""Periodic CRM work with thread-owned SQLite connections, outside Telegram polling."""
from contextlib import ExitStack
from dataclasses import replace
import threading
import time

from .api import Bitrix, RemoteError, Telegram
from .communications import InboxError
from .diagnostics import diagnostic
from .i18n import LocalizedTelegram
from .live_deals import LiveDeals
from .registration import Directory, Registration
from .reminders import Reminders
from .storage import Store
from .work_inbox import WorkInbox


class TicketLocks:
    """Serialize native writes/reconciliation for one ticket across both connections."""
    def __init__(self):
        self.guard = threading.Lock()
        self.locks = {}

    def get(self, ident):
        with self.guard:
            return self.locks.setdefault(ident, threading.RLock())


class BackgroundWorker:
    def __init__(self, factory, *, interval=5):
        self.factory, self.interval = factory, interval
        self.stop = threading.Event()
        self.wakeup = threading.Event()
        self.ready = threading.Event()
        self.fatal = None
        self.thread = threading.Thread(target=self.run, name='fom-crm-background', daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.wakeup.set()
        self.thread.join(timeout=2)

    def wake(self):
        self.wakeup.set()

    def check(self):
        if self.fatal:
            raise self.fatal

    def run(self):
        while not self.stop.is_set():
            try:
                with ExitStack() as resources:
                    tasks = self.factory(resources)
                    self.ready.set()
                    diagnostic('background.ready', 0, 'ok')
                    while not self.stop.is_set():
                        self.wakeup.clear()
                        for name, task in tasks:
                            if self.stop.is_set():
                                break
                            started = time.monotonic()
                            status = 'ok'
                            try:
                                task()
                            except RemoteError as exc:
                                status = exc.code
                                if exc.code == 'TELEGRAM_401':
                                    self.fatal = exc
                                    return
                            except InboxError:
                                # A foreground change won the version check. Poll it next time.
                                status = 'CONCURRENT_CHANGE'
                            elapsed = time.monotonic() - started
                            if elapsed >= 2 or status != 'ok':
                                diagnostic('background.' + name, elapsed, status)
                        self.wakeup.wait(self.interval)
            except Exception as exc:
                # A transient background failure cannot block menus or destroy its journal.
                diagnostic('background.restart', 0, type(exc).__name__)
                self.stop.wait(self.interval)


def crm_worker(config, registration, work):
    locks = TicketLocks()
    if work.sync:
        work.sync.ticket_locks = locks
    settings = work.sync.settings if work.sync else None

    def factory(resources):
        # Every connection and service is constructed inside the worker thread.
        store = Store(config.database)
        resources.callback(store.close)
        telegram = LocalizedTelegram(Telegram(config.token), store)
        member_access = Registration(registration.settings, store,
                                     Directory(Bitrix(config.webhook)), telegram)
        member_access.okb_enabled = registration.okb_enabled
        member_access.personal_statistics_enabled = registration.personal_statistics_enabled
        live = LiveDeals(member_access, replace(config, users=dict(config.users)),
                         Bitrix(config.webhook), telegram)
        inbox = WorkInbox(member_access, telegram, live, support_settings=settings)
        if inbox.sync:
            inbox.sync.ticket_locks = locks
        reminders = Reminders(member_access, live, telegram)
        return [('requests', inbox.flush), ('reminders', reminders.flush)]

    worker = BackgroundWorker(factory)
    work.background = worker
    return worker
