from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import test_work_sync as fixture
from datfo_crm_bot.api import Bitrix, RemoteError, Telegram
from datfo_crm_bot.background import BackgroundWorker, TicketLocks, crm_worker
from datfo_crm_bot.diagnostics import diagnostic
from datfo_crm_bot.registration import Registration
from datfo_crm_bot.storage import Store
from datfo_crm_bot.work_inbox import WorkStore
from datfo_crm_bot.work_sync import WorkSync


class BackgroundTests(unittest.TestCase):
    def test_slow_network_task_does_not_block_foreground_sqlite_or_menu(self):
        entered, release = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'worker.sqlite3'
            primary = Store(path)
            main_thread = threading.get_ident()
            ownership = []

            def factory(resources):
                store = Store(path)
                resources.callback(store.close)
                ownership.append(threading.get_ident())

                def slow_read():
                    store.db.execute('SELECT count(*) FROM sessions').fetchone()
                    entered.set()
                    release.wait(5)

                return [('crm', slow_read)]

            try:
                with BackgroundWorker(factory, interval=10) as worker:
                    self.assertTrue(entered.wait(3))
                    started = time.monotonic()
                    primary.set_session(123, {'step': 'preview'})
                    self.assertEqual(primary.session(123), {'step': 'preview'})
                    worker.check()
                    self.assertLess(time.monotonic() - started, .3)
                    self.assertNotEqual(ownership[0], main_thread)
                    release.set()
                self.assertFalse(worker.thread.is_alive())
            finally:
                release.set()
                primary.close()

    def test_crm_failure_does_not_skip_reminders_or_terminate_worker(self):
        reminder = threading.Event()
        def factory(resources):
            def failed():
                raise RemoteError('CONNECTION_ERROR')
            return [('requests', failed), ('reminders', reminder.set)]
        with BackgroundWorker(factory, interval=10) as worker:
            self.assertTrue(reminder.wait(3))
            worker.check()
            self.assertTrue(worker.thread.is_alive())

    def test_invalid_bot_token_is_propagated_to_main_loop(self):
        def factory(resources):
            def revoked():
                raise RemoteError('TELEGRAM_401')
            return [('reminders', revoked)]
        with BackgroundWorker(factory, interval=10) as worker:
            worker.thread.join(timeout=3)
            with self.assertRaisesRegex(RemoteError, 'TELEGRAM_401'):
                worker.check()

    def test_failed_setup_is_retried_and_resources_close_on_worker_thread(self):
        ready, closed = threading.Event(), threading.Event()
        attempts = []
        def factory(resources):
            attempts.append(threading.get_ident())
            resources.callback(closed.set)
            if len(attempts) == 1:
                raise RuntimeError('private text must not appear in log')
            return [('reminders', ready.set)]
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            with BackgroundWorker(factory, interval=.01):
                self.assertTrue(ready.wait(3))
            self.assertNotIn('private text', output.getvalue())
        self.assertTrue(closed.is_set())
        self.assertEqual(len(attempts), 2)


class ConcurrentSupportTests(unittest.TestCase):
    setUp = fixture.SupportSyncTests.setUp
    setUpBase = fixture.SupportSyncTests.setUpBase
    tearDown = fixture.SupportSyncTests.tearDown
    ticket = fixture.SupportSyncTests.ticket

    def test_foreground_flush_only_wakes_worker_and_keeps_pending_notifications(self):
        self.ticket(300)
        class Worker:
            def __init__(self):
                self.woken = False
            def wake(self):
                self.woken = True
        self.work.background = worker = Worker()
        calls = len(self.api.calls)
        self.work.flush()
        self.assertTrue(worker.woken)
        self.assertEqual(len(self.api.calls), calls)
        self.work.delivery_note(200, 1)
        self.assertFalse(self.telegram.messages)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM fom_request_outbox WHERE state='pending'").fetchone()[0], 1)

    def test_two_connections_with_stale_jobs_create_exactly_one_native_card(self):
        row = self.ticket(300)
        job = dict(self.store.db.execute('SELECT * FROM fom_request_crm_jobs WHERE ticket=?', (row['id'],)).fetchone())
        locks = TicketLocks()
        add_started, release, second_ready = threading.Event(), threading.Event(), threading.Event()
        original_call = self.api.call
        def delayed_call(method, payload):
            if method == 'crm.item.add':
                add_started.set()
                release.wait(5)
            return original_call(method, payload)
        self.api.call = delayed_call

        def submit(second=False):
            primary = Store(self.path)
            try:
                store = WorkStore(primary, self.registration.settings.admins)
                sync = WorkSync(store, self.live.crm, self.settings)
                sync.ticket_locks = locks
                if second:
                    second_ready.set()
                sync.submit(job)
            finally:
                primary.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(submit)
            self.assertTrue(add_started.wait(3))
            second = pool.submit(submit, True)
            self.assertTrue(second_ready.wait(3))
            release.set()
            first.result(timeout=3)
            second.result(timeout=3)
        self.assertEqual(sum(method == 'crm.item.add' for method, payload in self.api.calls), 1)
        self.assertEqual(self.sync.binding(row['id'])['state'], 'confirmed')

    def test_production_factory_uses_separate_connections_and_shared_ticket_locks(self):
        config = replace(self.live.crm.config, database=self.path)
        with patch('datfo_crm_bot.background.Telegram', return_value=self.telegram), \
                patch('datfo_crm_bot.background.Bitrix', return_value=self.api):
            with crm_worker(config, self.registration, self.work) as worker:
                self.assertTrue(worker.ready.wait(3))
                worker.check()
                self.assertIs(self.work.background, worker)
                self.assertIsInstance(self.work.sync.ticket_locks, TicketLocks)
        self.assertFalse(worker.thread.is_alive())


class ApiTimingTests(unittest.TestCase):
    def test_reads_have_short_timeout_and_writes_keep_uncertain_response_safety(self):
        with patch('datfo_crm_bot.api.request_json', return_value={'result': True}) as request:
            api = Bitrix('https://example.invalid/private-secret/')
            api.call('crm.item.get', {'id': 1})
            self.assertEqual(request.call_args.args[2], 12)
            api.call('crm.item.add', {'fields': {'title': 'private client'}})
            self.assertEqual(request.call_args.args[2], 40)
        with patch('datfo_crm_bot.api.request_json', side_effect=RemoteError('CONNECTION_ERROR', uncertain=True)) as request:
            with self.assertRaises(RemoteError) as failure:
                api.call('crm.item.add', {})
            self.assertTrue(failure.exception.uncertain)
            self.assertEqual(request.call_count, 1)

    def test_diagnostics_never_print_urls_payloads_or_token_and_ignore_normal_long_poll(self):
        with patch('sys.stdout', new_callable=io.StringIO) as output, \
                patch('datfo_crm_bot.api.request_json', return_value={'ok': True, 'result': []}), \
                patch('datfo_crm_bot.api.time.monotonic', side_effect=[0, 30, 31, 34]):
            api = Telegram('private-token')
            api.updates(1)
            api.call('getMe')
        self.assertNotIn('getUpdates', output.getvalue())
        self.assertIn('telegram.getMe seconds=3.000', output.getvalue())
        self.assertNotIn('private-token', output.getvalue())
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            diagnostic('https://private.invalid/credential', 2, 'https://error.invalid/secret')
        self.assertNotIn('private', output.getvalue())
        self.assertNotIn('secret', output.getvalue())


if __name__ == '__main__':
    unittest.main()
