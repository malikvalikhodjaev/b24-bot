"""Read-only audit of Malik's already confirmed and completed sale."""
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from datfo_crm_bot.api import Bitrix
from datfo_crm_bot.config import load_env, connection_from_env
from datfo_crm_bot.live_deals import SOURCE

REQUEST = 'datfo-live-20f7c6a956ea43ac9ee3cb8fd8bb770f'


def main():
    load_env(ROOT / '.env')
    _, hook = connection_from_env(live=True)
    api = Bitrix(hook)
    with sqlite3.connect((ROOT / 'data/crm_bot.sqlite3').as_uri() + '?mode=ro', uri=True) as database:
        database.row_factory = sqlite3.Row
        parent = database.execute('SELECT * FROM operations WHERE request_id=?', (REQUEST,)).fetchone()
        child = database.execute('SELECT * FROM operations WHERE request_id=?', (REQUEST + '-pharmacy',)).fetchone()
        assert parent['status'] == child['status'] == 'succeeded'
        state = json.loads(parent['value'])
        saved = json.loads(parent['result'])
        pharmacy_result = json.loads(child['result'])
        assert (saved['id'], saved['pharmacy_id'], saved['activity_id']) == (140546, 24930, 351449)
        assert pharmacy_result['company_id'] == 86017 and pharmacy_result['contact_id'] == 29980
        assert database.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        with sqlite3.connect((ROOT / 'data/backups/before-finish-confirmed-sale.sqlite3').as_uri() + '?mode=ro', uri=True) as backup:
            original = backup.execute('SELECT value FROM operations WHERE request_id=?', (REQUEST,)).fetchone()[0]
        assert parent['value'] == original, 'The confirmed snapshot changed'

    deal = api.call('crm.item.get', {'entityTypeId': 2, 'id': saved['id']})['result']['item']
    pharmacy = api.call('crm.item.get', {'entityTypeId': 1034, 'id': saved['pharmacy_id'], 'useOriginalUfNames': 'Y'})['result']['item']
    activity = api.call('crm.activity.get', {'id': saved['activity_id']})['result']
    links = api.call('crm.contact.company.items.get', {'id': 29980})['result']
    assert int(deal['assignedById']) == int(pharmacy['assignedById']) == 132
    assert int(deal['categoryId']) == 45 and deal['stageId'] == 'C45:UC_SVFORP'
    assert int(deal['companyId']) == int(pharmacy['companyId']) == 86017
    assert int(deal['parentId1034']) == 24930
    assert deal['originId'] == REQUEST and deal['originatorId'] == SOURCE
    assert pharmacy['xmlId'] == REQUEST + '-pharmacy'
    assert pharmacy['stageId'] == 'DT1034_17:UC_EF7EB7'
    assert 29980 in [int(value) for value in pharmacy['contactIds']]
    assert any(int(row['COMPANY_ID']) == 86017 for row in links)
    assert int(activity['OWNER_TYPE_ID']) == 2 and int(activity['OWNER_ID']) == 140546
    assert int(activity['RESPONSIBLE_ID']) == 132
    assert datetime.fromisoformat(activity['DEADLINE']) == datetime.fromisoformat(state['deadline'])
    assert activity['SUBJECT'] == state['next_step']

    counts = {}
    for name, entity, filters in (
        ('deal', 2, {'originId': REQUEST, 'originatorId': SOURCE}),
        ('pharmacy', 1034, {'xmlId': REQUEST + '-pharmacy'}),
    ):
        rows = api.list_all('crm.item.list', {'entityTypeId': entity, 'filter': filters, 'select': ['id']}, key='items')
        counts[name] = len(rows)
    assert counts == {'deal': 1, 'pharmacy': 1}, 'Duplicate request records'
    report = {
        'ok': True, 'read_only': True, 'request': REQUEST, 'deal_id': 140546,
        'pharmacy_id': 24930, 'company_id': 86017, 'contact_id': 29980,
        'responsible_id': 132, 'deal_stage': deal['stageId'], 'pharmacy_stage': pharmacy['stageId'],
        'activity_id': 351449, 'activity_title': activity['SUBJECT'],
        'deadline': state['deadline'], 'request_counts': counts,
        'confirmed_snapshot_unchanged': True, 'sqlite_integrity': 'ok',
    }
    (ROOT / 'data/confirmed-sale-audit-20261007.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
