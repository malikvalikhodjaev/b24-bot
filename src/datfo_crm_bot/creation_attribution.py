"""Immutable bot authorship from saved operations, independent of CRM assignee."""
import json


def bot_creation_ids(store, user_id):
    ids = {kind: set() for kind in ('deal', 'support', 'pharmacy', 'contact')}

    def add(kind, result):
        if not isinstance(result, dict) or result.get('existing'):
            return
        ident = (result or {}).get('id')
        if isinstance(ident, int) and not isinstance(ident, bool) and ident > 0:
            ids[kind].add(ident)

    for row in store.db.execute(
            "SELECT kind,status,result FROM operations WHERE user_id=? AND result IS NOT NULL", (user_id,)):
        result = json.loads(row['result'])
        if row['kind'] in ids and row['status'] in {'succeeded', 'created'} and not result.get('existing'):
            add(row['kind'], result)
    # A confirmed write counts even if a later step of the overall form failed.
    # Reused companies, contacts and pharmacies have status 'succeeded', not a write.
    for row in store.db.execute('''SELECT s.name,s.result FROM operation_steps s
            JOIN operations o ON o.request_id=s.request_id
            WHERE o.user_id=? AND s.status='succeeded_write' AND s.result IS NOT NULL''', (user_id,)):
        if row['name'] in ids:
            add(row['name'], json.loads(row['result']))
    tables = {row['name'] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if {'fom_requests', 'fom_request_crm'} <= tables:
        for row in store.db.execute('''SELECT c.crm_id FROM fom_request_crm c
                JOIN fom_requests r ON r.id=c.ticket WHERE r.creator=? AND c.crm_id IS NOT NULL''', (user_id,)):
            add('support', {'id': row['crm_id']})
    if {'fom_requests','fom_support_contact_steps'} <= tables:
        for row in store.db.execute('''SELECT s.result FROM fom_support_contact_steps s
                JOIN fom_requests r ON r.id=s.ticket WHERE r.creator=? AND s.phase='contact'
                AND s.state='confirmed' AND s.result IS NOT NULL''', (user_id,)):
            value = json.loads(row['result'])
            if value.get('created') is True:
                add('contact', value)
    return {kind: sorted(values) for kind, values in ids.items()}
