"""Counts of existing CRM records by native creation date and bot origin."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .api import RemoteError
from .config import positive_id


class CreationStatistics:
    def __init__(self, api, pharmacy, support_category):
        self.api, self.pharmacy = api, pharmacy
        self.support_category = positive_id(support_category, 'Воронка техподдержки')

    def query(self, kind, start, end, responsible_id, *, bot):
        if kind == 'pharmacy':
            # This portal's universal CRM filter treats ISO offsets as part of
            # the date text. Use its verified local SQL datetime format instead;
            # legacy deals/contacts correctly accept timezone-aware ISO below.
            portal_zone = ZoneInfo('Asia/Tashkent')
            def local_date(value):
                moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=portal_zone)
                return moment.astimezone(portal_zone).strftime('%Y-%m-%d %H:%M:%S')
            filters = {'<createdTime': local_date(end),
                       'categoryId': self.pharmacy.category_id}
            if start is not None:
                filters['>=createdTime'] = local_date(start)
            if responsible_id is not None:
                filters[self.pharmacy.fields['responsible_id']] = responsible_id
            if bot:
                # Standalone OKB, sales and support forms all create pharmacies.
                # Reusing an existing pharmacy never changes its native origin.
                field = '%' + self.pharmacy.fields['request_id']
                filters['0'] = {'logic': 'OR', **{
                    str(index): {field: prefix} for index, prefix in enumerate(
                        ('datfo-okb-', 'datfo-live-', 'datfo-form-', 'fom-request-'))}}
            return 'crm.item.list', {'entityTypeId': self.pharmacy.entity_type_id,
                'useOriginalUfNames': 'Y', 'select': ['id'], 'filter': filters, 'start': 0}
        filters = {'<DATE_CREATE': end}
        if start is not None:
            filters['>=DATE_CREATE'] = start
        if kind == 'contact':
            if responsible_id is not None:
                filters['ASSIGNED_BY_ID'] = responsible_id
            if bot:
                filters['ORIGINATOR_ID'] = 'datfo_telegram_okb'
            return 'crm.contact.list', {'select': ['ID'], 'filter': filters, 'start': 0}
        if kind not in {'deal', 'support'}:
            raise ValueError('Unknown statistics entity')
        filters['CATEGORY_ID' if kind == 'support' else '!CATEGORY_ID'] = self.support_category
        if responsible_id is not None:
            filters['ASSIGNED_BY_ID'] = responsible_id
        if bot:
            if kind == 'support':
                filters['@ORIGINATOR_ID'] = ['fom-support-telegram', 'datfo-sales-telegram']
            else:
                filters['ORIGINATOR_ID'] = 'datfo-sales-telegram'
        return 'crm.deal.list', {'select': ['ID'], 'filter': filters, 'start': 0}

    def counts(self, start, end, responsible_id=None, *, kinds=('deal', 'pharmacy', 'support')):
        report = {'bot': {}, 'all': {}}
        for source in report:
            for kind in kinds:
                method, payload = self.query(kind, start, end, responsible_id, bot=source == 'bot')
                try:
                    response = self.api.call(method, payload)
                    total = response.get('total')
                    # Counting the first 50 rows would underreport large selections.
                    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
                        raise RemoteError('INVALID_STATISTICS_TOTAL')
                    report[source][kind] = {'count': total}
                except RemoteError as exc:
                    report[source][kind] = {'error': exc.code}
        return report

    def total(self, method, payload):
        total = self.api.call(method, payload).get('total')
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise RemoteError('INVALID_STATISTICS_TOTAL')
        return total

    def personal_counts(self, start, end, creator_id, store, user_id, *, kinds):
        """Disjoint native-created and bot-created counts, with no assignee filter."""
        from .creation_attribution import bot_creation_ids
        own_ids = bot_creation_ids(store, user_id)
        report = {'bot': {}, 'all': {}}
        for kind in kinds:
            try:
                count = 0
                for offset in range(0, len(own_ids[kind]), 100):
                    method, payload = self.query(kind, start, end, None, bot=True)
                    payload['filter']['@id' if kind == 'pharmacy' else '@ID'] = own_ids[kind][offset:offset+100]
                    count += self.total(method, payload)
                report['bot'][kind] = {'count': count}
            except RemoteError as exc:
                report['bot'][kind] = {'error': exc.code}
            try:
                creator = 'createdBy' if kind == 'pharmacy' else 'CREATED_BY_ID'
                method, payload = self.query(kind, start, end, None, bot=False)
                payload['filter'][creator] = creator_id
                created = self.total(method, payload)
                # Subtract native bot-origin intersection: works also when the
                # REST connection belongs to the manager and shares their author ID.
                method, payload = self.query(kind, start, end, None, bot=True)
                payload['filter'][creator] = creator_id
                through_bot = self.total(method, payload)
                if through_bot > created:
                    raise RemoteError('INCONSISTENT_STATISTICS_TOTAL')
                report['all'][kind] = {'count': created - through_bot}
            except RemoteError as exc:
                report['all'][kind] = {'error': exc.code}
        return report
