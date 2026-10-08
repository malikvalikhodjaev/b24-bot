"""Read-only verification of the personal overview against native CRM totals."""
from datetime import datetime
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from datfo_crm_bot.api import Bitrix
from datfo_crm_bot.config import ROOT, connection_from_env, load_env
from datfo_crm_bot.crm_statistics import CreationStatistics
from datfo_crm_bot.okb_crm import OkbSettings
from datfo_crm_bot.personal_statistics import KINDS


class ReadOnly:
    def __init__(self, hook):
        self.api, self.calls = Bitrix(hook), []

    def call(self, method, payload):
        if method not in {'crm.item.list', 'crm.contact.list', 'crm.deal.list'}:
            raise AssertionError('Only native list reads are permitted')
        result = self.api.call(method, payload)
        self.calls.append({'method': method, 'filter': payload['filter'], 'total': result.get('total')})
        return result


def main():
    load_env(ROOT / '.env')
    _, hook = connection_from_env(live=True)
    api = ReadOnly(hook)
    statistics = CreationStatistics(api, OkbSettings.load().pharmacy, 47)
    now = datetime.now(ZoneInfo('Asia/Tashkent'))
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    report = statistics.counts(start, now.isoformat(), 132, kinds=KINDS)
    result = {'checked_at': now.isoformat(), 'responsible_id': 132, 'period': 'month', 'report': report}
    errors = [f'{source}:{kind}:{cell["error"]}' for source in report
              for kind, cell in report[source].items() if 'error' in cell]
    if not errors:
        method, payload = statistics.query('pharmacy', start, now.isoformat(), 132, bot=True)
        origin_filters = payload['filter'].pop('0')
        totals = []
        for key, query in origin_filters.items():
            if key != 'logic':
                response = api.call(method, {**payload, 'filter': {**payload['filter'], **query}})
                totals.append(response['total'])
        result['individual_pharmacy_sources'] = totals
        result['pharmacy_or_matches_separate_sources'] = sum(totals) == report['bot']['pharmacy']['count']
        result['bot_is_subset'] = all(report['bot'][kind]['count'] <= report['all'][kind]['count'] for kind in KINDS)
        if not result['pharmacy_or_matches_separate_sources'] or not result['bot_is_subset']:
            errors.append('NATIVE_COUNTS_DISAGREE')
    result['read_only_calls'] = api.calls
    result['errors'] = errors
    (ROOT / 'data/personal-statistics-live-20261007.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': report, 'calls': len(api.calls), 'errors': errors}, ensure_ascii=False))
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
