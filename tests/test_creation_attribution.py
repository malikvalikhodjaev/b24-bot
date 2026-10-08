from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from datfo_crm_bot.creation_attribution import bot_creation_ids
from datfo_crm_bot.crm_statistics import CreationStatistics
from datfo_crm_bot.okb_crm import OkbSettings
from datfo_crm_bot.storage import Store


class AuthoredApi:
    def __init__(self):
        self.calls = []
        self.rows = [
            {'ID': 101, 'CREATED_BY_ID': 132, 'ASSIGNED_BY_ID': 133, 'CATEGORY_ID': 45, 'ORIGINATOR_ID': '', 'DATE_CREATE': '2026-10-07T10:00:00+05:00'},
            {'ID': 102, 'CREATED_BY_ID': 133, 'ASSIGNED_BY_ID': 132, 'CATEGORY_ID': 45, 'ORIGINATOR_ID': '', 'DATE_CREATE': '2026-10-07T10:00:00+05:00'},
            {'ID': 103, 'CREATED_BY_ID': 2955, 'ASSIGNED_BY_ID': 133, 'CATEGORY_ID': 45, 'ORIGINATOR_ID': 'datfo-sales-telegram', 'DATE_CREATE': '2026-10-07T10:00:00+05:00'},
            {'ID': 104, 'CREATED_BY_ID': 132, 'ASSIGNED_BY_ID': 133, 'CATEGORY_ID': 45, 'ORIGINATOR_ID': 'datfo-sales-telegram', 'DATE_CREATE': '2026-10-07T10:00:00+05:00'},
            {'ID': 105, 'CREATED_BY_ID': 2955, 'ASSIGNED_BY_ID': 132, 'CATEGORY_ID': 45, 'ORIGINATOR_ID': 'datfo-sales-telegram', 'DATE_CREATE': '2026-10-07T10:00:00+05:00'},
            {'ID': 106, 'CREATED_BY_ID': 132, 'ASSIGNED_BY_ID': 132, 'CATEGORY_ID': 47, 'ORIGINATOR_ID': '', 'DATE_CREATE': '2026-10-07T10:00:00+05:00'},
            {'ID': 107, 'CREATED_BY_ID': 2955, 'ASSIGNED_BY_ID': 133, 'CATEGORY_ID': 47, 'ORIGINATOR_ID': 'fom-support-telegram', 'DATE_CREATE': '2026-10-07T10:00:00+05:00'},
        ]

    def call(self, method, payload):
        assert method == 'crm.deal.list'
        self.calls.append((method, deepcopy(payload)))
        def matches(row):
            for key, value in payload['filter'].items():
                if key.startswith('>='):
                    if row[key[2:]] < value: return False
                elif key.startswith('<'):
                    if row[key[1:]] >= value: return False
                elif key.startswith('@'):
                    if row[key[1:]] not in value: return False
                elif key.startswith('!'):
                    if row[key[1:]] == value: return False
                elif row[key] != value:
                    return False
            return True
        selected = [row for row in self.rows if matches(row)]
        return {'result': selected[:50], 'total': len(selected)}


class CreationAttributionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.folder.name) / 'statistics.sqlite3')
        self.api = AuthoredApi()
        self.stats = CreationStatistics(self.api, OkbSettings.load().pharmacy, 47)

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()

    def saved(self, kind, ident, user=100, status='succeeded', existing=False):
        request = f'operation-{user}-{kind}-{ident}'
        self.store.prepare(user, {'request_id': request, 'kind': kind}, '2026-10-07T05:00:00Z')
        self.store.status(request, status, {'id': ident, 'existing': existing})
        return request

    def report(self, start='2026-10-01T00:00:00+05:00'):
        return self.stats.personal_counts(start, '2026-10-08T00:00:00+05:00', 132, self.store, 100,
                                          kinds=('deal', 'support'))

    def test_creator_counts_disjoint_sources_and_reassignment_does_not_change_authorship(self):
        self.saved('deal', 103)
        self.saved('deal', 104)
        self.saved('deal', 105, user=200)
        self.saved('support', 107)
        before = self.report()
        self.assertEqual(before, {'bot': {'deal': {'count': 2}, 'support': {'count': 1}},
                                 'all': {'deal': {'count': 1}, 'support': {'count': 1}}})
        for row in self.api.rows:
            row['ASSIGNED_BY_ID'] = 999
        self.assertEqual(self.report(), before)
        self.assertTrue(all('ASSIGNED_BY_ID' not in payload['filter'] for _, payload in self.api.calls))

    def test_native_deletion_and_creation_period_are_used_for_bot_records(self):
        self.saved('deal', 103)
        self.assertEqual(self.report()['bot']['deal']['count'], 1)
        self.assertEqual(self.report('2026-10-07T11:00:00+05:00')['bot']['deal']['count'], 0)
        self.api.rows = [row for row in self.api.rows if row['ID'] != 103]
        self.assertEqual(self.report()['bot']['deal']['count'], 0)

    def test_reused_entities_are_not_bot_creations_and_partial_writes_are_deduplicated(self):
        request = self.saved('pharmacy', 801, status='existing', existing=True)
        self.store.set_operation_step(request, 'pharmacy', 'succeeded', {'id': 801})
        self.store.set_operation_step(request, 'contact', 'succeeded', {'id': 802})
        self.assertEqual(bot_creation_ids(self.store, 100)['pharmacy'], [])
        self.store.set_operation_step(request, 'pharmacy', 'succeeded_write', {'id': 801, 'existing': True})
        self.assertEqual(bot_creation_ids(self.store, 100)['pharmacy'], [])
        self.assertEqual(bot_creation_ids(self.store, 100)['contact'], [])
        self.store.set_operation_step(request, 'contact', 'succeeded_write', {'id': 803})
        partial = self.saved('deal', 103, status='uncertain')
        self.store.set_operation_step(partial, 'deal', 'succeeded_write', {'id': 103})
        self.assertEqual(bot_creation_ids(self.store, 100)['contact'], [803])
        self.assertEqual(self.report()['bot']['deal']['count'], 1)
        self.store.status(partial, 'succeeded', {'id': 103})
        self.assertEqual(bot_creation_ids(self.store, 100)['deal'], [103])

    def test_support_uses_original_ticket_creator_after_handoff(self):
        self.store.db.executescript('''CREATE TABLE fom_requests(id INTEGER PRIMARY KEY,creator INTEGER,owner INTEGER);
            CREATE TABLE fom_request_crm(ticket INTEGER PRIMARY KEY,crm_id INTEGER);
            INSERT INTO fom_requests VALUES(1,100,200);
            INSERT INTO fom_request_crm VALUES(1,107);''')
        self.assertEqual(self.report()['bot']['support']['count'], 1)
        self.store.db.execute('UPDATE fom_requests SET owner=300')
        self.assertEqual(bot_creation_ids(self.store, 100)['support'], [107])
        self.assertEqual(bot_creation_ids(self.store, 300)['support'], [])

    def test_no_bot_ids_is_zero_without_an_unscoped_origin_query(self):
        result = self.report()
        self.assertEqual(result['bot'], {'deal': {'count': 0}, 'support': {'count': 0}})
        self.assertEqual(len(self.api.calls), 4)
        self.assertTrue(all(payload['filter'].get('CREATED_BY_ID') == 132 for _, payload in self.api.calls))


if __name__ == '__main__':
    unittest.main()
