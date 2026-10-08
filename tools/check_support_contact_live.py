"""Read-only native schema confirmation; no contacts or requests are created."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from datfo_crm_bot.api import Bitrix
from datfo_crm_bot.config import connection_from_env, load_env


def main():
    load_env(ROOT/'.env')
    _,hook=connection_from_env(live=True)
    api=Bitrix(hook)
    contacts=api.call('crm.contact.fields')['result']
    deals=api.call('crm.item.fields',{'entityTypeId':2,'useOriginalUfNames':'Y'})['result']['fields']
    keys=['NAME','PHONE','ASSIGNED_BY_ID','ORIGINATOR_ID','ORIGIN_ID','OPENED']
    result={'contact_fields':{key:{name:contacts.get(key,{}).get(name) for name in ['type','isRequired','isReadOnly']} for key in keys},
            'request_contacts':{name:{key:deals.get(name,{}).get(key) for key in ['type','isReadOnly','isMultiple']} for name in ['contactId','contactIds']},
            'required_contact_fields':[key for key,value in contacts.items() if isinstance(value,dict) and value.get('isRequired') and not value.get('isReadOnly')]}
    result['ok']=all(isinstance(contacts.get(key),dict) and not contacts[key].get('isReadOnly') for key in keys) and isinstance(deals.get('contactIds'),dict) and not deals['contactIds'].get('isReadOnly')
    (ROOT/'data/support-contact-live-fields-20261008.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
    if not result['ok']:
        raise SystemExit('Native contact fields are not confirmed')


if __name__=='__main__':
    main()
