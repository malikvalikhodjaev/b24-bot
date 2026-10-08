"""Resume only the requisite of an already confirmed, partially saved request."""
from pathlib import Path
import argparse
from dataclasses import replace
import json
import re
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from datfo_crm_bot.api import Bitrix, RemoteError
from datfo_crm_bot.config import ROOT, load_env, connection_from_env, User
from datfo_crm_bot.demo import demo_config
from datfo_crm_bot.okb_crm import OkbCrm, OkbSettings
from datfo_crm_bot.okb_service import OkbBot
from datfo_crm_bot.storage import Store


class DiagnosticBitrix(Bitrix):
    def call(self, method, payload=None):
        if method not in {'crm.requisite.add', 'crm.item.add'}:
            return super().call(method, payload)
        request = Request(self.base + method + '.json', data=json.dumps(payload).encode(),
                          headers={'Content-Type': 'application/json'}, method='POST')
        http_error = None
        try:
            with urlopen(request, timeout=40) as response:
                result = json.loads(response.read())
        except HTTPError as exc:
            http_error = exc.code
            result = json.loads(exc.read())
        if http_error or 'result' not in result or result.get('error'):
            # Retain facts about a field validation failure, never remote text/URLs.
            detail = str(result.get('error', '')) + ' ' + str(result.get('error_description', ''))
            safe_detail = detail.replace(self.base, '[connection]')
            for part in self.base.rstrip('/').split('/')[-2:]:
                if len(part) >= 8:
                    safe_detail = safe_detail.replace(part, '[redacted]')
            safe_detail = re.sub(r'https?://\S+|[A-Za-z0-9_-]{32,}', '[redacted]', safe_detail)[:500]
            diagnostic = {'method': method, 'code': RemoteError(str(result.get('error', 'MISSING_RESULT'))).code,
                          'http_status': http_error, 'response_keys': list(result),
                          'mentions_xml_id': 'XML_ID' in detail.upper(),
                          'mentions_50': '50' in detail,
                          'mentions_inn': 'ИНН' in detail.upper() or 'RQ_INN' in detail.upper(),
                          'mentions_access': 'ACCESS' in detail.upper() or 'ДОСТУП' in detail.upper()}
            diagnostic['safe_detail'] = safe_detail
            path = 'requisite-error-diagnostic.json' if method == 'crm.requisite.add' else 'item-error-diagnostic.json'
            (ROOT / 'data' / path).write_text(json.dumps(diagnostic, indent=2), encoding='utf-8')
            print(json.dumps(diagnostic))
            raise RemoteError(diagnostic['code'])
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    parser.add_argument('--recover-diagnostic', action='store_true')
    args = parser.parse_args()
    load_env(ROOT / '.env')
    token, hook = connection_from_env(live=True)
    store = Store(ROOT / 'data/crm_bot.sqlite3')
    try:
        operation = store.operation(args.request)
        allowed = {'created', 'succeeded', 'existing'} | ({'sending'} if args.recover_diagnostic else set())
        if not operation or operation['value'].get('workflow') != 'okb' or operation['status'] not in allowed:
            raise RuntimeError('Only a confirmed partial pharmacy request can be resumed')
        registration = store.registration(operation['user_id'])
        company_step = store.operation_step(args.request, 'company')
        if not registration or registration['status'] != 'approved' or not company_step or company_step['status'] != 'succeeded_write':
            raise RuntimeError('Approved user and previously saved company are required')
        settings = OkbSettings.load()
        config = replace(demo_config(ROOT / 'data/crm_bot.sqlite3'), token=token, webhook=hook,
            targets={'pharmacy': settings.pharmacy}, users={operation['user_id']: User(registration['bitrix_id'], False, registration['name'])})
        crm = OkbCrm(config, DiagnosticBitrix(hook), settings)
        bot = OkbBot(config, store, crm, None)
        state, company = operation['value'], company_step['result']
        previous = store.operation_step(args.request, 'requisite')
        if args.recover_diagnostic and previous and previous['status'] == 'sending':
            # The preceding local diagnostic received a response without result;
            # its missing-result exception prevented the phase from recording it.
            # This explicit recovery is restricted to the user's exact request.
            if args.request != 'datfo-live-20f7c6a956ea43ac9ee3cb8fd8bb770f-pharmacy':
                raise RuntimeError('Diagnostic recovery is restricted to the inspected request')
            found = crm.find_requisite(company['id'], state['inn'])
            if found:
                store.set_operation_step(args.request, 'requisite', 'succeeded', found)
            else:
                store.set_operation_step(args.request, 'requisite', 'rejected')
        result = bot.phase(state, 'requisite', lambda: crm.find_requisite(company['id'], state['inn']),
                           lambda: crm.create_requisite(state, company), verify_only=False)
        print(json.dumps({'request': args.request, 'company_id': company['id'], 'requisite': result}))
    finally:
        store.close()


if __name__ == '__main__':
    try:
        main()
    except RemoteError as exc:
        print('Requisite not confirmed: ' + exc.code)
        raise SystemExit(2)
