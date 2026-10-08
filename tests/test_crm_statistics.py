from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from datfo_crm_bot.api import RemoteError
from datfo_crm_bot.crm_statistics import CreationStatistics
from datfo_crm_bot.demo import DemoCrm, demo_config
from datfo_crm_bot.i18n import language_context
from datfo_crm_bot.okb_crm import OkbSettings
from datfo_crm_bot.region_cities import RegionCities
from datfo_crm_bot.service import Bot
from datfo_crm_bot.storage import Store


class CountingApi:
    def __init__(self, totals=(1, 2, 3, 125, 260, 410)):
        self.calls, self.totals = [], list(totals)

    def call(self, method, payload):
        self.calls.append((method, deepcopy(payload)))
        total = self.totals[len(self.calls)-1]
        if isinstance(total, RemoteError):
            raise total
        return {'result': [] if method == 'crm.deal.list' else {'items': []}, 'total': total}


class CreationStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.api = CountingApi()
        self.target = OkbSettings.load().pharmacy
        self.stats = CreationStatistics(self.api, self.target, 47)
        self.start, self.end = '2026-10-06T00:00:00+05:00', '2026-10-06T16:00:00+05:00'

    def test_native_total_counts_more_than_one_page_and_uses_same_period(self):
        report = self.stats.counts(self.start, self.end)
        self.assertEqual(report['all']['pharmacy']['count'], 260)
        self.assertEqual(report['bot']['support']['count'], 3)
        self.assertEqual(len(self.api.calls), 6)
        for method, payload in self.api.calls:
            date = 'createdTime' if method == 'crm.item.list' else 'DATE_CREATE'
            self.assertEqual(payload['filter']['>=' + date], '2026-10-06 00:00:00' if method == 'crm.item.list' else self.start)
            self.assertEqual(payload['filter']['<' + date], '2026-10-06 16:00:00' if method == 'crm.item.list' else self.end)
            self.assertNotIn('ASSIGNED_BY_ID', payload['filter'])
            self.assertNotIn('assignedById', payload['filter'])

    def test_bot_origin_and_pharmacy_reuse_and_support_pipeline_are_distinct(self):
        self.stats.counts(self.start, self.end)
        queries = [payload['filter'] for method, payload in self.api.calls]
        self.assertEqual(queries[0]['!CATEGORY_ID'], 47)
        self.assertEqual(queries[0]['ORIGINATOR_ID'], 'datfo-sales-telegram')
        self.assertEqual(queries[1]['0'], {'logic': 'OR', '0': {'%xmlId': 'datfo-okb-'},
                         '1': {'%xmlId': 'datfo-live-'}, '2': {'%xmlId': 'datfo-form-'},
                         '3': {'%xmlId': 'fom-request-'}})
        self.assertEqual(queries[1]['categoryId'], 17)
        self.assertEqual(queries[2]['CATEGORY_ID'], 47)
        self.assertEqual(queries[2]['@ORIGINATOR_ID'], ['fom-support-telegram', 'datfo-sales-telegram'])
        self.assertNotIn('ORIGINATOR_ID', queries[3])
        self.assertNotIn('%xmlId', queries[4])
        self.assertNotIn('@ORIGINATOR_ID', queries[5])

    def test_personal_filters_use_actual_responsible_on_both_sources(self):
        self.stats.counts(self.start, self.end, 132)
        for method, payload in self.api.calls:
            field = 'assignedById' if method == 'crm.item.list' else 'ASSIGNED_BY_ID'
            self.assertEqual(payload['filter'][field], 132)

    def test_universal_date_filter_uses_portal_time_across_utc_midnight(self):
        _, payload = self.stats.query('pharmacy', '2026-10-06T19:00:00+00:00',
                                     '2026-10-07T02:38:11.352253+00:00', 132, bot=True)
        self.assertEqual(payload['filter']['>=createdTime'], '2026-10-07 00:00:00')
        self.assertEqual(payload['filter']['<createdTime'], '2026-10-07 07:38:11')

    def test_partial_error_and_invalid_total_are_unavailable_not_zero(self):
        self.api.totals = [RemoteError('ACCESS_DENIED'), 2, True, 0, '50', 410]
        report = self.stats.counts(self.start, self.end)
        self.assertEqual(report['bot']['deal'], {'error': 'ACCESS_DENIED'})
        self.assertEqual(report['bot']['support'], {'error': 'INVALID_STATISTICS_TOTAL'})
        self.assertEqual(report['all']['pharmacy'], {'error': 'INVALID_STATISTICS_TOTAL'})
        self.assertEqual(report['all']['deal'], {'count': 0})
        self.assertEqual(report['all']['support'], {'count': 410})

    def test_report_renders_both_sources_with_native_timezone_and_no_team_leak(self):
        with tempfile.TemporaryDirectory() as folder:
            config = demo_config(Path(folder) / 'stats.sqlite3')
            store = Store(config.database)
            try:
                now = datetime.fromisoformat('2026-10-06T12:30:00+05:00')
                bot = Bot(config, store, DemoCrm(), None, clock=lambda: now)
                bot.creation_statistics = self.stats
                self.api.totals = [130, 5, 265, 5, 415, 5]
                text, _, state = bot.stats(2, {'scope': 'team'}, 'Сегодня')
                self.assertIn('🤖 Через бот', text)
                self.assertIn('🏢 Создано вами в Б24', text)
                self.assertIn('Сделки: 125', text)
                self.assertIn('06.10.2026 00:00', text)
                self.assertIsNone(state)
                for method, payload in self.api.calls:
                    key = 'createdBy' if method == 'crm.item.list' else 'CREATED_BY_ID'
                    self.assertEqual(payload['filter'][key], 20)
                self.api.calls.clear()
                self.api.totals = [1, 2, 3, 125, 260, 410]
                with language_context('uz'):
                    text, _, _ = bot.stats(1, {'scope': 'team'}, 'Сегодня')
                self.assertIn('🤖 Бот орқали', text)
                self.assertIn('🏢 Б24да жами', text)
                self.assertIn('Сделкалар:', text)
            finally:
                store.close()

    def test_missing_city_does_not_silently_fall_back_to_global_list(self):
        links = RegionCities()
        with self.assertRaises(ValueError):
            links.choices('4559', [{'id': '4572', 'title': 'Андижан'}])
        with self.assertRaises(ValueError):
            links.validate({'business_region': {'id': '4559'}, 'city': {'id': '4572'}})


if __name__ == '__main__':
    unittest.main()
