"""Active technicians from the verified Bitrix company structure, without contact data."""
from copy import deepcopy
import threading
import time

from .api import RemoteError
from .config import positive_id


SUPPORT_DEPARTMENT = 17
REGIONAL_DEPARTMENTS = {60: 1, 61: 2, 62: 3, 63: 4}


class TechnicianDirectory:
    def __init__(self, api, *, ttl=300):
        self.api, self.ttl = api, ttl
        self.lock = threading.RLock()
        self.cached, self.checked_at = None, 0

    def departments(self):
        rows = self.api.list_all('department.get', {})
        by_id = {positive_id(row.get('ID'), 'Department ID'): row for row in rows}
        if SUPPORT_DEPARTMENT not in by_id:
            raise RemoteError('FOM_TECHNICIAN_STRUCTURE_CHANGED')
        selected = {}
        for ident, region in REGIONAL_DEPARTMENTS.items():
            row = by_id.get(ident)
            if not row or str(row.get('PARENT')) != str(SUPPORT_DEPARTMENT):
                raise RemoteError('FOM_TECHNICIAN_STRUCTURE_CHANGED')
            selected[ident] = region
        # Follow child departments, preserving the regional parent association.
        while True:
            added = {ident: selected[int(row['PARENT'])] for ident, row in by_id.items()
                     if ident not in selected and str(row.get('PARENT', '')).isdigit()
                     and int(row['PARENT']) in selected}
            if not added:
                return selected
            selected.update(added)

    @staticmethod
    def parse(row, departments):
        if row.get('ACTIVE') not in (True, 'Y', 'true', 1) or row.get('USER_TYPE') != 'employee':
            return None
        values = row.get('UF_DEPARTMENT')
        if not isinstance(values, list):
            return None
        ids = sorted({int(value) for value in values if str(value).isdigit() and int(value) in departments})
        name = ' '.join(str(row.get(key) or '').strip() for key in ('NAME', 'LAST_NAME')).strip()
        if not ids or not name:
            return None
        return {'bitrix_id': positive_id(row.get('ID'), 'Employee ID'), 'name': name,
                'department_ids': ids, 'regions': sorted({departments[ident] for ident in ids})}

    def listing(self, *, force=False):
        with self.lock:
            if not force and self.cached is not None and time.monotonic() - self.checked_at < self.ttl:
                return deepcopy(self.cached)
            departments = self.departments()
            rows = self.api.list_all('user.get', {'FILTER': {'ACTIVE': True, 'USER_TYPE': 'employee',
                'UF_DEPARTMENT': sorted(departments)},
                'select': ['ID', 'ACTIVE', 'NAME', 'LAST_NAME', 'USER_TYPE', 'UF_DEPARTMENT']})
            users = {}
            for row in rows:
                person = self.parse(row, departments)
                if person:
                    if person['bitrix_id'] in users and users[person['bitrix_id']] != person:
                        raise RemoteError('FOM_TECHNICIAN_LIST_INVALID')
                    users[person['bitrix_id']] = person
            self.cached = sorted(users.values(), key=lambda row: (row['name'].casefold(), row['bitrix_id']))
            self.checked_at = time.monotonic()
            return deepcopy(self.cached)

    def employee(self, ident):
        """Fresh activity and department checks before assignment, never a stale cache."""
        departments = self.departments()
        rows = self.api.list_all('user.get', {'FILTER': {'ID': positive_id(ident, 'Employee ID'),
            'ACTIVE': True, 'USER_TYPE': 'employee'},
            'select': ['ID', 'ACTIVE', 'NAME', 'LAST_NAME', 'USER_TYPE', 'UF_DEPARTMENT']})
        matches = [person for row in rows if (person := self.parse(row, departments))
                   and person['bitrix_id'] == int(ident)]
        if len(matches) != 1:
            raise RemoteError('FOM_TECHNICIAN_CHANGED')
        return matches[0]


def search(rows, query='', region=None):
    terms = str(query).casefold().replace('ё', 'е').split()
    return [deepcopy(row) for row in rows if (region is None or region in row['regions'])
            and all(term in row['name'].casefold().replace('ё', 'е') for term in terms)]
